#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Private stdin RPC over SSH. Only existing CLI mode changes and RPS restoration.
The controller receives sudo credentials through the pipe, never through this JSON.
"""

import ipaddress
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

BASE = Path("/sys/class/net")


def read(p):
    return Path(p).read_text().strip()


def command(args):
    return subprocess.check_output(args, text=True).strip()


def check_name(name):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,14}", name):
        raise ValueError("Invalid interface")
    return name


def context():
    cpus = {}
    for line in Path("/proc/stat").read_text().splitlines():
        words = line.split()
        if words and re.fullmatch(r"cpu[0-9]+", words[0]):
            cpus[words[0]] = list(map(int, words[1:9]))
    freqs = []
    for p in Path("/sys/devices/system/cpu").glob("cpu[0-9]*/cpufreq/scaling_cur_freq"):
        try:
            freqs.append(int(read(p)))
        except (OSError, ValueError, TypeError):
            pass
    temperatures = {}
    for p in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            temperatures[read(p / "type")] = int(read(p / "temp")) / 1000
        except (OSError, ValueError, TypeError):
            temperatures[p.name] = None
    mem = {
        s[0].rstrip(":"): int(s[1])
        for l in Path("/proc/meminfo").read_text().splitlines()
        if (s := l.split()) and len(s) > 1 and s[1].isdigit()
    }
    models = sorted(
        {
            line.split(":", 1)[1].strip()
            for line in Path("/proc/cpuinfo").read_text().splitlines()
            if line.startswith("model name")
        }
    )
    return {
        "cpu_model": models,
        "loadavg": list(os.getloadavg()),
        "cpu_ticks": cpus,
        "cpu_khz": freqs,
        "temperature_c": temperatures,
        "mem_available_kib": mem.get("MemAvailable"),
    }


def snapshot(cfg, transport):
    iface = cfg["ifaces"][transport]
    peer = cfg["peer_ips"][transport]
    route = json.loads(command(["ip", "-j", "route", "get", peer]))[0]
    neighbor = json.loads(
        command(["ip", "-j", "neigh", "show", "to", peer, "dev", route["dev"]])
    )
    links = {}
    for key, name in cfg["ifaces"].items():
        p = BASE / name
        links[key] = {
            "ifindex": int(read(p / "ifindex")),
            "mac": read(p / "address"),
            "mtu": int(read(p / "mtu")),
            "rps": read(p / "queues/rx-0/rps_cpus"),
            **{
                n: int(read(p / "statistics" / n))
                for n in (
                    "rx_bytes",
                    "tx_bytes",
                    "rx_errors",
                    "tx_errors",
                    "rx_dropped",
                    "tx_dropped",
                )
            },
        }
    source_iface = cfg.get("bridge", iface)
    result = {
        "links": links,
        "ip_mac": read(BASE / source_iface / "address"),
        "route": route,
        "neighbor": neighbor,
        "context": context(),
        "kernel": os.uname().release,
    }
    if cfg["role"] == "host":
        usb = (BASE / cfg["ifaces"]["ncm"] / "device").resolve().parent
        result["usb_mbit"] = int(read(usb / "speed"))
        p = BASE / cfg["ifaces"]["ncm"] / "cdc_ncm"
        result["ncm_settings"] = {
            n: read(p / n) for n in ("tx_timer_usecs", "min_tx_pkt", "tx_max", "rx_max")
        }
    else:
        p = Path("/sys/class/udc/3550000.usb")
        result["usb_state"] = read(p / "state")
        result["usb_speed"] = read(p / "current_speed")
        result["qmult"] = int(
            read("/sys/kernel/config/usb_gadget/l4t/functions/ncm.usb0/qmult")
        )
        result["driver"] = read("/sys/module/tegra_xudc/version")
    return result


def main():
    cfg = json.loads(sys.stdin.readline())
    if cfg["role"] not in ("host", "orin"):
        raise ValueError("Invalid role")
    for name in cfg["ifaces"].values():
        check_name(name)
    if "bridge" in cfg:
        check_name(cfg["bridge"])
    for addr in cfg["peer_ips"].values():
        ipaddress.IPv4Address(addr)
    initial_masks = (
        {k: read(BASE / v / "queues/rx-0/rps_cpus") for k, v in cfg["ifaces"].items()}
        if cfg["role"] == "host"
        else {}
    )
    initial_speed = (
        read("/sys/class/udc/3550000.usb/current_speed")
        if cfg["role"] == "orin"
        else None
    )
    initial_status = (
        json.loads(command(["/usr/local/bin/orin-usb-gen2", "status"]))
        if cfg["role"] == "orin"
        else None
    )
    if initial_speed and initial_speed not in ("super-speed", "super-speed-plus"):
        raise ValueError("Initial link must be configured Gen1/Gen2")
    if initial_status and initial_status["generation"] != (
        2 if initial_speed == "super-speed-plus" else 1
    ):
        raise ValueError("Initial capability and negotiated generation must match")

    def switch(generation):
        if cfg["role"] != "orin" or type(generation) != int or generation not in (1, 2):
            raise ValueError("Invalid mode request")
        return json.loads(
            command(
                [
                    "/usr/local/bin/orin-usb-gen2",
                    "enable" if generation == 2 else "disable",
                ]
            )
        )

    def rps():
        if cfg["role"] != "host":
            raise ValueError("Host operation only")
        for key, mask in initial_masks.items():
            p = BASE / cfg["ifaces"][key] / "queues/rx-0/rps_cpus"
            p.write_text(mask)
        return initial_masks

    print("ready", flush=True)
    try:
        for line in sys.stdin:
            try:
                req = json.loads(line)
                op = req["op"]
                if op == "snapshot":
                    out = snapshot(cfg, req["transport"])
                elif op == "clock":
                    out = time.monotonic_ns()
                elif op == "switch":
                    out = switch(req["generation"])
                elif op == "restore_rps":
                    out = rps()
                elif op == "close":
                    break
                else:
                    raise ValueError("Unsupported operation")
                print(json.dumps({"ok": True, "result": out}), flush=True)
            except Exception as exc:
                print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
    finally:
        try:
            if cfg["role"] == "orin":
                switch(initial_status["generation"])
                final = json.loads(command(["/usr/local/bin/orin-usb-gen2", "status"]))
                if final["boot_gen2_enabled"] != initial_status["boot_gen2_enabled"]:
                    raise RuntimeError("Boot policy changed externally during campaign")
            else:
                rps()
            print(json.dumps({"ok": True, "restored": True}), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "restore_error": str(exc)}), flush=True)


if __name__ == "__main__":
    main()
