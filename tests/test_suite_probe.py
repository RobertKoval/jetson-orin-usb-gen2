import importlib.util
import json
from pathlib import Path
import socket
import subprocess
import sys
import unittest

FILE = Path(__file__).parents[1] / "bench/suite_probe.py"
spec = importlib.util.spec_from_file_location("suite_probe", FILE)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def test_nearest_rank_quantiles(self):
        r = probe.distribution([i * 1000 for i in range(100, 0, -1)])
        self.assertEqual((r["p50_us"], r["p95_us"], r["p99_us"]), (50, 95, 99))
        self.assertEqual(probe.distribution([1000])["p99_us"], 1)
        with self.assertRaises(ValueError):
            probe.distribution([])

    def exchange(self, mode, extra):
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        server = subprocess.Popen(
            [
                sys.executable,
                str(FILE),
                "server",
                "127.0.0.1",
                "--port",
                str(port),
                "--kind",
                "echo" if mode == "latency" else "sink",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            self.assertEqual(server.stdout.readline().strip(), "ready")
            client = subprocess.run(
                [
                    sys.executable,
                    str(FILE),
                    mode,
                    "127.0.0.1",
                    "--port",
                    str(port),
                    *extra,
                ],
                capture_output=True,
                text=True,
                timeout=20,
            )
            self.assertEqual(client.returncode, 0, client.stderr)
            self.assertEqual(server.wait(timeout=5), 0, server.stderr.read())
            return json.loads(client.stdout)
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=5)
            server.stdout.close()
            server.stderr.close()

    def test_echo_roundtrip_verifies_all_payloads(self):
        r = self.exchange("latency", ["--quick"])
        self.assertTrue(r["all_payloads_verified"])
        self.assertEqual([v["bytes"] for v in r["cases"]], list(probe.SIZES))
        self.assertTrue(all(v["n"] == 5 and len(v["rtt_ns"]) == 5 for v in r["cases"]))

    def test_bulk_exact_minimum_and_block_plus_tail(self):
        for n in (1, probe.BLOCK - 1, probe.BLOCK, probe.BLOCK + 1):
            with self.subTest(n=n):
                self.assertEqual(self.exchange("bulk", ["--bytes", str(n)])["bytes"], n)

    def test_invalid_bulk_bounds_fail_before_connect(self):
        for n in (-1, 0, probe.MAX_BYTES + 1):
            r = subprocess.run(
                [sys.executable, str(FILE), "bulk", "127.0.0.1", "--bytes", str(n)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(r.returncode, 2)


if __name__ == "__main__":
    unittest.main()
