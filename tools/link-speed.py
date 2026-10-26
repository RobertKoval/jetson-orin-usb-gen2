#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Transient speed switch for the default L4T gadget and the pinned test driver."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import time

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "build/orin-usb-speed"
UDC = Path("/sys/kernel/config/usb_gadget/l4t/UDC")
STATE = Path("/sys/class/udc/3550000.usb")
VERSION = Path("/sys/module/tegra_xudc/version")

def run(mode):
    return json.loads(subprocess.check_output([str(TOOL), mode], text=True))

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("status", "gen1", "gen2"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root is required for /dev/mem; no changes made")
    previous = run("status")
    if args.mode == "status":
        print(json.dumps(previous))
        return
    if VERSION.read_text().strip() != "orin-gen2-probe-3":
        raise RuntimeError("Unexpected driver; do not hot-unload stock tegra_xudc")
    controller = UDC.read_text().strip()
    if controller != "3550000.usb":
        raise RuntimeError("Expected bound default L4T gadget")
    if Path("/sys/class/usb_role/usb2-0-role-switch/role").read_text().strip() != "device":
        raise RuntimeError("Expected device role")
    wanted = "super-speed-plus" if args.mode == "gen2" else "super-speed"
    if previous["generation"] == int(args.mode[-1]) and (STATE / "state").read_text().strip() == "configured" and (STATE / "current_speed").read_text().strip() == wanted:
        print("Already configured at " + wanted)
        return
    def interrupted(signum, frame):
        raise InterruptedError("Interrupted; restoring original setting")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        UDC.write_text("\n")
        time.sleep(1)
        run(args.mode)
        UDC.write_text(controller)
        for _ in range(10):
            time.sleep(1)
            if (STATE / "state").read_text().strip() == "configured" and (STATE / "current_speed").read_text().strip() == wanted:
                print("Configured at " + wanted + "; independently check host speed and data")
                return
        raise RuntimeError("Requested speed did not configure")
    except BaseException:
        UDC.write_text("\n")
        time.sleep(1)
        run("gen" + str(previous["generation"]))
        UDC.write_text(controller)
        raise

if __name__ == "__main__":
    main()
