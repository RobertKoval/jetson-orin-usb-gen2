# Build, install, verify and roll back

This guide targets the **AGX Orin Developer Kit with L4T R36.5 and kernel 5.15.185-tegra only**. Use J40 beside the 40-pin header. Keep management access on Ethernet/Wi-Fi or a separate serial console: the gadget USB link will disconnect during changes.

The patch and underlying Gen1/Gen2 switch were tested live. **The clean-boot disk-install procedure below is a documented integration recipe, not an end-to-end validated deployment.** It follows NVIDIA's native module/initramfs workflow. Have a known-good boot/recovery route before changing boot artifacts; kernel or module upgrades may replace this patch. Secure Boot/signing integration is outside the tested scope.

## 1. Obtain matching source and headers

Download **Driver Package (BSP) Sources** from the [R36.5 release page](https://developer.nvidia.com/embedded/jetson-linux-r365). Extract `public_sources.tbz2`, then its `Linux_for_Tegra/source/kernel_src.tbz2`. The kernel tree is `kernel/kernel-jammy-src`.

On Orin, install the native compiler/build tools and matching NVIDIA kernel headers through the BSP package setup. Verify:

```sh
uname -m                       # aarch64
uname -r                       # 5.15.185-tegra
cat /etc/nv_tegra_release       # R36, revision 5.0
ls /lib/modules/$(uname -r)/build/Module.symvers
```

The build requires `make`, a C compiler, `patch`, `sha256sum`, Python 3 and the matching configured headers. Do not substitute a generic Ubuntu kernel-header package.

## 2. Build without touching the running driver

From the repository root:

```sh
./scripts/build.sh /path/to/Linux_for_Tegra/source/kernel/kernel-jammy-src
modinfo build/tegra-xudc.ko | head
# version must be orin-gen2-probe-3; vermagic must match the running kernel
```

Input source SHA-256:
`fec05c0060201cce2e27a142bccb629df1b7398479f9d21bed2b053b2535448f`

Patched source SHA-256:
`3ff1616f8924e006d724fc4568025f9f1a11e49ddb801a3a7e56694efcc20f00`

The script never patches the supplied kernel tree. Generated files stay in ignored `build/`. Binary hashes may differ with build paths/toolchains; the source identity is pinned.

## 3. Install for a clean boot

**Do not hot-unload the stock `tegra_xudc` module.** Its teardown left a PHY notifier registered during the experiment. Replacing the module's code underneath that callback caused a kernel panic. Adding a teardown fix to the *new* module cannot repair the old instance already in memory.

Instead, stage the module on disk, update the initramfs, then reboot. The following commands assume the standard tested filesystem layout with a regular `/boot/initrd`. Inspect your boot entry first. If your system uses a different initrd, signed boot artifacts or an A/B deployment, use its established module-update/recovery process instead of these paths.

Run the staging block from the repository root in Bash:

```bash
set -euo pipefail
KREL=$(uname -r)
[[ "$KREL" == 5.15.185-tegra ]]
MODULE="/lib/modules/$KREL/kernel/drivers/usb/gadget/udc/tegra-xudc.ko"
BACKUP="/var/backups/orin-usb-gen2/$KREL"
test -f "$MODULE"
# Exact stock module used in the experiment; other builds need separate review.
[[ $(sha256sum "$MODULE" | cut -d' ' -f1) == dde8f6726941ea4f12c5e48020550db6b1903b2a816d365aeaeff5e27edf6415 ]]
test -f /boot/initrd
test ! -L /boot/initrd
test "$(modinfo -F version build/tegra-xudc.ko)" = orin-gen2-probe-3
sudo test ! -e "$BACKUP/tegra-xudc.ko"  # never overwrite the original backup
sudo install -d -m 700 "$BACKUP"
sudo cp -a "$MODULE" "$BACKUP/tegra-xudc.ko"
sudo cp -a /boot/initrd "$BACKUP/initrd"
sudo install -m 644 build/tegra-xudc.ko "$MODULE"
sudo depmod -a "$KREL"
sudo nv-update-initrd
```

NVIDIA documents `nv-update-initrd` for native target updates in its [R36.5 Kernel Customization guide](https://docs.nvidia.com/jetson/archives/r36.5/DeveloperGuide/SD/Kernel/KernelCustomization.html). If staging or initramfs generation fails, **restore the backup before rebooting** using the rollback block below.

Before reboot, inspect the module on disk and, if the module is included in your initramfs, the copy inside `/boot/initrd` (`lsinitramfs`/`unmkinitramfs`). Do not assume the file on disk is the one the next boot will use. Schedule a reboot when other jobs can stop:

```sh
sudo reboot
```

After boot, the decisive loaded-module check is:

```sh
cat /sys/module/tegra_xudc/version   # orin-gen2-probe-3
cat /sys/class/udc/3550000.usb/state
cat /sys/class/udc/3550000.usb/current_speed
```

First establish a working **Gen1** gadget before changing speed. If the loaded version is missing/wrong, stop; do not work around the guard by forcing a live replacement.

## 4. Switch the default gadget to Gen2

Connect a known Gen2 host port and suitable data cable. Run on Orin from the repository root:

```sh
sudo python3 tools/link-speed.py status
sudo python3 tools/link-speed.py gen2
```

The public tool is restricted to the standard AGX Orin devkit model, T234 PADCTL, the default `l4t` gadget, J40, and `orin-gen2-probe-3`. The C helper refuses writes while a gadget is bound. Do not invoke raw register writes to bypass a rejection.

Success must include:

- Orin `state`: `configured`.
- Orin `current_speed`: `super-speed-plus`.
- Host `lsusb -t`: **10000M for the NVIDIA device itself**, not just its root hub.
- Real data verification below.

A changed `maximum_speed` or a Gen2 log followed by descriptor errors is not a working USB network. A 5G cable/port can keep the actual link at Gen1. The switch tool times out and attempts to restore the previous generation when the requested mode does not configure. Abrupt process termination or device failure can still require restoring the binding or rebooting.

The register change is runtime-only. This repository does not enable Gen2 automatically at boot.

## 5. Select the NCM data path

The default L4T gadget exposes both RNDIS and NCM over one physical link. Its DHCP pool can assign the same host address to the wrong interface after re-enumeration. Identify NCM with `ethtool -i <interface>`: it must say `cdc_ncm`.

Example persistent host profile, after setting `HOST_IF` to your actual NCM interface:

```sh
HOST_IF=your_ncm_interface
HOST_MAC=$(cat "/sys/class/net/$HOST_IF/address")
sudo nmcli connection add type ethernet con-name orin-usb-ncm \
  ifname "$HOST_IF" 802-3-ethernet.mac-address "$HOST_MAC" \
  ipv4.method manual ipv4.addresses 192.168.55.100/24 \
  ipv4.never-default yes ipv6.method link-local ipv6.never-default yes \
  connection.autoconnect-priority 100
sudo nmcli connection up orin-usb-ncm
ip route get 192.168.55.1
```

Use an unused profile name or update an existing one deliberately. Prevent RNDIS from also owning `192.168.55.100`: disconnect its competing host profile or configure it in a separate subnet. For this experiment, a second subnet was used for RNDIS. Do not give two host interfaces the same IPv4 address.

On Orin, verify `ip neigh show dev l4tbr0` maps the host's NCM IP to `HOST_MAC`. Correct stale entries if necessary. On both ends inspect interface byte counters during the test; a successful TCP connection alone does not establish which interface carried it. RPS CPU masks and private network configuration are deliberately not hardcoded in this repository.

## Verify real data

`bench/transfer.py` and `bench/integrity.py` each transfer exactly 1.25 GiB using bounded memory. Start the server on one endpoint and the client on the other; pass the chosen local/server USB IP. SSH may launch a process, but do not tunnel the data through SSH.

Example, with the **server running on Orin**:

```sh
# Orin terminal:
python3 bench/integrity.py server 192.168.55.1
# Host terminal:
python3 bench/integrity.py client 192.168.55.1
```

The client must print `verified: true`, the exact byte count, and a matching digest. Reverse the roles and use the host USB address to check the other direction. The integrity probe uses TCP port45681; the throughput probe uses45680. Both require those ports to be available and reachable on the selected USB subnet.

For throughput, repeat with `bench/transfer.py`. Hashing is excluded from that probe; integrity-test timing is not directly comparable. Run sequential directions first; only then run a separate pair concurrently for duplex. Watch interface counters, USB resets, and kernel logs. Packet drops and TCP recovery can coexist with a correct end-to-end hash.

## Rollback

### Return to Gen1 without replacing the patched driver

```sh
sudo python3 tools/link-speed.py gen1
```

Confirm a configured gadget and **5000M on the host**. This was tested in the experiment. A reboot also resets the runtime device-speed field, but if you installed the module on disk, reboot alone does **not** restore the original module.

### Restore the original module and initramfs on disk

For the exact staging recipe above:

```bash
set -euo pipefail
KREL=5.15.185-tegra
BACKUP="/var/backups/orin-usb-gen2/$KREL"
MODULE="/lib/modules/$KREL/kernel/drivers/usb/gadget/udc/tegra-xudc.ko"
sudo test -f "$BACKUP/tegra-xudc.ko"
sudo test -f "$BACKUP/initrd"
sudo cp -a "$BACKUP/tegra-xudc.ko" "$MODULE"
sudo depmod -a "$KREL"
sudo cp -a "$BACKUP/initrd" /boot/initrd
```

Then reboot through your planned maintenance/recovery route. If the system is not reachable, perform restoration using your known-good boot entry, serial/recovery setup or mounted root filesystem. This repository does not supply a recovery image. Remove the optional host NetworkManager profile with `sudo nmcli connection delete orin-usb-ncm` if no longer wanted.

## What was actually validated

- Exact driver source applied and built for the tested kernel.
- Gen1/Gen2 runtime switching, host negotiation, configured gadget, NCM transfers, RNDIS echo, SHA-256 in both directions, and one vendor gadget-service recreation.
- The experiment used a guarded one-off live migration, **not** the disk-install/reboot recipe above. Its private-offset migration/recovery modules are intentionally excluded from the public workflow.
- No new boot-time installation or live mode switch is performed merely by cloning/building this repository.
