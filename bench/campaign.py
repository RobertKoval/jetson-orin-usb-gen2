#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Run the predefined campaign via management SSH; data uses explicit USB IPs.
Config contains SSH aliases/interfaces/IPs, never passwords. Output omits them.
"""

import argparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
import getpass
import math
import gzip
import hashlib
import json
from pathlib import Path
import select
import subprocess
import sys
import time

HERE = Path(__file__).resolve().parent
REMOTE = "/tmp/orin-benchmark-campaign"
TOTAL = 1342177280


class InvalidTrial(RuntimeError):
    pass


def ssh(cfg, role, cmd):
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=8",
        cfg[role]["ssh"],
        cmd,
    ]


def line(proc, timeout=30):
    ready, _, _ = select.select([proc.stdout], [], [], timeout)
    if not ready:
        raise TimeoutError("No process response")
    answer = proc.stdout.readline()
    if not answer:
        raise RuntimeError("Process ended before response")
    return answer.strip()


class Control:
    def __init__(self, cfg, role, password):
        self.cfg, self.role = cfg, role
        self.p = subprocess.Popen(
            ssh(cfg, role, "sudo -S -p '' python3 -u " + REMOTE + "/ssh_control.py"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,
        )
        try:
            self.p.stdin.write(password + "\n")
            self.p.stdin.flush()
            remote_cfg = {
                "role": role,
                "ifaces": cfg[role]["ifaces"],
                "peer_ips": cfg["orin" if role == "host" else "host"]["ips"],
            }
            if role == "orin":
                remote_cfg["bridge"] = cfg[role].get("bridge", "l4tbr0")
            self.p.stdin.write(json.dumps(remote_cfg) + "\n")
            self.p.stdin.flush()
            if line(self.p) != "ready":
                raise RuntimeError("Control initialization failed")
        except BaseException:
            self.p.stdin.close()
            self.p.terminate()
            try:
                self.p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait(timeout=5)
            self.p.stdout.close()
            raise

    def request(self, **data):
        self.p.stdin.write(json.dumps(data) + "\n")
        self.p.stdin.flush()
        reply = json.loads(line(self.p, 40))
        if not reply["ok"]:
            raise RuntimeError(self.role + ": " + reply.get("error", str(reply)))
        return reply["result"]

    def clock_bounds(self):
        samples = []
        for _ in range(3):
            start = time.monotonic_ns()
            remote = self.request(op="clock")
            end = time.monotonic_ns()
            samples.append((remote - end, remote - start))
        return min(samples, key=lambda v: v[1] - v[0])

    def close(self):
        self.p.stdin.write('{"op":"close"}\n')
        self.p.stdin.flush()
        reply = json.loads(line(self.p, 40))
        self.p.stdin.close()
        self.p.wait(timeout=10)
        self.p.stdout.close()
        if not reply.get("restored"):
            raise RuntimeError("Control failed to restore initial state: " + str(reply))


class Campaign:
    def __init__(self, cfg, out, smoke=False):
        self.smoke = smoke
        self.cfg, self.out = cfg, Path(out)
        self.controls = {}
        self.records = []
        self.next_id = 0
        self.out.mkdir(parents=True, exist_ok=True)
        if (self.out / "raw.jsonl.gz").exists():
            raise FileExistsError("Refusing to overwrite an earlier campaign")
        self.active_trial = {}
        self.fixed_settings = None
        self.raw = gzip.open(self.out / "raw.jsonl.gz", "wt")

    def setup(self):
        for role in ("host", "orin"):
            subprocess.run(ssh(self.cfg, role, "mkdir -p " + REMOTE), check=True)
            for name in ("suite_probe.py", "ssh_control.py"):
                subprocess.run(
                    [
                        "scp",
                        "-q",
                        str(HERE / name),
                        self.cfg[role]["ssh"] + ":" + REMOTE + "/" + name,
                    ],
                    check=True,
                )
        # The credentials exist only in local/SSH process memory, never config/output.
        for role in ("orin", "host"):
            password = getpass.getpass(role + " sudo password: ")
            self.controls[role] = Control(self.cfg, role, password)
            del password

    @staticmethod
    def fingerprint(snaps):
        return {
            "host_ncm": snaps["host"]["ncm_settings"],
            "qmult": snaps["orin"]["qmult"],
            "interfaces": {
                role: {
                    t: {k: v[k] for k in ("mtu", "rps")}
                    for t, v in snap["links"].items()
                }
                for role, snap in snaps.items()
            },
        }

    def snapshots(self, transport, generation):
        result = {
            r: c.request(op="snapshot", transport=transport)
            for r, c in self.controls.items()
        }
        host, orin = result["host"], result["orin"]
        if host["usb_mbit"] != generation * 5000:
            raise InvalidTrial("wrong_host_link_speed")
        if orin["usb_state"] != "configured" or orin["usb_speed"] != (
            "super-speed-plus" if generation == 2 else "super-speed"
        ):
            raise InvalidTrial("wrong_device_link_state")
        for role, other in [("host", "orin"), ("orin", "host")]:
            expected = self.cfg[role].get("bridge", self.cfg[role]["ifaces"][transport])
            if result[role]["route"]["dev"] != expected:
                raise InvalidTrial("wrong_route_" + role)
            neighbor = result[role]["neighbor"]
            if not any(
                n.get("lladdr", "").lower() == result[other]["ip_mac"].lower()
                for n in neighbor
            ):
                raise InvalidTrial("wrong_neighbor_" + role)
        if (
            self.fixed_settings is not None
            and self.fingerprint(result) != self.fixed_settings
        ):
            raise InvalidTrial("settings_differ_from_campaign_baseline")
        return result

    def set_mode(self, generation):
        self.controls["orin"].request(op="switch", generation=generation)
        if not self.smoke:
            time.sleep(
                20
            )  # fixed delay; no stationarity or stall-free behavior is implied
        deadline = time.monotonic() + 20
        while True:
            try:
                self.controls["host"].request(op="restore_rps")
                for transport in ("ncm", "rndis"):
                    peer = self.cfg["orin"]["ips"][transport]
                    subprocess.run(
                        ssh(self.cfg, "host", f"ping -n -q -c 1 -W 2 {peer}"),
                        check=True,
                        capture_output=True,
                        text=True,
                        timeout=8,
                    )
                result = self.snapshots("ncm", generation)
                self.snapshots("rndis", generation)
                return result
            except Exception:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.5)

    def server(self, dst, transport, kind, port):
        cmd = f"python3 {REMOTE}/suite_probe.py server {self.cfg[dst]['ips'][transport]} --kind {kind} --port {port}"
        p = subprocess.Popen(
            ssh(self.cfg, dst, cmd), stdout=subprocess.PIPE, text=True, bufsize=1
        )
        try:
            if line(p, 15) != "ready":
                raise RuntimeError("Server not ready")
            return p
        except BaseException:
            p.terminate()
            p.wait(timeout=5)
            raise

    def client(self, src, dst, transport, kind, port, total=TOTAL, loaded=False):
        cmd = f"python3 {REMOTE}/suite_probe.py {kind} {self.cfg[dst]['ips'][transport]} --port {port}"
        if kind == "bulk":
            cmd += f" --bytes {1048576 if self.smoke else total}"
        if kind == "latency" and self.smoke:
            cmd += " --quick"
        if loaded:
            cmd += " --loaded"
        p = subprocess.run(
            ssh(self.cfg, src, cmd), capture_output=True, text=True, timeout=150
        )
        if p.returncode:
            raise RuntimeError("Probe failed: " + p.stderr)
        return json.loads(p.stdout)

    @staticmethod
    def finish(p):
        try:
            rc = p.wait(timeout=5)
            if rc:
                raise RuntimeError("Server failed")
        finally:
            if p.poll() is None:
                p.terminate()
                p.wait(timeout=5)
            p.stdout.close()

    @staticmethod
    def cpu(before, after):
        busy = []
        soft = []
        for name, a in before["cpu_ticks"].items():
            b = after["cpu_ticks"][name]
            v = [y - x for x, y in zip(a, b)]
            total = sum(v)
            if total > 0:
                busy.append((total - v[3] - v[4]) / total * 100)
                soft.append(v[6] / total * 100)
        return {
            "mean_busy_pct": sum(busy) / len(busy) if busy else None,
            "max_softirq_pct": max(soft) if soft else None,
            "loadavg_before": before["loadavg"],
            "loadavg_after": after["loadavg"],
            "temperature_c_after": after["temperature_c"],
            "cpu_khz_after": after["cpu_khz"],
            "mem_available_kib_after": after["mem_available_kib"],
        }

    def save(
        self, pair, generation, transport, direction, result, before, after, label
    ):
        src, dst = direction.split("-")
        links = {}
        for role in ("host", "orin"):
            a = before[role]["links"][transport]
            b = after[role]["links"][transport]
            if a["ifindex"] != b["ifindex"]:
                raise InvalidTrial("interface_recreated_" + role)
            if a["mtu"] != b["mtu"] or a["rps"] != b["rps"]:
                raise InvalidTrial("interface_settings_changed")
            links[role] = {
                key: b[key] - a[key]
                for key in (
                    "rx_bytes",
                    "tx_bytes",
                    "rx_errors",
                    "tx_errors",
                    "rx_dropped",
                    "tx_dropped",
                )
            }
        required = (
            result["bytes"]
            if result["kind"] == "bulk"
            else sum(c["bytes"] * (c["n"] + c["warmup"]) for c in result["cases"])
        )
        for role, key in ((src, "tx_bytes"), (dst, "rx_bytes")):
            if links[role][key] < required:
                raise InvalidTrial("insufficient_data_on_" + role)
        if result["kind"] == "latency":
            for role, key in ((src, "rx_bytes"), (dst, "tx_bytes")):
                if links[role][key] < required:
                    raise InvalidTrial("echo_return_path_mismatch")
        if (
            before["host"]["ncm_settings"] != after["host"]["ncm_settings"]
            or before["orin"]["qmult"] != after["orin"]["qmult"]
        ):
            raise InvalidTrial("driver_settings_changed")
        self.next_id += 1
        record = {
            "id": self.next_id,
            "pair": pair,
            "generation": generation,
            "transport": transport,
            "direction": direction,
            "label": label,
            "valid": True,
            "result": result,
            "byte_deltas": links,
            "context": {
                r: self.cpu(before[r]["context"], after[r]["context"])
                for r in ("host", "orin")
            },
            "settings": {
                "host_ncm": before["host"]["ncm_settings"],
                "qmult": before["orin"]["qmult"],
                "mtu": {
                    r: before[r]["links"][transport]["mtu"] for r in ("host", "orin")
                },
                "rps": {
                    r: before[r]["links"][transport]["rps"] for r in ("host", "orin")
                },
            },
        }
        self.raw.write(json.dumps(record) + "\n")
        self.raw.flush()
        self.records.append(record)

    def one(self, pair, generation, transport, direction, kind):
        self.active_trial = {
            "pair": pair,
            "generation": generation,
            "transport": transport,
            "direction": direction,
            "kind": kind,
            "label": "unloaded",
        }
        src, dst = direction.split("-")
        before = self.snapshots(transport, generation)
        p = self.server(dst, transport, "echo" if kind == "latency" else "sink", 45682)
        try:
            result = self.client(src, dst, transport, kind, 45682)
        finally:
            self.finish(p)
        after = self.snapshots(transport, generation)
        self.save(
            pair, generation, transport, direction, result, before, after, "unloaded"
        )

    def finish_all(self, servers):
        errors = []
        for p in servers:
            try:
                self.finish(p)
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError("Server cleanup: " + "; ".join(errors))

    def duplex(self, pair, generation):
        self.active_trial = {
            "pair": pair,
            "generation": generation,
            "transport": "ncm",
            "kind": "bulk",
            "label": "duplex",
        }
        before = self.snapshots("ncm", generation)
        clocks_before = {r: c.clock_bounds() for r, c in self.controls.items()}
        servers = []
        try:
            for dst in ("host", "orin"):
                servers.append(self.server(dst, "ncm", "sink", 45683))
            with ThreadPoolExecutor(2) as pool:
                jobs = [
                    pool.submit(self.client, src, dst, "ncm", "bulk", 45683)
                    for src, dst in [("host", "orin"), ("orin", "host")]
                ]
                results = [j.result() for j in jobs]
        finally:
            self.finish_all(servers)
        clocks_after = {r: c.clock_bounds() for r, c in self.controls.items()}
        bounds = {
            r: (
                min(clocks_before[r][0], clocks_after[r][0]),
                max(clocks_before[r][1], clocks_after[r][1]),
            )
            for r in clocks_before
        }
        # Conservative overlap estimate under offsets bracketed by before/after SSH calibration.
        lower = (
            min(v["end_ns"] - bounds[r][1] for r, v in zip(("host", "orin"), results))
            - max(
                v["start_ns"] - bounds[r][0] for r, v in zip(("host", "orin"), results)
            )
        ) / 1e9
        upper = (
            min(v["end_ns"] - bounds[r][0] for r, v in zip(("host", "orin"), results))
            - max(
                v["start_ns"] - bounds[r][1] for r, v in zip(("host", "orin"), results)
            )
        ) / 1e9
        if lower <= 0:
            raise InvalidTrial("duplex_overlap_not_established")
        after = self.snapshots("ncm", generation)
        for direction, result in zip(("host-orin", "orin-host"), results):
            result.update(
                overlap_estimate_s=[lower, upper],
                clock_calibration_width_ms={
                    r: (b[1] - b[0]) / 1e6 for r, b in bounds.items()
                },
            )
            self.save(
                pair, generation, "ncm", direction, result, before, after, "duplex"
            )

    def loaded(self, pair, generation):
        self.active_trial = {
            "pair": pair,
            "generation": generation,
            "label": "under-ncm-load",
        }
        before_ncm = self.snapshots("ncm", generation)
        before_rndis = self.snapshots("rndis", generation)
        servers = []
        try:
            servers.append(self.server("orin", "ncm", "sink", 45683))
            servers.append(self.server("orin", "rndis", "echo", 45684))
            with ThreadPoolExecutor(2) as pool:
                bulk = pool.submit(
                    self.client, "host", "orin", "ncm", "bulk", 45683, 4 * 1024**3
                )
                time.sleep(0.5)
                echo = pool.submit(
                    self.client, "host", "orin", "rndis", "latency", 45684, TOTAL, True
                )
                large = bulk.result()
                small = echo.result()
        finally:
            self.finish_all(servers)
        a = self.snapshots("ncm", generation)
        b = self.snapshots("rndis", generation)
        case = small["cases"][0]
        qualified = [
            i
            for i, (start, end) in enumerate(zip(case["start_ns"], case["end_ns"]))
            if start >= large["start_ns"] and end <= large["end_ns"]
        ]
        if len(qualified) < 100:
            raise InvalidTrial("insufficient_loaded_overlap")
        values = sorted(case["rtt_ns"][i] for i in qualified)
        case["overlap_indices"] = qualified
        case["overlap_stats"] = {
            "n": len(values),
            **{
                f"p{q}_us": values[math.ceil(len(values) * q / 100) - 1] / 1000
                for q in (50, 95, 99)
            },
        }
        small["bulk_interval_ns"] = [large["start_ns"], large["end_ns"]]
        self.save(
            pair, generation, "ncm", "host-orin", large, before_ncm, a, "load-generator"
        )
        self.save(
            pair,
            generation,
            "rndis",
            "host-orin",
            small,
            before_rndis,
            b,
            "under-ncm-load",
        )

    def run(self):
        if self.smoke:
            self.set_mode(2)
            for transport in ("ncm", "rndis"):
                for direction in ("host-orin", "orin-host"):
                    for kind in ("latency", "bulk"):
                        self.one(0, 2, transport, direction, kind)
            print("SMOKE_COMPLETE", flush=True)
            return
        for pair in range(1, 7):
            order = (1, 2) if pair % 2 else (2, 1)
            for generation in order:
                self.active_trial = {
                    "pair": pair,
                    "generation": generation,
                    "label": "switch",
                }
                print(f"pair={pair} generation={generation} START", flush=True)
                self.set_mode(generation)
                for transport in ("ncm", "rndis"):
                    for direction in ("host-orin", "orin-host"):
                        self.one(pair, generation, transport, direction, "latency")
                    for direction in ("host-orin", "orin-host"):
                        self.one(pair, generation, transport, direction, "bulk")
                self.duplex(pair, generation)
                if pair in (1, 4):
                    self.loaded(pair, generation)
                print(
                    f"pair={pair} generation={generation} DONE records={len(self.records)}",
                    flush=True,
                )

    def close(self):
        # Restore device mode first; then restore host interface masks after re-enumeration.
        errors = []
        for role in ("orin", "host"):
            if role in self.controls:
                if role == "host":
                    time.sleep(2)
                try:
                    self.controls[role].close()
                except Exception as exc:
                    errors.append(role + ": " + str(exc))
        self.raw.close()
        if errors:
            raise RuntimeError("RESTORATION FAILED: " + "; ".join(errors))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    parser.add_argument("--out", required=True)
    parser.add_argument("--smoke", action="store_true")
    a = parser.parse_args()
    cfg = json.loads(Path(a.config).read_text())
    campaign = Campaign(cfg, a.out, a.smoke)
    started = datetime.now(timezone.utc)
    manifest = {
        "date": started.date().isoformat(),
        "started_utc": started.isoformat(),
        "probe_sha256": hashlib.sha256(
            (HERE / "suite_probe.py").read_bytes()
        ).hexdigest(),
        "smoke": a.smoke,
        "pairs": 0 if a.smoke else 6,
        "pair_orders": []
        if a.smoke
        else [[1, 2] if i % 2 else [2, 1] for i in range(1, 7)],
        "settle_s": 0 if a.smoke else 20,
        "protocol_revision": 2,
        "complete": False,
    }
    try:
        campaign.setup()
        baseline = {
            r: c.request(op="snapshot", transport="ncm")
            for r, c in campaign.controls.items()
        }
        manifest["environment"] = {
            r: {"kernel": v["kernel"], "cpu_model": v["context"]["cpu_model"]}
            for r, v in baseline.items()
        }
        campaign.fixed_settings = campaign.fingerprint(baseline)
        manifest["fixed_settings"] = campaign.fixed_settings
        manifest["driver"] = baseline["orin"]["driver"]
        manifest["initial_host_link_mbit"] = baseline["host"]["usb_mbit"]
        campaign.run()
        manifest["complete"] = True
    except BaseException as exc:
        manifest["failure_type"] = type(exc).__name__
        manifest["failed_trial"] = campaign.active_trial
        campaign.raw.write(
            json.dumps(
                {
                    **campaign.active_trial,
                    "valid": False,
                    "failure_type": type(exc).__name__,
                    "reason": str(exc)
                    if isinstance(exc, InvalidTrial)
                    else "probe_or_control_failure",
                }
            )
            + "\n"
        )
        campaign.raw.flush()
        if isinstance(exc, InvalidTrial):
            manifest["invalid_reason"] = str(exc)
        raise
    finally:
        try:
            campaign.close()
            manifest["restored"] = True
        except Exception as exc:
            manifest["restored"] = False
            print(str(exc), file=sys.stderr)
        manifest["finished_utc"] = datetime.now(timezone.utc).isoformat()
        (Path(a.out) / "manifest.json").write_text(
            json.dumps(manifest, indent=2) + "\n"
        )
        if not manifest["restored"]:
            raise RuntimeError(
                "Campaign did not restore the initial state; inspect both hosts"
            )


if __name__ == "__main__":
    main()
