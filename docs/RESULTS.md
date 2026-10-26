# Validation record — 2026-10-26

This page records the initial experiment. See [the later repeated campaign](BENCHMARKS.md) for expanded measurements and the newly observed unresolved TX-queue stalls.

## Environment

One NVIDIA Jetson AGX Orin Developer Kit64GB, Jetson Linux R36.5, kernel5.15.185-tegra. Linux x86-64 host, verified10G Type-A port and A↔C cable. CPU buffers only; no GPU staging, LLM execution, weight quantization or NVMe offload was measured.

The final Gen1/Gen2 comparison used the same port and candidate driver, NCM, MTU1500, qmult5, host aggregation timer400µs, padding threshold13312 and the same RPS configuration. The UMS virtual-disk function was absent in both final arms. Machines were shared, not isolated benchmark hosts.

## Bulk data

Each transfer sends **1,342,177,280 bytes** in1MiB writes, then waits for the receiver's byte-count acknowledgment. This corresponds to65,536 tokens ×4 residual streams ×2560 elements ×2 bytes. It is a transfer-size example, not proof of an inference runtime's exact wire layout.

Final unidirectional results:

| Direction | Gen1 seconds | Gen2 seconds | Gen2 MiB/s |
|---|---:|---:|---:|
| Host→Orin | 3.727667 | 3.378639 | 378.85 |
| Orin→host | 3.575431 | 2.323943 | 550.79 |

Concurrent Gen2 transfers took4.704041s and2.381666s. They overlap but do not both sustain their isolated-direction rates. One final guarded run per direction/rate was collected, plus exploratory runs; no confidence interval or sustained peak rate is claimed.

The final runner checked the route, interface index before/after, and actual bytes counted on NCM. The public transfer probe prints transfer results; **it does not automatically verify your network routing**. Check interface counters independently as described in INSTALL.md.

## Integrity

A separate probe uses deterministic pseudorandom1MiB blocks with a distinct byte-offset header and streaming SHA-256. It checked the complete1.25GiB in both directions, twice per direction atGen2, including after the final configuration:

`78e2ca85f0fb48ea122cff838c9d6239d22cc8daebee83c2a2c4402510475d64`

Both final checks returned `verified=true`. Integrity-test timing includes hashing and is not the throughput benchmark. Finite tests cannot establish permanent corruption freedom.

## Small-message RTT

RNDIS, persistent TCP_NODELAY echo, payload comparison on every iteration;20 warmup +100 measured samples (1MiB had only10 measured samples). Host initiates:

| Bytes | p50 ms | p95 ms | p99 ms |
|---|---:|---:|---:|
| 1 | .080229 | .082818 | .086479 |
| 8192 | .230236 | .252396 | .265306 |
| 16384 | .343644 | .364794 | .366514 |
| 20480 | .410283 | .435333 | .478453 |
| 65536 | 1.140971 | 1.199080 | 1.215870 |

RTT includes sending the entire payload and returning it; it is not one-way activation latency. The public repository ships bulk/integrity probes, not a claim that every inference transport achieves this RTT.

## Limits and adverse observations

- Negotiated10G is confirmed separately on the host and device; it is not10Gb/s application throughput.
- Final NCM error counters were0, but1038 RX drops were observed across roughly5.8million packets. TCP recovered and hashes matched. This is **not** a zero-packet-loss result.
- The initial hot replacement caused a real kernel panic and required reboot. The source-level lifetime fix and successful guarded migration are documented in DESIGN.md. This is not an unattended production-ready driver release.
- Gen1 rollback/re-enable, composite enumeration and one vendor gadget-service recreation were exercised. General module unloading, all suspend/resume paths, USB compliance, other gadget functions and long-duration stress remain unvalidated.
- No new panic was observed after the final candidate load in the session. This observation is not a formal concurrency proof.
- The native public build script is checked against the exact tested driver source. Public wrapper packaging and cold-boot disk installation have narrower verification than the original live hardware experiment; see README and INSTALL.
- Private logs, addresses, MACs, serials and crash dumps were omitted. Sanitized numeric transfer results are in `results/measurements.json`.
