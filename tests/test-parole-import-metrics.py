#!/usr/bin/python3 -B
"""Provider observations use real units and durable, per-source admission state."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _parole_import_metrics import LOOKUP, STARTUP, SUCCEEDED, THROUGHPUT, TOTAL, Transfer, WEEK, limited, observe, snapshot
from _parole_index import Index
from _parole_sources import controls, seed


class Measurements(unittest.TestCase):
    def setUp(self):
        base = Path(self.enterContext(TemporaryDirectory(prefix="parole-provider-metrics-")))
        self.root, self.state = base / "music", base / "state"
        self.root.mkdir()
        self.index = Index(self.root, self.state)

    def test_average_favors_recent_measurements_and_keeps_exact_counts(self):
        observe(self.index, "Internet Archive", {LOOKUP: 10, TOTAL: 30., SUCCEEDED: 1}, now=1000)
        observe(self.index, "Internet Archive", {LOOKUP: 20, TOTAL: 40., SUCCEEDED: 0}, now=1000)
        observe(self.index, "Jamendo", {LOOKUP: 3, SUCCEEDED: 1}, now=1000)
        saved = snapshot(Index(self.root, self.state), now=1000)
        archive = saved["Internet Archive"]
        self.assertEqual(archive[LOOKUP]["mean"], 12.5)
        self.assertEqual((archive[TOTAL]["samples"], archive[SUCCEEDED]["total"]), (2, 1))
        self.assertEqual(archive[SUCCEEDED]["mean"], .75)
        self.assertEqual(saved["Jamendo"][LOOKUP]["mean"], 3)
        observe(self.index, "Internet Archive", {LOOKUP: 2}, now=1000 + 28 * WEEK)
        self.assertLess(snapshot(self.index)["Internet Archive"][LOOKUP]["mean"], 2.1)
        observe(self.index, "Bandcamp", {LOOKUP: float("nan"), TOTAL: -1, SUCCEEDED: 3}, now=1000)
        self.assertNotIn("Bandcamp", snapshot(self.index))

    def test_actual_provider_pause_survives_restart_and_statistics_reset(self):
        seed(self.index)
        observe(self.index, "Internet Archive", {LOOKUP: 2}, now=1000)
        limited(self.index, "Internet Archive", 60, 429, now=1000)
        limited(self.index, "Internet Archive", 1, 503, now=1010)
        limited(self.index, "Jamendo", 100, 403, now=1000)
        reopened = Index(self.root, self.state)
        controls(reopened, "Internet Archive", "reset")
        extent = snapshot(reopened, now=1030)
        self.assertEqual(extent, {"Internet Archive": {"limited_until": 1060, "last_http": 429}})
        self.assertEqual(snapshot(reopened, now=1060), {})

    def test_throughput_has_no_startup_or_first_buffer_bytes(self):
        clock = Transfer(10.)
        self.assertEqual(clock.measurements(), {})
        clock.received(8192, 12.)
        self.assertEqual(clock.measurements(), {STARTUP: 2.})
        clock.received(2 * 1024**2 + 8192, 12.5)
        clock.received(4, 13.)
        self.assertEqual(clock.measurements(), {STARTUP: 2., THROUGHPUT: 4 * 1024**2})


if __name__ == "__main__":
    unittest.main()
