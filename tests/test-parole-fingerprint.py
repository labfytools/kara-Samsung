#!/usr/bin/python3 -B
"""Bounded Chromaprint comparisons keep acoustics separate from edition confidence."""
from pathlib import Path
from random import Random
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _parole_fingerprint import ALGORITHM, VERSION, Fingerprint, parse, similarity
from _parole_index import Index
from _parole_music_identity import CONFIRMED, PROBABLE
from _parole_music_registry import acoustic_evidence, possible_duplicates, store_import, stored_fingerprint


class FingerprintTests(unittest.TestCase):
    def setUp(self):
        source = Random(7251)
        self.values = [source.getrandbits(32) for _ in range(100)]
        self.other = [source.getrandbits(32) for _ in range(100)]
        self.original = Fingerprint(tuple(self.values), 12500)

    def test_parser_rejects_short_malformed_and_uninformative_patterns(self):
        raw = {"version": VERSION, "algorithm": ALGORITHM, "millis": 12500, "words": self.values}
        self.assertEqual(parse(raw), self.original)
        for invalid in (raw | {"words": self.values[:20]}, raw | {"words": [1] * 100},
                        raw | {"words": [2**32, *self.values]}, raw | {"millis": 120001},
                        raw | {"algorithm": 1}):
            self.assertIsNone(parse(invalid))

    def test_similarity_allows_small_offsets_and_rejects_incomparable_prefixes(self):
        shifted = Fingerprint(tuple(self.other[:4] + self.values[:-4]), 12500)
        self.assertGreater(similarity(self.original, shifted), .99)
        independent = Fingerprint(tuple(self.other), 12500)
        self.assertLess(similarity(self.original, independent), .6)
        self.assertIsNone(similarity(self.original, Fingerprint(tuple(self.values), 60000)))

    def test_registry_keeps_fingerprints_complementary(self):
        base = Path(self.enterContext(TemporaryDirectory(prefix="parole-fingerprint-")))
        root = base / "library"
        target = root / "Artiste" / "Edition" / "Titre.flac"
        target.parent.mkdir(parents=True)
        target.touch()
        index = Index(root, base / "state")
        tags = {"title": "Titre", "artist": "Artiste", "album": "Edition", "track": "1",
                "duration_seconds": 125.}
        old = {"path": str(target), "provider": "Internet Archive", "external_id": "3500", "tags": tags,
               "pcm": "a"*64, "sha256": "b"*64}
        store_import(index, [old], fingerprints={old["pcm"]: self.original.document()})
        self.assertEqual(stored_fingerprint(index, old["pcm"]), self.original.document())
        incoming = {"pcm": "c"*64, "sha256": "d"*64, "tags": dict(tags), "fingerprint": self.original.document()}
        self.assertEqual(acoustic_evidence(index, incoming)[0]["confiance"], PROBABLE)
        self.assertEqual(list(possible_duplicates(index, incoming)), [])
        # Recording metadata can corroborate the audio clue. The change of
        # PCM remains consequential for audio and cache publication.
        old["tags"]["isrc"] = "FRABC1900001"
        incoming["tags"]["isrc"] = old["tags"]["isrc"]
        store_import(index, [old])
        self.assertEqual(acoustic_evidence(index, incoming)[0]["confiance"], CONFIRMED)
        self.assertEqual(list(possible_duplicates(index, incoming)), [])


if __name__ == "__main__":
    unittest.main()
