#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Summarize complete paired prefixes; retain and disclose aborted campaigns."""

import argparse
from collections import defaultdict
import gzip
import json
import math
from pathlib import Path
import statistics


def quantile(values, p):
    ordered = sorted(values)
    if not ordered:
        raise ValueError("Empty distribution")
    return ordered[math.ceil(len(ordered) * p) - 1]


def describe(values):
    return {
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
        "runs": len(values),
        "values": values,
    }


def loadavg_range(rows, role):
    values = [
        r["context"][role][key][0]
        for r in rows
        for key in ("loadavg_before", "loadavg_after")
    ]
    return [min(values), max(values)]


def schedule():
    keys = []
    for pair in range(1, 7):
        for g in (1, 2) if pair % 2 else (2, 1):
            for t in ("ncm", "rndis"):
                for kind in ("latency", "bulk"):
                    for d in ("host-orin", "orin-host"):
                        keys.append((pair, g, t, d, "unloaded", kind))
            for d in ("host-orin", "orin-host"):
                keys.append((pair, g, "ncm", d, "duplex", "bulk"))
            if pair in (1, 4):
                keys.extend(
                    [
                        (pair, g, "ncm", "host-orin", "load-generator", "bulk"),
                        (pair, g, "rndis", "host-orin", "under-ncm-load", "latency"),
                    ]
                )
    return keys


def select_rows(manifest, rows, allow_partial=False):
    if manifest.get("smoke"):
        raise ValueError("Smoke data is not a campaign")
    if not manifest.get("restored"):
        raise ValueError("Initial device state was not restored")
    if not manifest.get("complete") and not allow_partial:
        raise ValueError(
            "Aborted campaign: use --allow-incomplete to report its complete paired prefix"
        )
    failures = [r for r in rows if not r.get("valid")]
    valid = rows[: len(rows) - len(failures)] if failures else rows
    if any(not r.get("valid") for r in valid) or len(failures) > 1:
        raise ValueError("Invalid records must form a single terminal failure")
    keys = [
        (
            r["pair"],
            r["generation"],
            r["transport"],
            r["direction"],
            r["label"],
            r["result"]["kind"],
        )
        for r in valid
    ]
    expected = schedule()
    if keys != expected[: len(keys)]:
        raise ValueError("Data is not an exact prefix of the planned schedule")
    if manifest.get("complete") and (failures or keys != expected):
        raise ValueError("Complete manifest does not match schedule")
    pairs = [
        p
        for p in range(1, 7)
        if sum(k[0] == p for k in keys) == sum(k[0] == p for k in expected)
    ]
    if not pairs:
        raise ValueError("No complete paired comparison")
    selected = [r for r in valid if r["pair"] in pairs]
    return selected, {
        "complete_pairs": pairs,
        "selected_valid_records": len(selected),
        "all_valid_records": len(valid),
        "unpaired_valid_records": len(valid) - len(selected),
        "failed_records": failures,
        "selection": "Longest fully completed paired prefix; unmatched valid rows retained in raw data",
    }


def analyze(folder, allow_partial=False):
    folder = Path(folder)
    manifest = json.loads((folder / "manifest.json").read_text())
    with gzip.open(folder / "raw.jsonl.gz", "rt") as f:
        rows = [json.loads(x) for x in f]
    rows, coverage = select_rows(manifest, rows, allow_partial)
    latency = defaultdict(list)
    bulk = defaultdict(list)
    loaded = defaultdict(list)
    unloaded_samples = loaded_samples = 0
    for r in rows:
        v = r["result"]
        g = r["generation"]
        pair = r["pair"]
        transport = r["transport"]
        direction = r["direction"]
        if v["kind"] == "bulk":
            if r["label"] != "load-generator":
                bulk[(g, transport, direction, r["label"])].append(
                    (pair, v["MiB_s"], v["seconds"])
                )
        elif r["label"] == "unloaded":
            for c in v["cases"]:
                if c["n"] != len(c["rtt_ns"]):
                    raise ValueError("Sample count mismatch")
                stats = {
                    f"p{q}_us": quantile(c["rtt_ns"], q / 100) / 1000
                    for q in (50, 95, 99)
                }
                for k, val in stats.items():
                    if abs(c[k] - val) > 1e-9:
                        raise ValueError("Recorded percentile mismatch")
                latency[(g, transport, direction, c["bytes"])].append(
                    (pair, stats, c["n"])
                )
                unloaded_samples += c["n"]
        else:
            c = v["cases"][0]
            start, end = v["bulk_interval_ns"]
            ids = [
                i
                for i, (s, e) in enumerate(zip(c["start_ns"], c["end_ns"]))
                if s >= start and e <= end
            ]
            if ids != c["overlap_indices"]:
                raise ValueError("Loaded overlap mismatch")
            values = [c["rtt_ns"][i] for i in ids]
            if len(values) < 100:
                raise ValueError("Insufficient overlap")
            stats = {f"p{q}_us": quantile(values, q / 100) / 1000 for q in (50, 95, 99)}
            if c["overlap_stats"]["n"] != len(values):
                raise ValueError("Overlap sample count mismatch")
            for k, val in stats.items():
                if abs(c["overlap_stats"][k] - val) > 1e-9:
                    raise ValueError("Loaded percentile mismatch")
            loaded[(g, transport, direction, c["bytes"])].append(
                (pair, stats, len(values))
            )
            loaded_samples += len(values)
    expected = set(coverage["complete_pairs"])
    count = len(expected)
    latrows = []
    if len(latency) != 32:
        raise ValueError("Missing latency groups")
    for (g, t, d, n), runs in sorted(latency.items()):
        if len(runs) != count or {x[0] for x in runs} != expected:
            raise ValueError("Incomplete latency pair coverage")
        latrows.append(
            {
                "generation": g,
                "transport": t,
                "direction": d,
                "bytes": n,
                "samples_per_run": [x[2] for x in runs],
                **{
                    k: describe([x[1][k] for x in runs])
                    for k in ("p50_us", "p95_us", "p99_us")
                },
            }
        )
    bulkrows = []
    if len(bulk) != 12:
        raise ValueError("Missing bulk groups")
    for (g, t, d, label), runs in sorted(bulk.items()):
        if len(runs) != count or {x[0] for x in runs} != expected:
            raise ValueError("Incomplete bulk pair coverage")
        bulkrows.append(
            {
                "generation": g,
                "transport": t,
                "direction": d,
                "label": label,
                "MiB_s": describe([x[1] for x in runs]),
                "seconds": describe([x[2] for x in runs]),
            }
        )
    loadrows = []
    if len(loaded) != 2:
        raise ValueError("Missing loaded groups")
    for (g, t, d, n), runs in sorted(loaded.items()):
        load_expected = expected & {1, 4}
        if len(runs) != len(load_expected) or {x[0] for x in runs} != load_expected:
            raise ValueError("Incomplete loaded pair coverage")
        idle = {x[0]: x[1] for x in latency[(g, t, d, n)]}
        loadrows.append(
            {
                "generation": g,
                "transport": t,
                "direction": d,
                "bytes": n,
                "samples_per_run": [x[2] for x in runs],
                "paired_idle": {
                    k: describe([idle[x[0]][k] for x in runs])
                    for k in ("p50_us", "p95_us", "p99_us")
                },
                **{
                    k: describe([x[1][k] for x in runs])
                    for k in ("p50_us", "p95_us", "p99_us")
                },
            }
        )
    ratios = []
    for t in ("ncm", "rndis"):
        for d in ("host-orin", "orin-host"):
            a = {x[0]: x[1] for x in bulk[(1, t, d, "unloaded")]}
            b = {x[0]: x[1] for x in bulk[(2, t, d, "unloaded")]}
            ratios.append(
                {
                    "transport": t,
                    "direction": d,
                    "paired_gen2_over_gen1": describe(
                        [b[i] / a[i] for i in sorted(expected)]
                    ),
                }
            )
    summary = {
        "manifest": manifest,
        "coverage": coverage,
        "valid_records": len(rows),
        "unloaded_rtt_samples": unloaded_samples,
        "qualified_loaded_rtt_samples": loaded_samples,
        "latency": latrows,
        "bulk": bulkrows,
        "loaded": loadrows,
        "paired_throughput_ratios": ratios,
        "host_loadavg_range": loadavg_range(rows, "host"),
        "orin_loadavg_range": loadavg_range(rows, "orin"),
        "max_trial_rx_dropped": {
            role: max(r["byte_deltas"][role]["rx_dropped"] for r in rows)
            for role in ("host", "orin")
        },
    }
    (folder / "summary.json").write_text(
        json.dumps(summary, indent=2, allow_nan=False) + "\n"
    )
    return summary


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("folder")
    p.add_argument("--allow-incomplete", action="store_true")
    a = p.parse_args()
    s = analyze(a.folder, a.allow_incomplete)
    print(
        json.dumps(
            {
                k: s[k]
                for k in (
                    "coverage",
                    "valid_records",
                    "unloaded_rtt_samples",
                    "qualified_loaded_rtt_samples",
                    "paired_throughput_ratios",
                )
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
