# Open work

- [ ] Diagnose Gen2 NETDEV WATCHDOG / data-path stalls after runtime switching and under simultaneous NCM bulk + RNDIS echo; reproduce with controlled background load, capture queue/USB evidence, then rerun the unfinished six-pair campaign. See docs/BENCHMARKS.md and retained failure records.

- [ ] Validate the new installation and boot service across a real cold boot. Explicitly deferred by the user on2026-10-26; disk install, live service and runtime/permanent toggles passed. After reboot verify loaded module version, service journal, configuredSSP, host10000M and data integrity.
- [ ] Longer transport stress, suspend/resume and repeated physical reconnect testing; no claim of general USB certification.
- [ ] Investigate forward-path CPU/packet-processing bottlenecks and RX drops separately from link generation.
- [ ] Revisit C↔C with the patched driver; the validated path is SS10 A↔C.
- [ ] Validate any additional board, BSP or isochronous workload separately; existing identity guards stay strict.
