# Jetson AGX Orin USB device mode at 10 Gb/s

An experimental **USB 3.2 Gen 2x1 device-mode patch** for the NVIDIA Jetson AGX Orin Developer Kit, tested with **Jetson Linux R36.5 / kernel 5.15.185-tegra**.

The host negotiated **10000M**, the gadget reached **configured / super-speed-plus**, and 1.25 GiB TCP transfers passed end-to-end SHA-256 verification in both directions. This is working data transfer, not just a changed speed label.

**Experimental, one-board result. Not NVIDIA-supported or a general Orin-family driver.** The first stock-driver hot replacement caused a kernel panic because of a stale PHY notifier. The published patch includes teardown changes; the installation guide uses a clean boot instead of an address-dependent recovery helper. Read [installation and rollback](docs/INSTALL.md) before changing a kernel module.

## Results

CPU-buffer TCP transfer of **1.25 GiB**, the same host port and patched driver, switching only link generation. NCM route/interface counters were checked. Shared machines, one final controlled transfer per direction/rate; these are observations, not peak-throughput guarantees.

| Direction | Gen1 (5 Gb/s) | Gen2 (10 Gb/s) |
|---|---:|---:|
| Host → Orin | 3.728 s / 343 MiB/s | **3.379 s / 379 MiB/s** |
| Orin → host | 3.575 s / 358 MiB/s | **2.324 s / 551 MiB/s** |

With both directions active, the Gen2 transfers took 4.704 s and 2.382 s. RNDIS echo of a 20 KiB message measured **0.410 ms median RTT** (100 samples). These CPU-buffer results do not include CUDA staging or model inference. See [methodology and limitations](docs/RESULTS.md) and [sanitized measurements](results/measurements.json).

## What changes

Two independent layers need attention:

1. **PADCTL device capability:** the AGX Orin devkit J40 USB3 port has a separate device-speed field, `PORT1_DEV_SPEED_SUPPORT`, at `0x0352002c`, bits 21:20. The tested change is `0 → 1` (Gen1 → Gen2). The lower host-speed fields are different.
2. **XUDC software:** the stock driver does not handle negotiated `PORTSC.PS=5`. The patch recognizes SSP, handles its endpoint/control semantics, and advertises Gen2x1 capability for T234. It also addresses the notifier lifetime defect encountered during development.

The original T194 `port_speed_quirk` is already off on T234. Disabling it is not this unlock. The official TRM contains conflicting high-level descriptions; the register definition and actual negotiated link are the basis for this experiment. [Technical findings](docs/DESIGN.md).

## Scope

| Component | Tested scope |
|---|---|
| Board / connector | AGX Orin Developer Kit 64 GB, **J40 beside the 40-pin header** |
| BSP / kernel | R36.5, `5.15.185-tegra`, native aarch64 build |
| Host connection | Linux x86-64, verified Gen2 Type-A port, A↔C cable |
| Functions | NCM bulk data and RNDIS echo; default L4T composite enumeration |
| Not validated | NX/Nano/custom carriers, other BSPs, Windows/macOS, isochronous transfers, suspend/resume, long-duration stress, Secure Boot installation |

A root hub showing `10000M` does **not** establish that every physical downstream port supports 10 Gb/s. Check the actual device link. A C↔C detection problem is separate from the device-speed cap.

## Download and use the executable

Download the versioned source bundle (Python standard library + a small native C helper; no pip dependencies):

```sh
curl -fLO https://github.com/RobertKoval/jetson-orin-usb-gen2/releases/download/v0.2.0/jetson-orin-usb-gen2-v0.2.0.tar.gz
curl -fLO https://github.com/RobertKoval/jetson-orin-usb-gen2/releases/download/v0.2.0/jetson-orin-usb-gen2-v0.2.0.tar.gz.sha256
sha256sum -c jetson-orin-usb-gen2-v0.2.0.tar.gz.sha256
tar -xzf jetson-orin-usb-gen2-v0.2.0.tar.gz
cd jetson-orin-usb-gen2-v0.2.0
./orin-usb-gen2 --help
```

With the patched driver already loaded:

```sh
./orin-usb-gen2 build-helper
sudo ./orin-usb-gen2 status
sudo ./orin-usb-gen2 enable    # Gen2 for this session
sudo ./orin-usb-gen2 disable   # Gen1 for this session
```

For an initial driver installation and boot persistence:

```sh
./orin-usb-gen2 build /path/to/Linux_for_Tegra/source/kernel/kernel-jammy-src
sudo ./orin-usb-gen2 install --dry-run
sudo ./orin-usb-gen2 install
# Read the result and reboot when other work can stop; there is no automatic reboot.
# After the patched driver is loaded:
sudo orin-usb-gen2 enable --permanent
sudo orin-usb-gen2 disable --permanent
```

`--permanent` sets the runtime generation and the next-boot policy. Disabling permanent Gen2 leaves the patched driver installed, operating at Gen1. `restore-driver` separately restores the original module/initrd on disk for the next boot. **No command hot-unloads the stock driver.**

The installer preserves original module/initrd backups, checks the generated initramfs, and deploys a root-owned executable and a systemd oneshot. The boot service can arm Gen2 with no USB host attached; that is reported as *armed*, not as a verified connected10G link. Runtime switching requires a configured connection and verifies both register readback and negotiated device state. Concurrent mutations are rejected; failures attempt and verify rollback.

Read [installation and rollback](docs/INSTALL.md), [persistence/recovery guarantees](docs/PERSISTENCE.md), and [current validation](docs/VALIDATION.md). A successful setting is not a throughput or data-integrity test; confirm host speed and run the probes.

## Files

- `patches/`: exact tested XUDC patch; no kernel tree or binary module.
- `orin-usb-gen2`: executable CLI for runtime mode, diagnostics, installation and boot policy.
- `scripts/build.sh`: source-identity checks and native build.
- `tools/`: guarded runtime device-speed control; no firmware/fuse writes.
- `bench/`: bounded-memory bulk and SHA-256 transfer probes, no baked-in host addresses.
- `docs/`: installation, rollback, findings, incident explanation and validation limits.

Boot-time Gen2 is opt-in through `enable --permanent`; installation alone does not enable it. Kernel upgrades, other boards, Secure Boot integration and recovery images remain outside the supported recipe. Private machine/crash artifacts and address-dependent recovery helpers are not distributed.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Reports from other boards/releases are useful, but do not remove the identity checks just to make an unsupported system pass. Report the actual negotiated host speed and data verification separately from throughput.

## License

GPL-2.0-or-later. The patch retains the upstream NVIDIA/Google source provenance; see [NOTICE](NOTICE) and [LICENSE](LICENSE). NVIDIA manuals and firmware are linked, not redistributed.
