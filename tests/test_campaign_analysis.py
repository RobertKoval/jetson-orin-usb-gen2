import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bench"))
from analyze_campaign import schedule, select_rows, loadavg_range


def records(count):
    return [
        dict(
            pair=p,
            generation=g,
            transport=t,
            direction=d,
            label=l,
            result={"kind": k},
            valid=True,
        )
        for p, g, t, d, l, k in schedule()[:count]
    ]


class CoverageTests(unittest.TestCase):
    def test_load_range_includes_both_endpoints(self):
        rows = [
            {"context": {"host": {"loadavg_before": [8], "loadavg_after": [2]}}},
            {"context": {"host": {"loadavg_before": [6], "loadavg_after": [4]}}},
        ]
        self.assertEqual(loadavg_range(rows, "host"), [2, 8])

    def test_complete_requires_every_scheduled_record(self):
        manifest = {"complete": True, "restored": True}
        self.assertEqual(
            select_rows(manifest, records(128))[1]["complete_pairs"], list(range(1, 7))
        )
        for n in (127, 129):
            rows = records(128) if n == 129 else records(n)
            if n == 129:
                rows.append(rows[-1])
            with self.assertRaises(ValueError):
                select_rows(manifest, rows)

    def test_partial_is_explicit_and_keeps_failure_visible(self):
        manifest = {"complete": False, "restored": True}
        rows = records(74) + [
            {"valid": False, "pair": 4, "generation": 2, "label": "under-ncm-load"}
        ]
        with self.assertRaises(ValueError):
            select_rows(manifest, rows)
        selected, coverage = select_rows(manifest, rows, True)
        self.assertEqual(len(selected), 64)
        self.assertEqual(coverage["complete_pairs"], [1, 2, 3])
        self.assertEqual(coverage["unpaired_valid_records"], 10)
        self.assertEqual(coverage["failed_records"], rows[-1:])

    def test_pair_completion_boundary(self):
        manifest = {"complete": False, "restored": True}
        with self.assertRaises(ValueError):
            select_rows(manifest, records(23), True)
        for n in (24, 25):
            selected, coverage = select_rows(manifest, records(n), True)
            self.assertEqual(len(selected), 24)
            self.assertEqual(coverage["unpaired_valid_records"], n - 24)

    def test_rejects_missing_duplicate_reordered_or_post_failure_rows(self):
        manifest = {"complete": False, "restored": True}
        base = records(44)
        variants = [
            base[:3] + base[4:],
            base[:3] + [base[2]] + base[3:],
            [base[1], base[0]] + base[2:],
            base[:24] + [{"valid": False}] + base[24:],
        ]
        for rows in variants:
            with self.assertRaises(ValueError):
                select_rows(manifest, rows, True)

    def test_rejects_smoke_or_failed_restoration(self):
        for manifest in (
            {"complete": True, "restored": False},
            {"complete": True, "restored": True, "smoke": True},
        ):
            with self.assertRaises(ValueError):
                select_rows(manifest, records(128), True)


if __name__ == "__main__":
    unittest.main()
