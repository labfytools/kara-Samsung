#!/usr/bin/python3 -B
"""Provider observations use real units and durable, per-source admission state."""
from pathlib import Path
import os
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _parole_import_metrics import LOOKUP, STARTUP, SUCCEEDED, THROUGHPUT, TOTAL, Transfer, WEEK, limited, observe, snapshot
from _parole_index import Index
from _kara_import_result import ImportIssue
import _kara_import_resources as resources
import _parole_jamendo as jamendo
from _parole_lyrics_lookup import backoff, contexts, provider_profiles, record_profile
from _parole_sources import audio_policies, configure_limits, controls, seed


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

    def test_local_quota_and_jamendo_api_limit_have_distinct_origins(self):
        seed(self.index)
        configure_limits(self.index, "Internet Archive", 1, 5)
        self.assertTrue(self.index.reserve_daily("provider:Internet Archive", 1))
        with patch.dict(os.environ, KARA_JAMENDO_CLIENT_ID="fixtureid"), patch.object(
                resources, "RATE_DIRECTORY", self.state / "rates"), patch.object(jamendo, "fetch", return_value={
                    "headers": {"status": "failed", "code": 6}}):
            with self.assertRaisesRegex(ImportIssue, "code API 6"):
                jamendo.api_results(self.index, "albums")
            self.assertGreater(resources.service_delay("api.jamendo.com"), 110)
            resources.defer_service("api.jamendo.com", 2)
            self.assertGreater(resources.service_delay("api.jamendo.com"), 110)
        policies = audio_policies(self.index)
        archive = policies["Internet Archive"]
        self.assertEqual((archive["daily"], archive["used"], archive["interval"], archive["hold"]), (1, 1, 5, 0))
        self.assertEqual(policies["Jamendo"]["status"], 6)

    def test_audio_body_events_exclude_setup_and_finish_once(self):
        events = []
        notify = lambda event, **fields: events.append((event, fields))
        transfer = Transfer(10., notify)
        transfer.received(0, 11.)
        self.assertEqual(events, [])
        transfer.received(8192, 11.)
        transfer.received(16384, 11.2)
        transfer.end()
        transfer.end()
        self.assertEqual(events, [("audio-first-byte", {}), ("audio-bytes", {"size": 8192}),
                                  ("audio-bytes", {"size": 8192}), ("audio-transfer-ended", {})])
        Transfer(12., notify).end()
        self.assertEqual(len(events), 4)

    def test_lyrics_profiles_use_observed_context_and_form(self):
        tags = contexts({"language": "fr-FR", "genre": "Rap"})
        self.assertEqual(contexts({}), ())
        for _ in range(4):
            record_profile(self.index, "LRCLIB", False, tags, 2., True)
        self.assertEqual(provider_profiles(self.index, ["LRCLIB"], False, tags), {})
        record_profile(self.index, "LRCLIB", False, tags, 4., False)
        reopened = Index(self.root, self.state)
        saved = provider_profiles(reopened, ["LRCLIB"], False, tags)["LRCLIB"]
        self.assertEqual(len(saved), 2)
        self.assertTrue(all(sample["observations"] == 5 and sample["rate"] == .8 for sample in saved))
        self.assertEqual(provider_profiles(reopened, ["LRCLIB"], True, tags), {})
        self.assertEqual(provider_profiles(reopened, ["LRCLIB"], False, contexts({"language": "es"})), {})

    def test_http_cooldown_grows_and_respects_the_providers_delay(self):
        self.assertEqual(backoff(self.index, "LRCLIB", 1.), 5.)
        self.assertEqual(backoff(Index(self.root, self.state), "LRCLIB", 2.), 10.)
        self.assertEqual(backoff(self.index, "LRCLIB", 123.), 123.)
        self.assertEqual(backoff(self.index, "NetEase", 1.), 5.)


if __name__ == "__main__":
    unittest.main()
