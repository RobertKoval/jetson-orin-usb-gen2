"""Boot-artifact contracts using temporary files and simulated vendor commands."""
from pathlib import Path
import hashlib
import json
import os
import signal
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

import orin_install as install

STOCK = b"original supported module"
PATCHED = b"patched supported module"
SOURCE = b"exact patched source"
ORIGINAL_INITRD = b"original initramfs"
NEW_INITRD = b"generated initramfs"


def sha(data):
    return hashlib.sha256(data).hexdigest()


class VendorCommands:
    def __init__(self, paths):
        self.paths = paths
        self.calls = []
        self.enabled = False
        self.failures = {}
        self.old_initrd_module = False
        self.omit_initrd_module = False
        self.version = install.VERSION
        self.vermagic = install.KERNEL + " SMP preempt mod_unload aarch64"

    def __call__(self, args, **kwargs):
        self.calls.append(args)
        code, output = 0, ""
        if args[:3] == ["modinfo", "-F", "version"]:
            output = self.version
        elif args[:3] == ["modinfo", "-F", "vermagic"]:
            output = self.vermagic
        elif args[0] == "nv-update-initrd":
            # The vendor generator may write the live initrd before it fails.
            self.paths.initrd.write_bytes(NEW_INITRD)
        elif args[0] == "unmkinitramfs":
            if self.omit_initrd_module:
                return subprocess.CompletedProcess(args, 0, "", "")
            module = Path(args[2]) / "main/lib/modules" / install.KERNEL / "tegra-xudc.ko"
            module.parent.mkdir(parents=True)
            data = STOCK if Path(args[1]).read_bytes() == ORIGINAL_INITRD or self.old_initrd_module else PATCHED
            module.write_bytes(data)
        elif args[:2] == ["systemctl", "is-enabled"]:
            output, code = ("enabled", 0) if self.enabled else ("disabled", 1)
        elif args[:2] == ["systemctl", "enable"]:
            self.enabled = True
        elif args[:2] == ["systemctl", "disable"]:
            self.enabled = False
        key = " ".join(args[:2]) if args[0] == "systemctl" else args[0]
        count = self.failures.get(key, 0)
        if count:
            self.failures[key] = count - 1
            code = 1
        return subprocess.CompletedProcess(args, code, output, "injected vendor failure" if code and not output else "")


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.bundle = self.root / "bundle"
        self.bundle.mkdir()
        for name in install.APP_FILES:
            file = self.bundle / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(("payload:" + name).encode())
        (self.bundle / "build/tegra-xudc.c").write_bytes(SOURCE)
        (self.bundle / "build/tegra-xudc.ko").write_bytes(PATCHED)
        self.paths = install.InstallPaths(
            module=self.root / "modules/tegra-xudc.ko",
            initrd=self.root / "boot/initrd",
            boot_config=self.root / "boot/extlinux/extlinux.conf",
            state=self.root / "state/kernel",
            app=self.root / "libexec/orin-usb-gen2",
            launcher=self.root / "bin/orin-usb-gen2",
            unit=self.root / "systemd/orin-usb-gen2.service",
        )
        for file, data in ((self.paths.module, STOCK), (self.paths.initrd, ORIGINAL_INITRD),
                           (self.paths.boot_config, b"LABEL primary\n INITRD /boot/initrd\n")):
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_bytes(data)
        self.runner = VendorCommands(self.paths)
        self.installer = install.Installer(self.bundle, self.paths, self.runner)
        self.addCleanup(patch.stopall)
        patch.object(install, "STOCK_SHA256", sha(STOCK)).start()
        patch.object(install, "SOURCE_SHA256", sha(SOURCE)).start()

    def snapshot(self):
        return {str(path.relative_to(self.root)): ("link", os.readlink(path)) if path.is_symlink()
                else ("file", path.read_bytes()) if path.is_file()
                else ("dir", path.stat().st_mode & 0o777)
                for path in self.root.rglob("*")}

    def assert_original_boot(self):
        self.assertEqual(self.paths.module.read_bytes(), STOCK)
        self.assertEqual(self.paths.initrd.read_bytes(), ORIGINAL_INITRD)

    def manifest(self):
        return json.loads(self.installer.manifest_path.read_text())

    def test_install_is_idempotent_and_restore_preserves_both_originals(self):
        result = self.installer.install()
        self.assertTrue(result["changed"])
        self.assertEqual(self.paths.module.read_bytes(), PATCHED)
        self.assertEqual(self.paths.initrd.read_bytes(), NEW_INITRD)
        self.assertEqual(result["initrd_inspection"]["module_copies_verified"], 1)
        self.assertFalse(self.runner.enabled)
        self.assertEqual((self.paths.state / "module.original").read_bytes(), STOCK)
        self.assertEqual((self.paths.state / "initrd.original").read_bytes(), ORIGINAL_INITRD)
        for name in install.APP_FILES:
            self.assertEqual((self.paths.app / name).read_bytes(), (self.bundle / name).read_bytes())
        snapshot = self.snapshot()
        self.assertFalse(self.installer.install()["changed"])
        self.assertEqual(self.snapshot(), snapshot)
        self.assertTrue(self.installer.is_installed())
        self.installer.set_boot_enabled(True)
        self.assertTrue(self.installer.boot_enabled())
        self.installer.restore()
        self.assert_original_boot()
        self.assertFalse(self.installer.boot_enabled())
        self.assertFalse(self.installer.is_installed())
        self.assertEqual((self.paths.state / "module.original").read_bytes(), STOCK)
        self.assertEqual((self.paths.state / "initrd.original").read_bytes(), ORIGINAL_INITRD)

    def test_dry_run_and_rejected_stock_leave_files_unchanged(self):
        before = self.snapshot()
        self.installer.install(dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.paths.module.write_bytes(b"unknown module")
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "stock module"):
            self.installer.install()
        self.assertEqual(self.snapshot(), before)

    def test_wrong_source_version_and_vermagic_do_not_change_boot_files(self):
        variants = (("source", b"different source"), ("version", "wrong-version"),
                    ("vermagic", "5.15.184-tegra SMP"))
        for field, value in variants:
            with self.subTest(field=field):
                source = self.bundle / "build/tegra-xudc.c"
                if field == "source":
                    source.write_bytes(value)
                else:
                    setattr(self.runner, field, value)
                before = self.snapshot()
                with self.assertRaises(install.InstallError):
                    self.installer.install()
                self.assertEqual(self.snapshot(), before)
                source.write_bytes(SOURCE)
                self.runner.version = install.VERSION
                self.runner.vermagic = install.KERNEL + " SMP preempt mod_unload aarch64"

    def test_unsupported_boot_entry_is_rejected_without_writes(self):
        for entry in (b"LABEL primary\n", b"INITRD /boot/alternate\n", b"INITRD /boot/initrd extra\n"):
            with self.subTest(entry=entry):
                self.paths.boot_config.write_bytes(entry)
                before = self.snapshot()
                with self.assertRaisesRegex(install.InstallError, "extlinux"):
                    self.installer.install()
                self.assertEqual(self.snapshot(), before)

    def test_unknown_managed_paths_are_not_overwritten(self):
        for path in (self.paths.app, self.paths.unit, self.paths.launcher):
            with self.subTest(path=path):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"existing user content")
                before = self.snapshot()
                with self.assertRaisesRegex(install.InstallError, "unmanaged"):
                    self.installer.install()
                self.assertEqual(self.snapshot(), before)
                path.unlink()

    def test_symlink_initrd_and_redirected_parent_are_rejected(self):
        alternate = self.root / "alternate-initrd"
        self.paths.initrd.rename(alternate)
        self.paths.initrd.symlink_to(alternate)
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "symlink"):
            self.installer.install()
        self.assertEqual(self.snapshot(), before)
        self.paths.initrd.unlink()
        alternate.rename(self.paths.initrd)
        original_parent = self.paths.module.parent
        redirected = self.root / "redirected-modules"
        original_parent.rename(redirected)
        original_parent.symlink_to(redirected)
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "symlink"):
            self.installer.install()
        self.assertEqual(self.snapshot(), before)

    def test_depmod_and_initrd_failures_restore_original_bytes(self):
        for command in ("depmod", "nv-update-initrd"):
            with self.subTest(command=command):
                self.runner.failures[command] = 1
                with self.assertRaisesRegex(install.InstallError, "original boot files restored"):
                    self.installer.install()
                self.assert_original_boot()
                self.assertEqual(self.manifest()["phase"], "rolled_back")
                self.assertFalse(self.installer.is_installed())

    def test_bad_initramfs_copy_triggers_full_rollback(self):
        self.runner.old_initrd_module = True
        with self.assertRaisesRegex(install.InstallError, "Initramfs contains"):
            self.installer.install()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rolled_back")

    def test_application_copy_failure_restores_boot_files(self):
        original_copy = install._atomic_copy
        def copy(source, target, mode):
            if target.name == "orin_usb_gen2.py":
                raise OSError("disk full while copying application")
            original_copy(source, target, mode)
        with patch.object(install, "_atomic_copy", copy):
            with self.assertRaisesRegex(install.InstallError, "disk full"):
                self.installer.install()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rolled_back")

    def test_rollback_failure_is_distinct_and_can_be_recovered(self):
        self.runner.failures["depmod"] = 2
        with self.assertRaisesRegex(install.RollbackError, "rollback also failed"):
            self.installer.install()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rollback_failed")
        with self.assertRaisesRegex(install.InstallError, "Incomplete installation"):
            self.installer.is_installed()
        self.installer.restore()
        self.assertEqual(self.manifest()["phase"], "restored")
        self.assert_original_boot()

    def test_interrupted_transaction_can_restore_unknown_generated_initrd(self):
        self.installer.install()
        manifest = self.manifest()
        manifest["phase"] = "installing"
        self.installer.manifest_path.write_text(json.dumps(manifest))
        self.paths.initrd.write_bytes(b"partially generated initramfs")
        with self.assertRaisesRegex(install.InstallError, "Incomplete installation"):
            self.installer.install()
        self.installer.restore()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "restored")

    def test_installed_drift_or_corrupt_backup_is_not_overwritten(self):
        self.installer.install()
        for target in (self.paths.module, self.paths.initrd, self.paths.app / "orin_usb_gen2.py",
                       self.paths.state / "initrd.original"):
            with self.subTest(target=target):
                original = target.read_bytes()
                target.write_bytes(b"unexpected external modification")
                before = self.snapshot()
                with self.assertRaises(install.InstallError):
                    self.installer.restore()
                self.assertEqual(self.snapshot(), before)
                target.write_bytes(original)

    def test_redirected_installed_launcher_cannot_enable_boot(self):
        self.installer.install()
        self.paths.launcher.unlink()
        self.paths.launcher.symlink_to(self.root / "foreign-program")
        with self.assertRaisesRegex(install.InstallError, "redirected"):
            self.installer.set_boot_enabled(True)
        self.assertFalse(self.runner.enabled)

    def test_boot_enable_requires_completed_installation(self):
        self.installer.install()
        self.installer.restore()
        with self.assertRaisesRegex(install.InstallError, "complete installation"):
            self.installer.set_boot_enabled(True)
        self.assertFalse(self.runner.enabled)

    def test_missing_initrd_after_interruption_can_be_restored(self):
        self.installer.install()
        manifest = self.manifest()
        manifest["phase"] = "installing"
        self.installer.manifest_path.write_text(json.dumps(manifest))
        self.paths.initrd.unlink()
        self.installer.restore()
        self.assert_original_boot()

    def test_initramfs_without_driver_is_explicitly_reported(self):
        self.runner.omit_initrd_module = True
        result = self.installer.install()
        self.assertEqual(result["initrd_inspection"], {"module_copies_verified": 0})
        self.assertTrue(self.installer.is_installed())

    def test_restore_dry_run_preserves_installed_files_and_boot_policy(self):
        self.installer.install()
        self.installer.set_boot_enabled(True)
        before = self.snapshot()
        self.installer.restore(dry_run=True)
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.installer.boot_enabled())

    def test_failed_boot_enable_reverts_previous_policy(self):
        self.installer.install()
        self.runner.failures["systemctl enable"] = 1
        with self.assertRaisesRegex(install.InstallError, "previous policy restored"):
            self.installer.set_boot_enabled(True)
        self.assertFalse(self.installer.boot_enabled())

    def test_failed_policy_rollback_is_not_reported_as_restored(self):
        self.installer.install()
        self.runner.failures["systemctl enable"] = 1
        self.runner.failures["systemctl disable"] = 1
        with self.assertRaisesRegex(install.RollbackError, "restoring previous policy failed"):
            self.installer.set_boot_enabled(True)

    def test_unsafe_backup_permissions_are_rejected_without_mutation(self):
        self.installer.install()
        self.paths.state.chmod(0o777)
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "mode 0700"):
            self.installer.restore()
        self.assertEqual(self.snapshot(), before)

    def test_restore_disabling_failure_is_reported_and_boot_files_are_still_restored(self):
        self.installer.install()
        self.installer.set_boot_enabled(True)
        self.runner.failures["systemctl disable"] = 1
        with self.assertRaisesRegex(install.RollbackError, "disable boot service"):
            self.installer.restore()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rollback_failed")

    def test_new_bundle_cannot_silently_upgrade_existing_installation(self):
        self.installer.install()
        (self.bundle / "orin_usb_gen2.py").write_bytes(b"new application")
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "upgrades require separate review"):
            self.installer.install()
        self.assertEqual(self.snapshot(), before)

    def test_existing_enabled_boot_policy_is_preserved_and_rejected(self):
        self.runner.enabled = True
        before = self.snapshot()
        with self.assertRaisesRegex(install.InstallError, "pre-existing enabled"):
            self.installer.install()
        self.assertEqual(self.snapshot(), before)
        self.assertTrue(self.runner.enabled)

    def test_direct_keyboard_interrupt_during_install_restores_both_boot_files(self):
        original_runner = self.installer.runner
        def interrupted_runner(args, **kwargs):
            result = original_runner(args, **kwargs)
            if args[0] == "nv-update-initrd":
                raise KeyboardInterrupt()
            return result
        self.installer.runner = interrupted_runner
        with self.assertRaisesRegex(install.InstallError, "original boot files restored: KeyboardInterrupt"):
            self.installer.install()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rolled_back")

    def test_restore_ignores_repeated_signals_between_original_module_and_initrd(self):
        self.installer.install()
        original_copy = install._atomic_copy
        observed_boundary = []
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM)}
        def cancel(number, frame):
            raise RuntimeError(f"cancelled by signal {number}")
        def copy(source, target, mode):
            original_copy(source, target, mode)
            if source == self.paths.state / "module.original":
                observed_boundary.append((self.paths.module.read_bytes(), self.paths.initrd.read_bytes()))
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGINT):
                    os.kill(os.getpid(), number)
        try:
            for number in previous:
                signal.signal(number, cancel)
            with patch.object(install, "_atomic_copy", copy):
                result = self.installer.restore()
            self.assertTrue(result["restored"])
            self.assertEqual(observed_boundary, [(STOCK, NEW_INITRD)])
            self.assert_original_boot()
            self.assertEqual(self.manifest()["phase"], "restored")
            for number in previous:
                self.assertIs(signal.getsignal(number), cancel)
        finally:
            for number, handler in previous.items():
                signal.signal(number, handler)

    def test_install_cancel_then_second_signal_during_rollback_restores_originals(self):
        original_runner = self.installer.runner
        original_copy = install._atomic_copy
        interrupted = []
        previous = signal.getsignal(signal.SIGTERM)
        def cancel(number, frame):
            raise RuntimeError("initial cancellation")
        def runner(args, **kwargs):
            result = original_runner(args, **kwargs)
            if args[0] == "nv-update-initrd":
                os.kill(os.getpid(), signal.SIGTERM)
            return result
        def copy(source, target, mode):
            original_copy(source, target, mode)
            if source == self.paths.state / "module.original":
                interrupted.append(self.paths.initrd.read_bytes())
                os.kill(os.getpid(), signal.SIGTERM)
        self.installer.runner = runner
        try:
            signal.signal(signal.SIGTERM, cancel)
            with patch.object(install, "_atomic_copy", copy):
                with self.assertRaisesRegex(install.InstallError, "original boot files restored: initial cancellation"):
                    self.installer.install()
            self.assertEqual(interrupted, [NEW_INITRD])
            self.assert_original_boot()
            self.assertEqual(self.manifest()["phase"], "rolled_back")
            self.assertIs(signal.getsignal(signal.SIGTERM), cancel)
        finally:
            signal.signal(signal.SIGTERM, previous)

    def test_base_exception_mid_restore_finishes_other_copy_and_reports_failed_recovery(self):
        self.installer.install()
        original_copy = install._atomic_copy
        def copy(source, target, mode):
            original_copy(source, target, mode)
            if source == self.paths.state / "module.original":
                raise KeyboardInterrupt()
        with patch.object(install, "_atomic_copy", copy):
            with self.assertRaisesRegex(install.RollbackError, "restore module: KeyboardInterrupt"):
                self.installer.restore()
        self.assert_original_boot()
        self.assertEqual(self.manifest()["phase"], "rollback_failed")
        self.installer.restore()
        self.assertEqual(self.manifest()["phase"], "restored")

    def test_failed_initrd_recovery_remains_explicit_and_retryable(self):
        self.installer.install()
        original_copy = install._atomic_copy
        def copy(source, target, mode):
            if source == self.paths.state / "initrd.original":
                raise KeyboardInterrupt()
            original_copy(source, target, mode)
        with patch.object(install, "_atomic_copy", copy):
            with self.assertRaisesRegex(install.RollbackError, "restore initrd: KeyboardInterrupt"):
                self.installer.restore()
        self.assertEqual(self.paths.module.read_bytes(), STOCK)
        self.assertEqual(self.paths.initrd.read_bytes(), NEW_INITRD)
        self.assertEqual(self.manifest()["phase"], "rollback_failed")
        self.installer.restore()
        self.assert_original_boot()

    def test_worker_thread_mutation_is_rejected_before_boot_or_backup_changes(self):
        before = self.snapshot()
        errors = []
        def run():
            try:
                self.installer.install()
            except install.InstallError as error:
                errors.append(str(error))
        worker = threading.Thread(target=run)
        worker.start()
        worker.join()
        self.assertEqual(len(errors), 1)
        self.assertIn("main thread", errors[0])
        self.assertEqual(self.snapshot(), before)

    def test_reinstall_uses_preserved_original_backups(self):
        self.installer.install()
        self.installer.restore()
        self.installer.install()
        self.assertTrue(self.installer.is_installed())
        self.installer.restore()
        self.assert_original_boot()
        self.assertEqual((self.paths.state / "initrd.original").read_bytes(), ORIGINAL_INITRD)


if __name__ == "__main__":
    unittest.main()
