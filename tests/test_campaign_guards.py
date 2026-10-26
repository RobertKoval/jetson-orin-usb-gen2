import copy
import importlib.util
from pathlib import Path
from types import SimpleNamespace
import unittest

spec = importlib.util.spec_from_file_location(
    "campaign", Path(__file__).parents[1] / "bench/campaign.py"
)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class CampaignGuards(unittest.TestCase):
    def snapshots(self):
        links = lambda mac: {
            t: {"mtu": 1500, "rps": "0", "ifindex": 1, "mac": mac}
            for t in ("ncm", "rndis")
        }
        return {
            "host": {
                "links": links("host-mac"),
                "ip_mac": "host-mac",
                "route": {"dev": "ncm0"},
                "neighbor": [{"lladdr": "orin-mac"}],
                "usb_mbit": 5000,
                "ncm_settings": {"tx_timer_usecs": "400"},
            },
            "orin": {
                "links": links("orin-port"),
                "ip_mac": "orin-mac",
                "route": {"dev": "bridge0"},
                "neighbor": [{"lladdr": "host-mac"}],
                "usb_state": "configured",
                "usb_speed": "super-speed",
                "qmult": 5,
            },
        }

    def test_changed_setting_in_other_generation_is_rejected(self):
        baseline = self.snapshots()
        changed = copy.deepcopy(baseline)
        changed["host"]["usb_mbit"] = 10000
        changed["orin"]["usb_speed"] = "super-speed-plus"
        changed["host"]["ncm_settings"]["tx_timer_usecs"] = "1000"
        obj = object.__new__(m.Campaign)
        obj.fixed_settings = m.Campaign.fingerprint(baseline)
        obj.cfg = {
            "host": {"ifaces": {"ncm": "ncm0"}},
            "orin": {"ifaces": {"ncm": "usb1"}, "bridge": "bridge0"},
        }
        obj.controls = {
            r: SimpleNamespace(request=lambda role=r, **kw: changed[role])
            for r in changed
        }
        with self.assertRaisesRegex(m.InvalidTrial, "baseline"):
            obj.snapshots("ncm", 2)
        changed["host"]["ncm_settings"]["tx_timer_usecs"] = "400"
        self.assertEqual(obj.snapshots("ncm", 2)["host"]["usb_mbit"], 10000)

    def test_cleanup_finishes_all_servers_after_failure(self):
        obj = object.__new__(m.Campaign)
        closed = []

        def finish(p):
            closed.append(p)
            if p == 1:
                raise RuntimeError("first cleanup failed")

        obj.finish = finish
        with self.assertRaises(RuntimeError):
            obj.finish_all([1, 2])
        self.assertEqual(closed, [1, 2])

    def test_partial_duplex_startup_cleans_started_server(self):
        obj = object.__new__(m.Campaign)
        closed = []
        calls = []
        obj.snapshots = lambda *a: {}
        obj.controls = {
            "host": SimpleNamespace(clock_bounds=lambda: (0, 0)),
            "orin": SimpleNamespace(clock_bounds=lambda: (0, 0)),
        }

        def server(*args):
            calls.append(args)
            if len(calls) == 2:
                raise RuntimeError("second startup failed")
            return "first-server"

        obj.server = server
        obj.finish = lambda p: closed.append(p)
        with self.assertRaises(RuntimeError):
            obj.duplex(1, 1)
        self.assertEqual(closed, ["first-server"])


if __name__ == "__main__":
    unittest.main()
