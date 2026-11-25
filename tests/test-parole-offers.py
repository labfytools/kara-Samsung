#!/usr/bin/python3 -B
"""Speed strategy and acquisition plan safety."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _parole_import_metrics import STARTUP, SUCCEEDED, THROUGHPUT, TOTAL, WEEK, limited, observe
from _parole_import_providers import ARCHIVE, JAMENDO
from _parole_index import Index
from _parole_offer import create_plan, load_plan, plan_id, rank
from _parole_sources import seed

MIB = 1024**2


def record(provider):
    return {"provider": provider, "price": 0, "formats": ["FLAC"] if provider == ARCHIVE else ["MP3"],
            "bytes": 20 * MIB, "url": ("https://archive.org/details/album" if provider == ARCHIVE else
                                       "https://www.jamendo.com/album/48123"),
            "facts": {"artist": "Atelier", "title": "Album", "count": 2, "discs": 1,
                      "release_id": "b90181d2-ff52-4bfb-aeb5-f8c66c8fed25",
                      "tracks": [{"title": "Piste A", "disc": 1, "number": 1, "duration": 120,
                                  "isrc": "FRABC1900001"},
                                 {"title": "Piste B", "disc": 1, "number": 2, "duration": 125,
                                  "isrc": "FRABC1900002"}]}}


class OfferTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="parole-offer-")))
        self.index = Index(root, root / "state")
        seed(self.index)
        self.offers = [record(ARCHIVE), record(JAMENDO)]

    def test_complete_authorized_confirmed_plan_is_durable(self):
        uri = create_plan(self.index, self.offers)
        self.assertEqual(uri, create_plan(self.index, self.offers[::-1]))
        refreshed = [self.offers[0] | {"bytes": 21 * MIB}, self.offers[1] | {"url": "https://www.jamendo.com/album/48123/album"}]
        self.assertEqual(uri, create_plan(self.index, refreshed))
        fresh = Index(self.index.root, self.index.folder.parents[2])
        self.assertEqual(load_plan(fresh, plan_id(uri)), self.offers)
        for edits in ({"price": 3}, {"url": "https://www.jamendo.com/album/48123?token=private"},
                      {"facts": self.offers[1]["facts"] | {"tracks": []}},
                      {"facts": self.offers[1]["facts"] | {"title": "Album Deluxe"}}):
            with self.subTest(edits=edits), self.assertRaises(ValueError):
                create_plan(self.index, [self.offers[0], self.offers[1] | edits])

    def test_speed_quality_preference_and_provider_cooldown(self):
        now = 1000
        observe(self.index, ARCHIVE, {STARTUP: 1., THROUGHPUT: MIB, TOTAL: 23., SUCCEEDED: 1}, now=now)
        observe(self.index, JAMENDO, {STARTUP: .5, THROUGHPUT: 4 * MIB, TOTAL: 7., SUCCEEDED: 1}, now=now)
        choices = rank(self.index, self.offers, now=now)
        self.assertEqual((choices[0].offer["provider"], choices[0].estimate), (JAMENDO, 7))
        self.assertEqual(rank(self.index, self.offers, remaining=1, now=now)[0].estimate, 3.5)
        self.index.configure(import_strategy="quality")
        self.assertEqual(rank(self.index, self.offers, now=now)[0].offer["provider"], ARCHIVE)
        self.index.configure(import_strategy="preferred", import_preferred=ARCHIVE)
        self.assertEqual(rank(self.index, self.offers, now=now)[0].reason, "fournisseur préféré disponible")
        limited(self.index, ARCHIVE, 120, 429, now=now)
        self.assertEqual([choice.offer["provider"] for choice in rank(self.index, self.offers, now=now)], [JAMENDO])
        self.assertEqual(rank(self.index, self.offers, now=now + 120)[0].offer["provider"], ARCHIVE)

    def test_new_source_can_be_tried_without_fabricating_a_rate(self):
        for _ in range(5):
            observe(self.index, ARCHIVE, {TOTAL: 6., SUCCEEDED: 1}, now=1000)
        chosen = rank(self.index, self.offers, now=1000)[0]
        self.assertEqual((chosen.offer["provider"], chosen.estimate, chosen.throughput), (JAMENDO, None, None))
        self.assertTrue(chosen.reason.startswith("premier transfert"))
        observe(self.index, JAMENDO, {TOTAL: 20., SUCCEEDED: 1}, now=1000)
        self.assertEqual(rank(self.index, self.offers, now=1000)[0].offer["provider"], ARCHIVE)
        later = 1000 + 12 * WEEK
        observe(self.index, JAMENDO, {TOTAL: 3., SUCCEEDED: 1}, now=later)
        self.assertEqual(rank(self.index, self.offers, now=later)[0].offer["provider"], JAMENDO)


if __name__ == "__main__":
    unittest.main()
