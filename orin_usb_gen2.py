# SPDX-License-Identifier: GPL-2.0-or-later
"""Runtime controller for the pinned AGX Orin USB Gen2 experiment."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import IntEnum
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import stat
import subprocess
import sys
import tempfile
import time

APP_VERSION = '0.2.0'
DRIVER_VERSION = 'orin-gen2-probe-3'
KERNEL = '5.15.185-tegra'
CONTROLLER = '3550000.usb'
ROOT = Path(__file__).resolve().parent
LOCK = Path('/run/orin-usb-gen2/lock')

class Generation(IntEnum):
    GEN1 = 1
    GEN2 = 2
    @property
    def speed(self):
        return {self.GEN1: 'super-speed', self.GEN2: 'super-speed-plus'}[self]
    @property
    def command(self):
        return 'gen' + str(int(self))

@dataclass(frozen=True)
class State:
    binding: str
    function: str
    role: str
    state: str
    speed: str
    driver_version: str | None
    def configured(self, generation):
        return self.binding == CONTROLLER and self.state == 'configured' and self.speed == generation.speed

class PreconditionError(RuntimeError): pass
class BusyError(RuntimeError): pass
class Cancelled(RuntimeError):
    def __init__(self, signum):
        self.signum = signum
        super().__init__('Interrupted by signal ' + str(signum))
class SwitchError(RuntimeError):
    def __init__(self, cause, rollback_error, state):
        self.cause, self.rollback_error, self.state = cause, rollback_error, state
        self.rollback_verified = rollback_error is None
        detail = 'Rollback verified' if self.rollback_verified else 'Rollback FAILED: ' + str(rollback_error)
        super().__init__(str(cause) + '; ' + detail)

@contextmanager
def exclusive_lock(path=LOCK):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent = path.parent.lstat()
    if not stat.S_ISDIR(parent.st_mode) or parent.st_uid != os.geteuid() or parent.st_mode & 0o022:
        raise PreconditionError('Unsafe lock directory: ' + str(path.parent))
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        meta = os.fstat(fd)
        if not stat.S_ISREG(meta.st_mode) or meta.st_uid != os.geteuid() or meta.st_nlink != 1:
            raise PreconditionError('Unsafe lock file')
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc: raise BusyError('Another orin-usb-gen2 operation is running') from exc
        yield
    finally:
        os.close(fd)

@contextmanager
def interrupt_handling(ignore=False):
    def handle(signum, frame): raise Cancelled(signum)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous: signal.signal(sig, signal.SIG_IGN if ignore else handle)
        yield
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)

class Hardware:
    def __init__(self, bundle=ROOT):
        self.tool = Path(bundle) / 'build/orin-usb-speed'
        self.udc = Path('/sys/kernel/config/usb_gadget/l4t/UDC')
        self.state_root = Path('/sys/class/udc') / CONTROLLER
    @staticmethod
    def read(path, optional=False):
        try: return Path(path).read_text().strip().rstrip('\0')
        except FileNotFoundError:
            if optional: return None
            raise PreconditionError('Missing device path: ' + str(path))
    def validate_platform(self):
        if sys.platform != 'linux' or os.uname().machine != 'aarch64' or os.uname().release != KERNEL:
            raise PreconditionError('Supported target: native aarch64 / ' + KERNEL)
        if self.read('/proc/device-tree/model') != 'NVIDIA Jetson AGX Orin Developer Kit':
            raise PreconditionError('Unsupported board; no changes made')
        if self.read('/proc/device-tree/bus@0/padctl@3520000/compatible') != 'nvidia,tegra234-xusb-padctl':
            raise PreconditionError('Unexpected PADCTL mapping')
    def snapshot(self):
        return State(self.read(self.udc), self.read(self.state_root/'function'),
                     self.read('/sys/class/usb_role/usb2-0-role-switch/role'),
                     self.read(self.state_root/'state'), self.read(self.state_root/'current_speed'),
                     self.read('/sys/module/tegra_xudc/version', optional=True))
    def helper(self, mode):
        if not self.tool.is_file() or not os.access(self.tool, os.X_OK):
            raise PreconditionError('Build the helper first: ./orin-usb-gen2 build-helper')
        run = subprocess.run([str(self.tool), mode], capture_output=True, text=True, timeout=5)
        if run.returncode:
            raise RuntimeError(run.stderr.strip() or 'Register helper failed')
        value = json.loads(run.stdout)
        Generation(value['generation'])  # reject an unknown/reserved helper result
        return value
    def generation(self): return Generation(self.helper('status')['generation'])
    def set_generation(self, generation):
        value = self.helper(generation.command)
        if Generation(value['generation']) != generation:
            raise RuntimeError('Register readback does not match requested generation')
    def read_binding(self): return self.read(self.udc)
    def unbind(self): self.udc.write_text('\n')
    def bind(self, controller): self.udc.write_text(controller)

class Clock:
    monotonic = staticmethod(time.monotonic)
    sleep = staticmethod(time.sleep)

def wait_speed(hw, speed, timeout=10.0, clock=Clock):
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('timeout must be finite and positive')
    deadline = clock.monotonic() + timeout
    while True:
        now = clock.monotonic()
        if now > deadline:
            raise TimeoutError('Timed out waiting for ' + speed)
        state = hw.snapshot()
        if state.binding == CONTROLLER and state.state == 'configured' and state.speed == speed: return state
        remaining = deadline - clock.monotonic()
        if remaining <= 0: raise TimeoutError('Timed out waiting for ' + speed)
        clock.sleep(min(0.1, remaining))

def wait_configured(hw, generation, timeout=10.0, clock=Clock):
    return wait_speed(hw, generation.speed, timeout, clock)

def restore_runtime(hw, previous, before, timeout=10.0, clock=Clock):
    errors = []
    try:
        hw.unbind()
        clock.sleep(1.0)  # preserve the tested USB disconnect/retrain interval
        hw.set_generation(previous)
    except Exception as exc:
        errors.append(str(exc))
    finally:
        # A register restoration failure must not prevent an attempted rebind.
        try:
            try: binding = hw.read_binding()
            except Exception: binding = None  # still attempt rebind; final verification remains mandatory
            if binding != before.binding: hw.bind(before.binding)
        except Exception as exc: errors.append('Rebind: ' + str(exc))
    if errors: raise RuntimeError('; '.join(errors))
    if hw.generation() != previous: raise RuntimeError('Restored register did not verify')
    if before.state == 'configured': return wait_speed(hw, before.speed, timeout, clock)
    state = hw.snapshot()
    if state.binding != before.binding: raise RuntimeError('Restored binding did not verify')
    return state

def switch(hw, generation, *, allow_disconnected=False, timeout=10.0, clock=Clock):
    """Caller holds the process lock. Does not unload, install or reboot."""
    generation = Generation(generation)
    if not math.isfinite(timeout) or timeout <= 0: raise ValueError('timeout must be finite and positive')
    hw.validate_platform()
    before = hw.snapshot()
    if before.binding != CONTROLLER: raise PreconditionError('Expected bound default L4T gadget')
    if before.role not in ('device', 'none') or (before.role == 'none' and not allow_disconnected):
        raise PreconditionError('Expected device role')
    previous = hw.generation()
    if previous == generation and before.configured(generation):
        return {'changed': False, 'generation': int(generation), 'link_verified': True, 'state': asdict(before)}
    if before.driver_version != DRIVER_VERSION:
        raise PreconditionError('Patched driver required; install it for a clean boot, never hot-unload stock XUDC')
    if before.state != 'configured' and not allow_disconnected:
        raise PreconditionError('No configured link; runtime switch requires a connected device')
    try:
        hw.unbind()
        clock.sleep(1.0)
        hw.set_generation(generation)
        hw.bind(before.binding)
        if allow_disconnected and before.state != 'configured':
            after = hw.snapshot()
            if after.binding != CONTROLLER or hw.generation() != generation:
                raise RuntimeError('Boot setting did not verify')
            return {'changed': True, 'generation': int(generation), 'link_verified': after.configured(generation), 'armed': not after.configured(generation), 'state': asdict(after)}
        after = wait_configured(hw, generation, timeout, clock)
        if hw.generation() != generation: raise RuntimeError('Register changed while verifying link')
        return {'changed': True, 'generation': int(generation), 'link_verified': True, 'state': asdict(after)}
    except BaseException as cause:
        rollback_error = None
        # Second SIGINT/SIGTERM must not interrupt the best-effort rollback.
        with interrupt_handling(ignore=True):
            try: restore_runtime(hw, previous, before, timeout, clock)
            except BaseException as exc: rollback_error = exc
        try: observed = asdict(hw.snapshot())
        except Exception as exc: observed = {'read_error': str(exc)}
        raise SwitchError(cause, rollback_error, observed) from cause

def persistent_switch(hw, installer, generation, *, timeout=10.0, clock=Clock):
    """Coordinate boot policy with the runtime transition under the same lock."""
    if generation == Generation.GEN2 and not installer.is_installed():
        raise PreconditionError('Run install before enabling permanent Gen2')
    before = hw.snapshot(); previous = hw.generation(); old_boot = installer.boot_enabled()
    result = switch(hw, generation, allow_disconnected=True, timeout=timeout, clock=clock)
    try:
        if old_boot != (generation == Generation.GEN2):
            installer.set_boot_enabled(generation == Generation.GEN2)
        if installer.boot_enabled() != (generation == Generation.GEN2):
            raise RuntimeError('Boot policy did not verify')
    except BaseException as cause:
        errors = []
        with interrupt_handling(ignore=True):
            try: installer.set_boot_enabled(old_boot)
            except Exception as exc: errors.append('Boot policy: ' + str(exc))
            try: restore_runtime(hw, previous, before, timeout, clock)
            except Exception as exc: errors.append('Runtime: ' + str(exc))
        error = RuntimeError('; '.join(errors)) if errors else None
        try: observed = asdict(hw.snapshot())
        except Exception as exc: observed = {'read_error': str(exc)}
        raise SwitchError(cause, error, observed) from cause
    return {**result, 'permanent': True, 'boot_generation': int(generation)}

def build_helper(bundle=ROOT):
    if sys.platform != 'linux' or os.uname().machine != 'aarch64':
        raise PreconditionError('Build the helper natively on aarch64 Linux')
    out = Path(bundle)/'build';out.mkdir(exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.orin-usb-speed-', dir=out)
    os.close(fd)
    candidate = Path(name)
    try:
        subprocess.run(['cc','-O2','-Wall','-Wextra','-Werror',str(Path(bundle)/'tools/padctl.c'),'-o',str(candidate)],check=True)
        candidate.chmod(0o755)
        candidate.replace(out/'orin-usb-speed')
    finally:
        candidate.unlink(missing_ok=True)
    return {'helper': str(out/'orin-usb-speed'), 'device_changed': False}

def report_status(hw, installer=None):
    result = {'cli_version': APP_VERSION}
    try: hw.validate_platform();result['platform_supported'] = True
    except Exception as exc: result.update(platform_supported=False, platform_error=str(exc));return result
    try: result.update(asdict(hw.snapshot()))
    except Exception as exc: result['state_error'] = str(exc)
    if os.geteuid() == 0:
        try: result['generation'] = int(hw.generation())
        except Exception as exc: result['register_error'] = str(exc)
    else: result['register_note'] = 'Use sudo for register readback'
    if installer:
        try: result['installed'] = installer.is_installed();result['boot_gen2_enabled'] = installer.boot_enabled()
        except Exception as exc: result['install_error'] = str(exc)
    return result

def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--version',action='version',version=APP_VERSION)
    sub=parser.add_subparsers(dest='command',required=True)
    for name in ('status','doctor','build-helper','boot-enable'):
        sub.add_parser(name)
    sub.add_parser('build').add_argument('source')
    for name in ('enable','disable'):
        sub.add_parser(name).add_argument('--permanent',action='store_true')
    for name in ('install','restore-driver'):
        sub.add_parser(name).add_argument('--dry-run',action='store_true')
    args=parser.parse_args(argv)
    from orin_install import Installer, InstallError, RollbackError
    hw=Hardware();installer=Installer(ROOT)
    try:
        if args.command in ('status','doctor'):
            result=report_status(hw,installer)
            if args.command=='doctor':
                result['ready_for_runtime_switch']=bool(result.get('platform_supported') and result.get('driver_version')==DRIVER_VERSION and result.get('binding')==CONTROLLER and result.get('state')=='configured' and hw.tool.is_file())
            print(json.dumps(result,indent=2));return 0 if result.get('platform_supported') else 2
        if args.command=='build-helper':result=build_helper()
        elif args.command=='build':
            subprocess.run([str(ROOT/'scripts/build.sh'),args.source],check=True);result={'built':True,'device_changed':False}
        else:
            if os.geteuid()!=0:raise PreconditionError('This operation requires sudo')
            hw.validate_platform()
            with exclusive_lock(),interrupt_handling():
                if args.command=='install':result=installer.install(dry_run=args.dry_run)
                elif args.command=='restore-driver':result=installer.restore(dry_run=args.dry_run)
                elif args.command=='boot-enable':
                    if not installer.is_installed(): raise PreconditionError('Boot service requires a verified installation')
                    result=switch(hw,Generation.GEN2,allow_disconnected=True)
                else:
                    gen=Generation.GEN2 if args.command=='enable' else Generation.GEN1
                    result=persistent_switch(hw,installer,gen) if args.permanent else switch(hw,gen)
        print(json.dumps(result,indent=2));return 0
    except SwitchError as exc:
        print(json.dumps({'error':str(exc),'rollback_verified':exc.rollback_verified,'state':exc.state},indent=2),file=sys.stderr)
        if isinstance(exc.cause,Cancelled) and exc.rollback_verified:return 128+exc.cause.signum
        return 4 if exc.rollback_verified else 5
    except RollbackError as exc:
        print(json.dumps({'error':str(exc),'rollback_verified':False,'recovery':'Retain backups and run restore-driver before rebooting'},indent=2),file=sys.stderr);return 5
    except InstallError as exc:
        print(json.dumps({'error':str(exc),'operation':args.command},indent=2),file=sys.stderr);return 1
    except BusyError as exc:print(str(exc),file=sys.stderr);return 3
    except PreconditionError as exc:print(str(exc),file=sys.stderr);return 2
    except Exception as exc:print(str(exc),file=sys.stderr);return 1
