# Contributing

Keep changes small and distinguish source inspection, negotiated link speed, successful enumeration, integrity and throughput. A speed string alone is not sufficient evidence.

For a reproduction report include board/carrier, BSP, kernel, module version, connector/host-port capability, sanitized `lsusb -t`, gadget state, test size and digest, and whether the source was an exact hash match. State whether both directions were tested and whether other workloads were active. Remove serial numbers, MAC/IP addresses, account names and credentials before attaching logs.

Do not weaken board/source/module checks to claim a new platform works. Add a separate documented target after checking its PADCTL mapping, driver ABI and recovery path. Suspend/resume and isochronous behavior are not established by the NCM tests.

Build verification:

```sh
bash -n scripts/build.sh
python3 -m unittest discover -s tests -v
python3 -m py_compile orin_usb_gen2.py orin_install.py bench/transfer.py bench/integrity.py
./scripts/build.sh /path/to/R36.5/kernel/kernel-jammy-src
```

The hardware-independent probes can also run over loopback to check their transport/integrity behavior; that does not validate USB. Do not run root/MMIO or module installation commands in generic CI. Kernel changes require renewed device testing; do not copy old benchmark numbers onto an untested patch.

Contributions are accepted under GPL-2.0-or-later. Preserve upstream attribution. No automated vendor issue, notification or submission is implied by opening a change here.
