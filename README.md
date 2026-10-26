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

## Getting started

1. Obtain the **R36.5 BSP sources** from [NVIDIA](https://developer.nvidia.com/embedded/jetson-linux-r365) and matching installed kernel headers.
2. Build without changing the running machine:

   ```sh
   git clone https://github.com/RobertKoval/jetson-orin-usb-gen2.git
   cd jetson-orin-usb-gen2
   ./scripts/build.sh /path/to/Linux_for_Tegra/source/kernel/kernel-jammy-src
   ```

   The script checks the exact input/output source hashes, applies the patch with no fuzz, and builds the module and register tool. It does not install or load anything.
3. Follow the [clean-boot installation recipe](docs/INSTALL.md). **Do not `rmmod` the unmodified stock driver to follow this README.**
4. Once the patched driver is loaded and the default gadget works at Gen1:

   ```sh
   sudo python3 tools/link-speed.py status
   sudo python3 tools/link-speed.py gen2
   ```

   This is a transient register change, not boot persistence. The tool accepts only the tested board/driver, disconnects the gadget before writing, and attempts to restore the previous generation if configuration fails.
5. Confirm `10000M` on the host, `configured` / `super-speed-plus` on Orin, then run the [data-integrity test](docs/INSTALL.md#verify-real-data).

The exact published driver source matches the successful experiment. The guarded public wrapper is a new packaging layer: its status and rejection paths are checked, but the complete public clean-boot recipe has **not** been exercised end to end. The original successful experiment used a guarded, build-specific live migration; those private-offset helpers are deliberately not distributed here.

## Files

- `patches/`: exact tested XUDC patch; no kernel tree or binary module.
- `scripts/build.sh`: source-identity checks and native build.
- `tools/`: guarded runtime device-speed control; no firmware/fuse writes.
- `bench/`: bounded-memory bulk and SHA-256 transfer probes, no baked-in host addresses.
- `docs/`: installation, rollback, findings, incident explanation and validation limits.

No automatic boot-time Gen2 enablement or unattended installer is provided. Original benchmark scripts and private machine/crash artifacts are not required to use the patch.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Reports from other boards/releases are useful, but do not remove the identity checks just to make an unsupported system pass. Report the actual negotiated host speed and data verification separately from throughput.

## License

GPL-2.0-or-later. The patch retains the upstream NVIDIA/Google source provenance; see [NOTICE](NOTICE) and [LICENSE](LICENSE). NVIDIA manuals and firmware are linked, not redistributed.
