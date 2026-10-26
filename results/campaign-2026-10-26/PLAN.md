# Predefined benchmark campaign

2026-10-26. Driver and runtime/permanent CLI unchanged. Only runtime generation is switched; permanent boot policy stays enabled. Final state is restored to initialGen2 and original host RPS masks.

Six paired rounds, order alternating: [Gen1,Gen2], [Gen2,Gen1], repeated3times. Each mode block:
- NCM and RNDIS echo from each endpoint:1B,20KiB,64KiB,1MiB; respectively2000,2000,1000,100 measured RTTs,100warmups per size. Persistent TCP_NODELAY, exact echo comparison, one local monotonic clock. Back-to-back closed-loop load, not paced application traffic.
- NCM and RNDIS bulk1.25GiB in each direction, separate runs.
- NCM duplex: two1.25GiB transfers started concurrently.
- Rounds1 and4 additionally: host-initiated RNDIS20KiB echo while host→Orin NCM streams4GiB. Only samples wholly inside the measured bulk interval are analyzed; both clients use the host's monotonic clock.

For every mode block independently verify host10000/5000 and device configuredSSP/SS, expected interfaces/routes, fixed NCM settings and host RPS. Observe interface indices, actual data-byte deltas, error/drop counters, CPU/load and temperature context. Reject invalid/misrouted/re-enumerated trials explicitly; retain failed records and reasons. No stopping unrelated workloads or rebooting.

Report per-run distributions and cross-run medians/ranges, not invented confidence intervals. Do not pool RTT samples as independent experimental replications. Hash integrity is checked separately after the campaign. Synthetic CPU-buffer probes only: no model/CUDA/inference throughput claim. Paths/addresses/serials/credentials remain outside published data.

Before the full run, method review added campaign-wide baseline enforcement for NCM parameters/qmult/MTU/RPS and complete cleanup after partial server startup. Duplex overlap is estimated from client monotonic intervals with before/after SSH clock-offset brackets; report the bracket width and do not sum rates as guaranteed instantaneous aggregate bandwidth. Loaded quantiles are explicitly recomputed from fully-overlapping samples (`overlap_stats`), with two paired loaded repetitions only. Optional unreadable thermal sensors are reported as null; they are not zero-temperature readings. Smoke runs are separate from the campaign dataset.

## Revision 2: fixed post-selection delay

The first full attempt completed Gen1 block1, then its first Gen2 NCM latency trial timed out. Both host USB network functions reported NETDEV WATCHDOG shortly after re-enumeration; current Gen2 connectivity later recovered without another mode change. Failed attempt retained under preliminary/, excluded from paired statistics. A follow-up full Gen2 latency probe after settling completed.

Before restarting the whole paired series, add a fixed20-second settling interval after every mode selection, including unchanged-mode selections. All other payloads/counts/order/settings remain the same. This defines a comparison after a fixed delay, without establishing stationarity and does NOT fix, hide, or validate away the observed switching transient. Report that failure separately; do not claim reliable immediate data readiness after every mode switch.

## Observed stop and analysis disposition

Revision2 stopped during pair4 Gen2 mixed load with host watchdogs207s after the last enumeration. This falsifies the idea that20s settling alone prevents the observed failure; it does not identify its cause. Retain all74 valid records and the failed record. Summarize the longest complete paired prefix (pairs1–3,64 records); retain10 unmatched pair4 Gen2 records without assigning them a paired comparison. This post-abort descriptive rule is explicit in the analyzer and report. Loaded latency has one completed pair; six planned pairs were not completed. Independent1.25GiB hashes passed both directions after recovery, initial Gen2/RPS and permanent policy restored, no reboot. Further stress/reproduction remains tracked in TODO.md.
