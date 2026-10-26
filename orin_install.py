"""Transactional disk installation for the single supported Jetson BSP.

This module never loads/unloads a driver, writes registers, or reboots. The NVIDIA
initramfs generator controls its own writes; original boot artifacts are backed
up durably before invoking it, and failures restore both originals.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import gzip
import hashlib
import json
import lzma
import os
from pathlib import Path
import re
import shutil
import signal
import stat
import subprocess
import tempfile
import threading
from typing import Callable

KERNEL = "5.15.185-tegra"
VERSION = "orin-gen2-probe-3"
STOCK_SHA256 = "dde8f6726941ea4f12c5e48020550db6b1903b2a816d365aeaeff5e27edf6415"
SOURCE_SHA256 = "3ff1616f8924e006d724fc4568025f9f1a11e49ddb801a3a7e56694efcc20f00"
APP_FILES = ("orin-usb-gen2", "orin_usb_gen2.py", "orin_install.py", "build/orin-usb-speed")
PHASES = {"prepared", "installing", "installed", "restoring", "restored", "rolled_back", "rollback_failed"}


class InstallError(RuntimeError):
    """A preflight or installation failure; no successful installation claimed."""


class RollbackError(InstallError):
    """Restoring original boot files or their dependency indexes failed."""


def _main_thread() -> None:
    if threading.current_thread() is not threading.main_thread():
        raise InstallError("Installer mutations must run in the main thread for interruption-safe recovery")


def _error_text(error: BaseException) -> str:
    return str(error) or type(error).__name__


@contextmanager
def _recovery_signals():
    """Finish restoring boot artifacts despite further interrupt/terminate signals.

    Ignored dispositions are inherited by recovery subprocesses. Temporarily
    block these signals while changing dispositions so both handlers are always
    restored together, including when the caller has custom cancellation hooks.
    """
    _main_thread()
    protected = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, protected)
    previous_handlers = {number: signal.getsignal(number) for number in protected}
    try:
        for number in protected:
            signal.signal(number, signal.SIG_IGN)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        yield
    finally:
        signal.pthread_sigmask(signal.SIG_BLOCK, protected)
        try:
            for number, handler in previous_handlers.items():
                signal.signal(number, handler)
        finally:
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


@dataclass(frozen=True)
class InstallPaths:
    module: Path = Path(f"/lib/modules/{KERNEL}/kernel/drivers/usb/gadget/udc/tegra-xudc.ko")
    initrd: Path = Path("/boot/initrd")
    boot_config: Path = Path("/boot/extlinux/extlinux.conf")
    state: Path = Path(f"/var/lib/orin-usb-gen2/{KERNEL}")
    app: Path = Path("/usr/local/libexec/orin-usb-gen2")
    launcher: Path = Path("/usr/local/bin/orin-usb-gen2")
    unit: Path = Path("/etc/systemd/system/orin-usb-gen2.service")


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _safe_path(path: Path, *, regular: bool = False) -> None:
    """Reject symlinks, including redirected managed parent directories.

    Ubuntu's standard /lib -> /usr/lib merge is the sole allowed ancestor alias.
    The destination itself is always required to be a regular file when present.
    """
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            if candidate == Path("/lib") and candidate.resolve() == Path("/usr/lib"):
                continue
            raise InstallError(f"Refusing symlink: {candidate}")
    if regular and (not path.exists() or not stat.S_ISREG(path.stat().st_mode)):
        raise InstallError(f"Expected regular file: {path}")
    if path.exists() and not regular and not (path.is_file() or path.is_dir()):
        raise InstallError(f"Unsupported filesystem object: {path}")


def _owned(path: Path, mode: int | None = None) -> None:
    metadata = path.stat()
    permissions = stat.S_IMODE(metadata.st_mode)
    if metadata.st_uid != os.geteuid() or permissions & 0o022 or (mode is not None and permissions != mode):
        raise InstallError(f"Unsafe ownership or permissions: {path}")


def _atomic_copy(source: Path, target: Path, mode: int) -> None:
    _safe_path(source, regular=True)
    _safe_path(target)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    scratch = Path(name)
    try:
        with os.fdopen(fd, "wb") as output, source.open("rb") as input_file:
            shutil.copyfileobj(input_file, output, 1024 * 1024)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(scratch, target)
        _fsync_dir(target.parent)
    finally:
        scratch.unlink(missing_ok=True)


def _atomic_bytes(data: bytes, target: Path, mode: int) -> None:
    _safe_path(target)
    fd, name = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    scratch = Path(name)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(data)
            os.fchmod(output.fileno(), mode)
            output.flush()
            os.fsync(output.fileno())
        os.replace(scratch, target)
        _fsync_dir(target.parent)
    finally:
        scratch.unlink(missing_ok=True)


class Installer:
    def __init__(self, bundle: Path, paths: InstallPaths = InstallPaths(), runner: Callable = subprocess.run):
        self.bundle = Path(bundle)
        self.paths = paths
        self.runner = runner

    @property
    def manifest_path(self) -> Path:
        return self.paths.state / "manifest.json"

    def _command(self, *args: str, allowed: tuple[int, ...] = (0,)) -> str:
        result = self.runner(list(args), check=False, capture_output=True, text=True,
                             env={**os.environ, "PATH": "/usr/sbin:/usr/bin:/sbin:/bin"})
        if result.returncode not in allowed:
            detail = (result.stderr or result.stdout or "").strip()
            raise InstallError(f"{' '.join(args)} failed ({result.returncode}): {detail}")
        return (result.stdout or "").strip()

    @contextmanager
    def _locked(self):
        _safe_path(self.paths.state)
        self.paths.state.mkdir(parents=True, mode=0o700, exist_ok=True)
        if stat.S_IMODE(self.paths.state.stat().st_mode) != 0o700 or self.paths.state.stat().st_uid != os.geteuid():
            raise InstallError("Backup directory must be owned by the current user and mode 0700")
        lock = self.paths.state / "lock"
        _safe_path(lock)
        fd = os.open(lock, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _load(self) -> dict | None:
        _safe_path(self.paths.state)
        if self.paths.state.exists() and (stat.S_IMODE(self.paths.state.stat().st_mode) != 0o700 or self.paths.state.stat().st_uid != os.geteuid()):
            raise InstallError("Backup directory must be owned by the current user and mode 0700")
        _safe_path(self.manifest_path)
        if not self.manifest_path.exists():
            return None
        _safe_path(self.manifest_path, regular=True)
        _owned(self.manifest_path, 0o600)
        try:
            data = json.loads(self.manifest_path.read_text())
            if data["schema"] != 1 or data["kernel"] != KERNEL or data["phase"] not in PHASES:
                raise ValueError("unsupported schema, kernel or phase")
            digests = [data["module_sha256"], data["unit_sha256"], *data["app_sha256"].values()]
            if set(data["app_sha256"]) != set(APP_FILES):
                raise ValueError("unexpected application inventory")
            for key in ("module", "initrd"):
                entry = data["originals"][key]
                digests.append(entry["sha256"])
                if type(entry["mode"]) is not int or not 0 <= entry["mode"] <= 0o777:
                    raise ValueError("invalid original file mode")
            if data.get("initrd_sha256") is not None:
                digests.append(data["initrd_sha256"])
            if any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-f]{64}", item) for item in digests):
                raise ValueError("invalid digest")
        except (ValueError, TypeError, KeyError, AttributeError) as error:
            raise InstallError(f"Invalid recovery manifest; retain backups and recover manually: {error}") from error
        return data

    def _save(self, manifest: dict, phase: str) -> None:
        manifest["phase"] = phase
        _atomic_bytes((json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), self.manifest_path, 0o600)

    def _unit_bytes(self) -> bytes:
        return ("[Unit]\nDescription=Enable experimental Orin USB Gen2\n"
                "Requires=nv-l4t-usb-device-mode.service\nAfter=nv-l4t-usb-device-mode.service\n\n"
                "[Service]\nType=oneshot\nRemainAfterExit=yes\n"
                f"ExecStart={self.paths.app}/orin-usb-gen2 boot-enable\n\n"
                "[Install]\nWantedBy=multi-user.target\n").encode()

    def _layout(self, *, missing_initrd: bool = False) -> None:
        for path in (self.paths.module, self.paths.initrd, self.paths.boot_config):
            if missing_initrd and path == self.paths.initrd and not path.exists():
                _safe_path(path)
                continue
            _safe_path(path, regular=True)
        for path in (self.paths.state, self.paths.app, self.paths.unit):
            _safe_path(path)
        _safe_path(self.paths.launcher.parent)
        initrds = []
        for line in self.paths.boot_config.read_text().splitlines():
            words = line.split("#", 1)[0].split()
            if words and words[0].upper() == "INITRD":
                initrds.append(words[1:])
        if not initrds or any(words != ["/boot/initrd"] for words in initrds):
            raise InstallError("Only extlinux boot entries using exactly /boot/initrd are supported")

    def _bundle(self) -> dict:
        module = self.bundle / "build/tegra-xudc.ko"
        source = self.bundle / "build/tegra-xudc.c"
        for path in (module, source, *(self.bundle / name for name in APP_FILES)):
            _safe_path(path, regular=True)
        if _sha(source) != SOURCE_SHA256:
            raise InstallError("Patched source differs from the exact tested source; rebuild with scripts/build.sh")
        if self._command("modinfo", "-F", "version", str(module)) != VERSION:
            raise InstallError(f"Module version must be {VERSION}")
        vermagic = self._command("modinfo", "-F", "vermagic", str(module))
        stock_vermagic = self._command("modinfo", "-F", "vermagic", str(self.paths.module))
        if not vermagic or vermagic.split()[0] != KERNEL or vermagic != stock_vermagic:
            raise InstallError("Module vermagic must match the exact supported installed kernel module")
        return {"module_sha256": _sha(module),
                "app_sha256": {name: _sha(self.bundle / name) for name in APP_FILES},
                "unit_sha256": hashlib.sha256(self._unit_bytes()).hexdigest()}

    def _backups(self, manifest: dict) -> None:
        for name in ("module", "initrd"):
            path = self.paths.state / f"{name}.original"
            _safe_path(path, regular=True)
            _owned(path, 0o600)
            if _sha(path) != manifest["originals"][name]["sha256"]:
                raise InstallError(f"Original {name} backup is corrupt; refusing restoration")
        if manifest["originals"]["module"]["sha256"] != STOCK_SHA256:
            raise InstallError("Original module backup is not the supported stock module")

    def _managed_files(self, manifest: dict, *, partial: bool = False) -> None:
        expected = {self.paths.app / name: digest for name, digest in manifest["app_sha256"].items()}
        expected[self.paths.unit] = manifest["unit_sha256"]
        _safe_path(self.paths.app)
        if self.paths.app.exists():
            _owned(self.paths.app, 0o755)
            expected_dirs = {self.paths.app / "build"}
            for path in self.paths.app.rglob("*"):
                _safe_path(path)
                _owned(path)
                if path not in expected and path not in expected_dirs and path.name != "__pycache__" and "__pycache__" not in path.parts:
                    raise InstallError(f"Unexpected managed application content: {path}")
        for path, digest in expected.items():
            _safe_path(path)
            if partial and not path.exists():
                continue
            _safe_path(path, regular=True)
            _owned(path)
            if _sha(path) != digest:
                raise InstallError(f"Managed file drift: {path}")
        launcher = self.paths.launcher
        _safe_path(launcher.parent)
        if launcher.is_symlink():
            if os.readlink(launcher) != str(self.paths.app / "orin-usb-gen2"):
                raise InstallError("Managed launcher was redirected")
        elif launcher.exists() or not partial:
            raise InstallError("Managed launcher is missing or is not the expected symlink")

    def _installed(self, manifest: dict) -> None:
        self._layout()
        self._backups(manifest)
        self._managed_files(manifest)
        if _sha(self.paths.module) != manifest["module_sha256"] or _sha(self.paths.initrd) != manifest.get("initrd_sha256"):
            raise InstallError("Installed boot artifacts drifted; inspect them before recovery")

    def is_installed(self) -> bool:
        manifest = self._load()
        if manifest is None or manifest["phase"] in {"restored", "rolled_back"}:
            return False
        if manifest["phase"] != "installed":
            raise InstallError(f"Incomplete installation ({manifest['phase']}); run restore before installing again")
        self._installed(manifest)
        return True

    def boot_enabled(self) -> bool:
        _safe_path(self.paths.unit)
        output = self._command("systemctl", "is-enabled", self.paths.unit.name, allowed=(0, 1, 4))
        if output == "enabled":
            return True
        if output in {"disabled", "static", "not-found"} or (not output and not self.paths.unit.exists()):
            return False
        raise InstallError(f"Unexpected boot-service state: {output!r}")

    def set_boot_enabled(self, enabled: bool) -> None:
        _main_thread()
        if type(enabled) is not bool:
            raise TypeError("enabled must be bool")
        with self._locked():
            manifest = self._load()
            if manifest is None:
                raise InstallError("Install the patched module and boot service first")
            self._managed_files(manifest)
            if enabled and not self.is_installed():
                raise InstallError("Cannot enable boot mode before a complete installation")
            previous = self.boot_enabled()
            if previous == enabled:
                return
            try:
                self._command("systemctl", "enable" if enabled else "disable", self.paths.unit.name)
                if self.boot_enabled() != enabled:
                    raise InstallError("systemd did not retain the requested boot-service state")
            except BaseException as original:
                try:
                    with _recovery_signals():
                        self._command("systemctl", "enable" if previous else "disable", self.paths.unit.name)
                        if self.boot_enabled() != previous:
                            raise InstallError("Previous boot-service state was not restored")
                except BaseException as recovery:
                    raise RollbackError(f"Boot policy change failed: {_error_text(original)}; restoring previous policy failed: {_error_text(recovery)}") from recovery
                raise InstallError(f"Boot policy change failed; previous policy restored: {_error_text(original)}") from original

    def _inspect_initrd(self, digest: str) -> dict:
        copies = 0
        with tempfile.TemporaryDirectory(prefix="orin-initrd-") as temporary:
            extraction = Path(temporary).resolve()
            self._command("unmkinitramfs", str(self.paths.initrd), str(extraction))
            for path in extraction.rglob("*"):
                if not any(path.name.startswith(name) for name in ("tegra-xudc.ko", "tegra_xudc.ko")):
                    continue
                _safe_path(path, regular=True)
                if path.suffix == ".xz":
                    with lzma.open(path, "rb") as stream:
                        actual = hashlib.sha256(stream.read()).hexdigest()
                elif path.suffix == ".gz":
                    with gzip.open(path, "rb") as stream:
                        actual = hashlib.sha256(stream.read()).hexdigest()
                elif path.suffix == ".ko":
                    actual = _sha(path)
                else:
                    raise InstallError(f"Unsupported module compression in initramfs: {path.name}")
                if actual != digest:
                    raise InstallError("Initramfs contains a tegra-xudc module different from the staged module")
                copies += 1
        return {"module_copies_verified": copies}

    def _write_app(self, manifest: dict) -> None:
        self.paths.app.parent.mkdir(parents=True, exist_ok=True)
        if not self.paths.app.exists():
            with tempfile.TemporaryDirectory(prefix=".orin-app-", dir=self.paths.app.parent) as directory:
                stage = Path(directory) / "app"
                stage.mkdir(mode=0o755)
                stage.chmod(0o755)
                (stage / "build").mkdir(mode=0o755)
                (stage / "build").chmod(0o755)
                for name in APP_FILES:
                    _atomic_copy(self.bundle / name, stage / name, 0o755 if name in {"orin-usb-gen2", "build/orin-usb-speed"} else 0o644)
                os.replace(stage, self.paths.app)
                _fsync_dir(self.paths.app.parent)
        self.paths.unit.parent.mkdir(parents=True, exist_ok=True)
        if not self.paths.unit.exists():
            _atomic_bytes(self._unit_bytes(), self.paths.unit, 0o644)
        self.paths.launcher.parent.mkdir(parents=True, exist_ok=True)
        if not self.paths.launcher.is_symlink():
            self.paths.launcher.symlink_to(self.paths.app / "orin-usb-gen2")
            _fsync_dir(self.paths.launcher.parent)
        self._managed_files(manifest)
        self._command("systemctl", "daemon-reload")

    def _recover(self, manifest: dict, phase: str) -> None:
        with _recovery_signals():
            try:
                self._backups(manifest)
                self._save(manifest, "restoring")
            except BaseException as error:
                raise RollbackError(f"Cannot start rollback; original recovery state retained: {_error_text(error)}") from error
            errors = []
            if self.paths.unit.exists():
                try:
                    self._command("systemctl", "disable", self.paths.unit.name)
                    if self.boot_enabled():
                        raise InstallError("Boot service remained enabled")
                except BaseException as error:
                    errors.append(f"disable boot service: {_error_text(error)}")
            for name, target in (("module", self.paths.module), ("initrd", self.paths.initrd)):
                try:
                    _atomic_copy(self.paths.state / f"{name}.original", target, manifest["originals"][name]["mode"])
                except BaseException as error:
                    errors.append(f"restore {name}: {_error_text(error)}")
            try:
                self._command("depmod", "-a", KERNEL)
            except BaseException as error:
                errors.append(f"restore dependency index: {_error_text(error)}")
            try:
                self._save(manifest, "rollback_failed" if errors else phase)
            except BaseException as error:
                errors.append(f"record recovery outcome: {_error_text(error)}")
            if errors:
                raise RollbackError("Rollback incomplete; do not reboot: " + "; ".join(errors))

    def install(self, dry_run: bool = False) -> dict:
        if not dry_run:
            _main_thread()
        self._layout()
        if self.runner is subprocess.run:
            for program in ("modinfo", "depmod", "nv-update-initrd", "unmkinitramfs", "systemctl"):
                if shutil.which(program, path="/usr/sbin:/usr/bin:/sbin:/bin") is None:
                    raise InstallError(f"Required command is unavailable: {program}")
        manifest = self._load()
        if manifest is not None and manifest["phase"] == "installed":
            self._installed(manifest)
            for name, digest in manifest["app_sha256"].items():
                if _sha(self.bundle / name) != digest:
                    raise InstallError("Installed application differs from this bundle; upgrades require separate review")
            if (self.bundle / "build/tegra-xudc.ko").exists() and _sha(self.bundle / "build/tegra-xudc.ko") != manifest["module_sha256"]:
                raise InstallError("Installed module differs from this build; upgrades require separate review")
            return {"installed": True, "changed": False, "dry_run": dry_run, "reboot_required": True}
        if manifest is not None and manifest["phase"] not in {"restored", "rolled_back"}:
            raise InstallError(f"Incomplete installation ({manifest['phase']}); run restore first")
        payload = self._bundle()
        if manifest is None:
            if _sha(self.paths.module) != STOCK_SHA256:
                raise InstallError("Installed module is not the exact supported stock module")
            for path in (self.paths.app, self.paths.unit, self.paths.launcher):
                if path.exists() or path.is_symlink():
                    raise InstallError(f"Refusing pre-existing unmanaged path: {path}")
            if self.boot_enabled():
                raise InstallError("Refusing a pre-existing enabled boot-service policy")
            for name in ("module.original", "initrd.original"):
                if (self.paths.state / name).exists() or (self.paths.state / name).is_symlink():
                    raise InstallError("Backups exist without a manifest; no boot files changed, inspect recovery state")
        else:
            self._backups(manifest)
            self._managed_files(manifest, partial=True)
            if any(payload[key] != manifest[key] for key in payload):
                raise InstallError("A different build cannot replace the preserved installation")
            if any(_sha(target) != manifest["originals"][name]["sha256"] for name, target in (("module", self.paths.module), ("initrd", self.paths.initrd))):
                raise InstallError("Restored boot files drifted; refusing installation")
        self._inspect_initrd(STOCK_SHA256)
        if dry_run:
            return {"installed": False, "changed": False, "dry_run": True, "reboot_required": True}
        with self._locked():
            if self._load() != manifest:
                raise InstallError("Installation state changed concurrently; retry")
            if manifest is None:
                manifest = {"schema": 1, "kernel": KERNEL, "phase": "prepared", **payload,
                            "initrd_sha256": None, "originals": {}}
                for name, target in (("module", self.paths.module), ("initrd", self.paths.initrd)):
                    manifest["originals"][name] = {"sha256": _sha(target), "mode": stat.S_IMODE(target.stat().st_mode)}
                    backup = self.paths.state / f"{name}.original"
                    if backup.exists() or backup.is_symlink():
                        raise InstallError("Refusing to overwrite an original backup")
                    _atomic_copy(target, backup, 0o600)
                self._save(manifest, "prepared")
            self._backups(manifest)
            try:
                self._save(manifest, "installing")
                _atomic_copy(self.bundle / "build/tegra-xudc.ko", self.paths.module, 0o644)
                if _sha(self.paths.module) != manifest["module_sha256"]:
                    raise InstallError("Build changed during installation")
                self._command("depmod", "-a", KERNEL)
                self._command("nv-update-initrd")
                _safe_path(self.paths.initrd, regular=True)
                inspection = self._inspect_initrd(manifest["module_sha256"])
                _atomic_copy(self.paths.initrd, self.paths.initrd, manifest["originals"]["initrd"]["mode"])
                manifest["initrd_sha256"] = _sha(self.paths.initrd)
                manifest["initrd_inspection"] = inspection
                self._write_app(manifest)
                self._save(manifest, "installed")
            except BaseException as original:
                try:
                    self._recover(manifest, "rolled_back")
                except BaseException as recovery:
                    raise RollbackError(f"Installation failed: {_error_text(original)}; rollback also failed: {_error_text(recovery)}. Do not reboot.") from recovery
                raise InstallError(f"Installation failed; original boot files restored: {_error_text(original)}") from original
        return {"installed": True, "changed": True, "dry_run": False, "reboot_required": True,
                "boot_enabled": False, "initrd_inspection": inspection}

    def restore(self, dry_run: bool = False) -> dict:
        if not dry_run:
            _main_thread()
        manifest = self._load()
        if manifest is None:
            raise InstallError("No installation manifest; refusing to guess original boot files")
        self._layout(missing_initrd=manifest["phase"] in {"prepared", "installing", "restoring", "rollback_failed"})
        self._backups(manifest)
        self._managed_files(manifest, partial=manifest["phase"] != "installed")
        if manifest["phase"] == "installed":
            self._installed(manifest)
        elif _sha(self.paths.module) not in {manifest["module_sha256"], manifest["originals"]["module"]["sha256"]}:
            raise InstallError("Module drifted outside the recorded transaction; refusing restoration")
        if manifest["phase"] in {"restored", "rolled_back"}:
            if _sha(self.paths.initrd) != manifest["originals"]["initrd"]["sha256"]:
                raise InstallError("Restored initramfs drifted; refusing to overwrite it")
        if dry_run:
            return {"restored": False, "changed": False, "dry_run": True, "reboot_required": True}
        with self._locked():
            if self._load() != manifest:
                raise InstallError("Installation state changed concurrently; retry")
            self._recover(manifest, "restored")
        return {"restored": True, "changed": True, "dry_run": False, "reboot_required": True,
                "boot_enabled": False}
