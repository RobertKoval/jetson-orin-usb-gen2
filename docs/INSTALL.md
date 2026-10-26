# Executable workflow: runtime and permanent mode

Supported target: standard **AGX Orin Developer Kit / J40 / L4T R36.5 / aarch64 / kernel5.15.185-tegra**. Keep management on Ethernet, Wi-Fi or separate serial: mode switches disconnect the gadget USB link. Other boards, kernels, custom carriers and signed/A/B boot layouts require separate validation.

## Commands

| Command | Effect |
|---|---|
| `status` | Device role, binding, actual speed, loaded driver; with sudo, PADCTL readback and installed state |
| `doctor` | Read-only readiness diagnostics |
| `build-helper` | Compile only the C helper, no root/device changes |
| `build SOURCE_DIR` | Apply the pinned patch and build the module/helper natively |
| `enable` / `disable` | Runtime Gen2 / Gen1; configured link required |
| `install --dry-run` | Check disk-install prerequisites and original initramfs without modifying boot artifacts |
| `install` | Back up originals, stage driver/initramfs, install CLI/unit; no reboot or hot unload |
| `enable --permanent` | Runtime Gen2 and enable the next-boot oneshot |
| `disable --permanent` | Runtime Gen1 and disable the next-boot oneshot; patched driver stays installed |
| `restore-driver --dry-run` | Inspect a recorded restoration without changing boot files |
| `restore-driver` | Restore original module/initramfs on disk and disable boot Gen2; reboot needed to change loaded driver |

`boot-enable` is the internal oneshot entry point. It can select Gen2 without a connected host; JSON `armed=true, link_verified=false` means that only the desired capability/binding is ready. Normal runtime success requires configured gadget state and the requested speed. Gen2 capability alone does not prove a Gen2 connection.

## 1. Obtain matching sources and build

Get **Driver Package (BSP) Sources** from [NVIDIA R36.5](https://developer.nvidia.com/embedded/jetson-linux-r365). Extract `public_sources.tbz2`, then its `Linux_for_Tegra/source/kernel_src.tbz2`. Use matching configured NVIDIA kernel headers, a C compiler, make, patch, sha256sum and Python3.10+.

```sh
uname -m
uname -r
cat /etc/nv_tegra_release
ls /lib/modules/$(uname -r)/build/Module.symvers
./orin-usb-gen2 build /path/to/Linux_for_Tegra/source/kernel/kernel-jammy-src
```

Source hashes enforced by the build:
- original: `fec05c0060201cce2e27a142bccb629df1b7398479f9d21bed2b053b2535448f`;
- patched: `3ff1616f8924e006d724fc4568025f9f1a11e49ddb801a3a7e56694efcc20f00`.

Output is `build/tegra-xudc.ko`, version `orin-gen2-probe-3`, and `build/orin-usb-speed`. The supplied kernel tree is not edited. Helper compilation publishes atomically after success, preserving the previous executable if compilation fails.

## 2. Choose runtime-only or disk installation

If this exact patched driver is already loaded, `build-helper` is enough:

```sh
./orin-usb-gen2 build-helper
sudo ./orin-usb-gen2 doctor
sudo ./orin-usb-gen2 disable
sudo ./orin-usb-gen2 enable
```

The stock driver cannot be made hot-unload-safe by replacing its disk file. **Do not use `rmmod` on the unmodified stock XUDC to install this patch.** Its stale notifier caused a real kernel panic during the original research.

For initial installation, inspect first:

```sh
sudo ./orin-usb-gen2 install --dry-run
sudo ./orin-usb-gen2 install
```

This requires the exact supported original module hash, standard extlinux entries using `/boot/initrd`, matching module version/full vermagic, native vendor tools, and no unknown existing managed paths. It refuses unsupported layouts, symlinks, backup corruption and drift.

Originals and a recovery manifest are retained under `/var/lib/orin-usb-gen2/5.15.185-tegra/`. Installation copies the driver on disk, runs `depmod` and NVIDIA's `nv-update-initrd`, extracts the resulting initramfs and verifies any XUDC copies. It installs the command in `/usr/local/bin/orin-usb-gen2` and a oneshot unit, **disabled by default**. The original source patch does not change.

Installation reports `reboot_required=true`; it never reboots or replaces a loaded module. Arrange a maintenance window and an independent recovery route, then reboot yourself. NVIDIA documents native `nv-update-initrd` in the [R36.5 kernel customization guide](https://docs.nvidia.com/jetson/archives/r36.5/DeveloperGuide/SD/Kernel/KernelCustomization.html).

After boot:

```sh
cat /sys/module/tegra_xudc/version  # orin-gen2-probe-3
sudo orin-usb-gen2 status
```

A successful disk-install check cannot prove which firmware boot entry was selected or that a future reboot will succeed. See [validation](VALIDATION.md) for the actual device-test boundary.

## 3. Set the boot policy

```sh
sudo orin-usb-gen2 enable --permanent
sudo orin-usb-gen2 status
```

This verifies the installed artifacts, selects Gen2, and enables `orin-usb-gen2.service` after `nv-l4t-usb-device-mode.service`. When no host is connected, boot mode may arm the setting for the next attachment; do not interpret that as a configured link.

```sh
sudo orin-usb-gen2 disable --permanent
```

This selects Gen1 and disables the Gen2 boot policy. A plain `disable` changes only the current session: if permanent Gen2 is still enabled, the next boot selects Gen2 again. `status` reports these separately. Boot-policy failures attempt to restore both the previous policy and runtime mode.

## 4. Verify the link and select NCM

On Orin:

```sh
cat /sys/class/udc/3550000.usb/state
cat /sys/class/udc/3550000.usb/current_speed
```

For an active Gen2 link expect `configured` / `super-speed-plus`. On the host, `lsusb -t` must show **10000M on the NVIDIA device**, not only its root hub.

The default gadget exposes RNDIS and NCM. For prefill identify the NCM interface with `ethtool -i <interface>` (`cdc_ncm`). Its default Orin address is `192.168.55.1`. Ensure `ip route get 192.168.55.1` selects NCM and that Orin's neighbor table maps the host address to its NCM MAC. RNDIS must not also own the same host IPv4 address.

Example host NetworkManager profile (replace `HOST_IF` with your actual interface; do not create a duplicate existing profile):

```sh
HOST_IF=your_ncm_interface
HOST_MAC=$(cat "/sys/class/net/$HOST_IF/address")
sudo nmcli connection add type ethernet con-name orin-usb-ncm \
  ifname "$HOST_IF" 802-3-ethernet.mac-address "$HOST_MAC" \
  ipv4.method manual ipv4.addresses 192.168.55.100/24 \
  ipv4.never-default yes ipv6.method link-local ipv6.never-default yes \
  connection.autoconnect-priority 100
sudo nmcli connection up orin-usb-ncm
```

Disconnect a competing RNDIS host profile or give it a separate subnet. Speed switching can recreate host interfaces; persistent host profiles matter. This CLI does not choose host IPs, set RPS CPU masks, disable UMS, or claim to preserve host-only runtime tuning across re-enumeration.

## 5. Verify data

Clone/extract this repository on both endpoints. For the standard NCM addresses:

```sh
# Orin terminal
python3 bench/integrity.py server 192.168.55.1
# Host terminal
python3 bench/integrity.py client 192.168.55.1
```

Expect `verified=true` for1,342,177,280 bytes. Reverse server/client roles using the host USB address. Use `bench/transfer.py` for a separate throughput measurement without hashing. Ports45681/45680 must be reachable on the selected USB subnet. Verify route and interface byte counters independently; a TCP connection alone does not prove the chosen path.

## 6. Recover or restore the original driver

Runtime failures attempt to restore the previous capability and binding, then verify the **previous actual negotiated speed**. That distinction matters when a Gen2-capable device was connected to a Gen1 host. A failed register restoration still attempts rebind. SIGINT/SIGTERM cannot interrupt the rollback itself; SIGKILL, power loss and kernel failure remain outside that guarantee.

Exit codes:0 success;2 CLI/platform/root/runtime precondition failure;3 concurrent operation;4 runtime switch failed but rollback verified;5 runtime or installer rollback failed;130/143 runtime interruption with verified rollback. Installer preflight rejection, installer cancellation after recovery, and other operational failures use1. Detailed failure JSON distinguishes restored from unverified state; inspect the message rather than assuming any nonzero result changed nothing.

If installation was interrupted or failed:

```sh
sudo ./orin-usb-gen2 restore-driver --dry-run
sudo ./orin-usb-gen2 restore-driver
```

Or use the installed `orin-usb-gen2` if available. The manifest and original backups remain available even if the download directory is removed. Restoration disables permanent Gen2 and restores original module/initramfs on disk; it does not unload the active driver. Reboot when appropriate to load the original.

If rollback is reported incomplete, **do not reboot until boot artifacts are recovered**. Keep the backups and resolve the reported cause. The vendor initrd generator owns its own writes: a power failure mid-generation can still require recovery from the saved originals. Unknown changes outside a recorded interrupted transaction are not overwritten automatically. Details: [PERSISTENCE.md](PERSISTENCE.md).

Removing `/var/lib/orin-usb-gen2` deletes your recorded recovery information. Do not delete it merely to bypass a drift or compatibility rejection. General kernel upgrades, Secure Boot signing and custom/A/B boot integration are separate work.
