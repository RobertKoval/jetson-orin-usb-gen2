# Open work

- [ ] Before further kernel changes, A/B-test the current A↔C cable against a short, verified USB-IF-certified 10Gb/s A↔C cable: current → replacement → current, using the same SS10 host port, driver, settings and controlled background workload. Compare bulk throughput in both directions, RTT p50/p95/p99, duplex and mixed NCM bulk + RNDIS echo; record watchdogs and USB link errors/retries/recovery where observable. Verify payload hashes separately: a matching digest does not rule out retransmissions. Resume when the comparison cable is available; no further experiments scheduled for today.

- [ ] Diagnose Gen2 NETDEV WATCHDOG / data-path stalls after runtime switching and under simultaneous NCM bulk + RNDIS echo; reproduce with controlled background load, capture queue/USB evidence, then rerun the unfinished six-pair campaign. See docs/BENCHMARKS.md and retained failure records.

- [ ] Validate the new installation and boot service across a real cold boot. Explicitly deferred by the user on2026-10-26; disk install, live service and runtime/permanent toggles passed. After reboot verify loaded module version, service journal, configuredSSP, host10000M and data integrity.
- [ ] Longer transport stress, suspend/resume and repeated physical reconnect testing; no claim of general USB certification.
- [ ] Investigate forward-path CPU/packet-processing bottlenecks and RX drops separately from link generation.
- [ ] Revisit C↔C with the patched driver; the validated path is SS10 A↔C.
- [ ] Validate any additional board, BSP or isochronous workload separately; existing identity guards stay strict.
