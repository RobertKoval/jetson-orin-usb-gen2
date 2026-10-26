# Disk installation and boot persistence

The executable stages the exact `orin-gen2-probe-3` driver for kernel
`5.15.185-tegra`. Installation changes the module on disk and the initramfs;
it does not replace the loaded module or reboot. A clean boot remains necessary
before the loaded patched driver can be used. Installation alone does not enable
Gen2 at boot.

The installer requires the tested stock module hash, the pinned patched source,
a matching module version and full vermagic, regular module/initrd files, and
extlinux entries that use exactly `/boot/initrd`. Other boot layouts, signed boot
artifacts and A/B deployments need their established installation and recovery
process. The executable performs native platform checks before invoking the
installer. Test-only path injection exists in the Python API, not as CLI flags.

## Files and retained originals

- `/var/lib/orin-usb-gen2/5.15.185-tegra/` is mode `0700`. Its original module and
  initrd backups and transaction manifest are mode `0600`. Original backups are
  never overwritten.
- `/usr/local/libexec/orin-usb-gen2/` contains the executable, its Python modules,
  and `build/orin-usb-speed`. The launcher is
  `/usr/local/bin/orin-usb-gen2`.
- `/etc/systemd/system/orin-usb-gen2.service` is a oneshot service ordered after
  and requiring `nv-l4t-usb-device-mode.service`. It invokes `boot-enable` and has
  no stop hook. Enabling or disabling the unit does not stop the vendor gadget
  service.

The separate permanent setting enables or disables that unit. Its runtime
counterpart still performs the guarded Gen1/Gen2 switch. Restoring original boot
files disables the unit and retains the installed application and originals so
that the running command and recovery tools remain available.

Unknown existing application/unit/launcher paths, redirected symlinks, unsafe
managed ownership/permissions, corrupt backups, changed installed files, and
unreviewed replacement builds are rejected. Repeating installation with the
same bundle is a no-op after verification. Removing the downloaded repository
does not remove the installed executable.

## Transaction and recovery boundary

Original files and their hashes are persisted before any boot artifact changes.
Module copies, restored initrds, application files and manifests use temporary
files followed by atomic replacement and filesystem sync. The installer calls
`depmod -a 5.15.185-tegra` and NVIDIA's `nv-update-initrd`, then extracts the
result using `unmkinitramfs`. Every discovered `tegra-xudc` module must match the
staged binary. Plain, gzip and xz module payloads are supported; other compression
formats are rejected. The result reports the number of verified copies, including
zero when this module is absent from the initramfs.

`nv-update-initrd` controls its own writes to the live initrd. The installer
cannot make that vendor operation atomic or guarantee survival of power loss
mid-generation. After successful generation and inspection, the final initrd is
copied atomically and synced. Backups and transaction phases support recovery
after an interrupted generation; an incomplete transaction is never reported as
installed. An interrupted transaction may restore an incomplete or missing
initrd from its verified original backup.

A command failure triggers restoration of both original boot files and their
module dependency index. A failed rollback is reported separately with an
instruction not to reboot. Run the executable's stock-restore operation to retry
recovery after resolving its reported cause. Changed files outside a recorded
incomplete transaction are not overwritten automatically. If interruption occurs
while making initial backups but before the first manifest, no boot artifact has
been changed; the installer refuses to guess ownership of those orphan backups.

SIGINT and SIGTERM are ignored during restoration and recovery, including by the
recovery subprocesses, so repeated cancellation cannot stop between restoring the
module and initrd. The caller's signal handlers and mask are restored afterward.
Installation catches direct Python `KeyboardInterrupt` as well as ordinary
command failures and attempts recovery. Recovery attempts the remaining files
even if one operation raises; an incomplete operation is recorded and reported
as a rollback failure. Mutating Python API calls require the main thread so this
signal protection can be installed before any writes. SIGKILL and power loss
still require recovery from the retained transaction and backups.

This protects the files managed by this installer. It does not establish which
boot entry firmware will select, validate Secure Boot signatures, supply a
recovery image, or prove that the next boot succeeds. After a clean boot, verify
the loaded module version and actual configured USB link. The automated tests
exercise temporary files and simulated vendor/systemd commands; real device installation and a live service start were exercised for v0.2.0;
a real reboot and cold-boot enable sequence remain a separate, explicitly deferred
hardware validation step. See VALIDATION.md for the measured boundary.
