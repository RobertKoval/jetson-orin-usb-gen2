# Findings behind the patch

## Role, PHY capability and controller software are separate

J40 shares physical USB lanes between the XUSB host controller and XUDC device controller. The Cypress Type-C controller and Linux role switch select host/device operation. Device role already supports traffic in both directions; it is not a one-way transmit/receive choice.

The first source inspection found `gadget.max_speed = USB_SPEED_SUPER`, no SSP speed-setting callback, and an inactive T194 `port_speed_quirk` on T234. That was insufficient to rule out Gen2 hardware. Further investigation found **separate host and device fields** in PADCTL:

| Standard AGX Orin devkit J40 / USB3 port1 | Register | Field |
|---|---|---|
| Host speed support | `0x0352002c` | bits5:4 |
| Device speed support | `0x0352002c` | **bits21:20** |

The latter is documented as0=Gen1,1=Gen2 in the [Orin TRM v1.2p](https://developer.nvidia.com/downloads/orin-series-soc-technical-reference-manual/), p7147. The same manual defines device `PORTSC.PS=5` as SSP on p7094. Its HSIO overview on p7013 mentions Gen1/Gen2 device support, while the USB overview on p7020 describes Gen1 only. The manual is linked rather than redistributed.

Changing `SS_PORT_CFG` from `0x00001111` to `0x00101111` on the tested board produced physical Gen2 negotiation when attached to a real Gen2 host port. The host logged `SuperSpeed Plus Gen 2x1`, and device `PORTSC.PS` became5. Stock enumeration then failed: the driver interpreted5 as UNKNOWN and selected the wrong EP0 packet size.

## SSP handling

The published patch is the exact successful experiment, including its diagnostic log and module version `orin-gen2-probe-3`:

- Recognize `PORTSC.PS=5` and set `USB_SPEED_SUPER_PLUS` / `USB_SSP_GEN_2x1`.
- Advertise SSP maximum capability for T234.
- Apply SuperSpeed endpoint companion, burst, control/status, wake and resume handling to SSP as well.
- Use512-byte EP0 packets for both SS and SSP.
- Keep USB2 branches unchanged and clear the remembered SSP rate on a new connection.

The patch does not program PADCTL automatically. The separate runtime tool performs that step only after the patched module is loaded and the gadget is unbound. There is no firmware replacement, clock doubling or fuse programming.

The patch's comparison changes touch isochronous-related code inherited from the stock driver, but **general SSP isochronous support is not validated**. Only the documented NCM/RNDIS workload is claimed.

## The hot-unload incident

An early experiment hot-unloaded the stock module. Its probe registers `xudc->vbus_nb` on a PHY notifier chain; its remove path does not unregister it before freeing the private object/module text. A subsequent role notification reached the old callback address. The crash record showed `atomic_notifier_call_chain` calling an address that, after module-text reuse, resolved inside `tegra_xudc_free_eps`. The board required a reboot.

The patch unregisters notifier producers before draining work. It stops the gadget, synchronizes IRQ, then cancels delayed/role work before freeing resources. A separate lost-disconnect wait can escape only when no gadget driver is bound, hardware is disconnected/disabled, and the controller's enable/run/interrupt bits are clear; active cases still wait for normal cleanup.

These changes address the encountered lifecycle problems. They are not proof of race freedom under every suspend, unplug or hot-unload sequence. In particular, loading the patched module does not retroactively fix a stock instance already in memory. **The public guide avoids that initial hot-unload.** The build-specific private-offset recovery modules used during development are not shipped.

## Other confounders found

- The original host root hub displayed10000M, but its per-port protocol capabilities only exposed5G. Moving to a verified Gen2 port was necessary.
- RNDIS and NCM share a physical cable and the default L4T bridge/DHCP setup. Re-enumeration can move the host address to RNDIS. A TCP connection completing is not proof it used NCM.
- A host reset and transient network profile/neighbor changes invalidated an exploratory control result. The final table uses explicit route/interface-counter checks.
- Recreating the vendor gadget service repaired missing net-device sysfs objects after controller replacement.
- The unused virtual mass-storage function was detached for the final controlled tests. Its involvement in an earlier reset was not established, so it is not presented as a proven reset fix.

## Source provenance

The patch applies to `drivers/usb/gadget/udc/tegra-xudc.c` extracted from NVIDIA's [R36.5 BSP source package](https://developer.nvidia.com/embedded/jetson-linux-r365). Input and output hashes are in [INSTALL.md](INSTALL.md) and enforced by the build script. The patched source was built and run on the target; source identity is stronger than a claim about any arbitrary upstream kernel.

The original installed module was also inspected with readelf, objdump and Ghidra to verify the T234 quirk flag and callback paths. Neither a proprietary manual nor device-specific binary/crash dump is included in this repository. Full gate-level capabilities and undocumented modes beyond this tested result remain outside scope.
