#!/usr/bin/python3 -B
"""Edition matching fixtures include deliberate lookalikes and partial releases."""
from dataclasses import replace
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _parole_music_identity import (CONFIRMED, DIFFERENT, PROBABLE, barcode, compare, display_credit, from_catalogue,
                                    normalized, position, same_track)

RELEASE = "b90181d2-ff52-4bfb-aeb5-f8c66c8fed25"
RELEASE_OTHER = "44c56580-d9f8-4d98-a27c-2465297c9cae"
GROUP = "93a25337-99ba-4cc1-bf09-1129f9c3fe78"
ARTIST = "a346160d-be2e-4810-8428-f740d4b10513"
RECORDING_ONE = "ab566c51-4031-4b30-a301-d8df8c4259dc"
RECORDING_TWO = "29478547-a135-4a1b-bc9b-76c8720c9771"


def edition(**changes):
    source = {
        "title": "D’été à l’automne", "artist": "André d’Avril", "year": "2019-06-12",
        "artist_id": ARTIST, "group_id": GROUP, "release_id": RELEASE, "barcode": "036000291452",
        "discs": 1, "count": 2,
        "tracks": [
            {"disc": 1, "number": "01/02", "title": "Où vas-tu ? (feat. Léa)", "duration": 210.1, "isrc": "FR-ABC-19-00001",
             "recording_id": RECORDING_ONE, "pcm": "a" * 64},
            {"disc": 1, "number": 2, "title": "Noël d’hiver", "duration": 175, "recording_id": RECORDING_TWO},
        ],
    }
    source.update(changes)
    return from_catalogue(source)


class IdentityTests(unittest.TestCase):
    def test_normalization_does_not_change_originals_or_remove_version_names(self):
        self.assertEqual(normalized("  D’ÉTÉ – À l'automne\u00a0"), "dete a lautomne")
        self.assertEqual(display_credit("Où vas-tu (feat. Léa)"), display_credit("OU VAS TU [FEATURING Lea]"))
        for marker in ("Deluxe", "Live", "Remix", "Acoustic", "Instrumental", "Remastered"):
            self.assertIn(marker.casefold(), normalized("Album " + marker))
        self.assertEqual(position("03/12"), 3)
        self.assertEqual(position("1.2"), 0)
        self.assertEqual(barcode("036000291452"), barcode("0036000291452"))
        self.assertEqual(barcode("036000291451"), "")

    def test_edition_and_recordings_can_agree_despite_credit_presentation_and_timing_noise(self):
        original = edition()
        rendered = edition(title="D'ete a l’automne", artist="ANDRE D'AVRIL", barcode="0036000291452")
        first = replace(rendered.tracks[0], title="OU VAS TU [FEATURING Lea]", millis=210850, pcm="b" * 64)
        other = replace(rendered, tracks=(first, rendered.tracks[1]))
        verdict = compare(original, other)
        self.assertEqual((verdict.confidence, verdict.positions), (CONFIRMED, ((1, 1), (1, 2))))
        self.assertEqual(same_track(original.tracks[0], first), CONFIRMED)
        self.assertEqual(original.tracks[0].title, "Où vas-tu ? (feat. Léa)")

    def test_confirmed_release_can_have_a_missing_local_track(self):
        complete = edition()
        local = replace(complete, discs=0, count=0, tracks=(complete.tracks[1],))
        result = compare(local, complete)
        self.assertEqual((result.confidence, result.positions), (CONFIRMED, ((1, 2),)))
        self.assertEqual(compare(replace(local, release_id=""), complete).confidence, PROBABLE)

    def test_group_release_or_fingerprint_alone_is_insufficient(self):
        original = edition()
        for other in (
            replace(original, tracks=(), barcode=""),
            replace(original, release_id="", barcode="", tracks=()),
            replace(original, artist_id="", release_id="", barcode="", tracks=original.tracks),
        ):
            self.assertEqual(compare(original, other).confidence, PROBABLE)

    def test_distinct_editions_and_recordings_never_confirm(self):
        original = edition()
        wrong_track = replace(original.tracks[0], title="Version radio", work=original.tracks[0].work)
        other_artist = "17df1819-acfc-4e31-a504-f1b0fd916226"
        variant = [replace(original, title=original.title + " " + label)
                   for label in ("Deluxe", "Live", "Remix", "Acoustic", "Instrumental", "Remastered")]
        variant += [
            replace(original, release_id=RELEASE_OTHER),
            replace(original, artist="Autre interprète", artist_id=other_artist),
            replace(original, count=3),
            replace(original, discs=2),
            replace(original, year=2025),
            replace(original, tracks=(wrong_track, original.tracks[1])),
            replace(original, tracks=(replace(original.tracks[0], millis=212700), original.tracks[1])),
            replace(original, tracks=(replace(original.tracks[0], recording=RECORDING_TWO), original.tracks[1])),
            replace(original, tracks=(replace(original.tracks[0], title="Où vas-tu ? (feat. Noé)"), original.tracks[1])),
            replace(original, tracks=(replace(original.tracks[0], number=2), replace(original.tracks[1], number=1))),
        ]
        verdicts = [compare(original, candidate).confidence for candidate in variant]
        self.assertEqual(verdicts.count(CONFIRMED), 0)
        self.assertEqual(verdicts.count(DIFFERENT), 16)

    def test_alias_requires_manual_evidence_and_artist_id_conflicts_take_precedence(self):
        original = replace(edition(), artist_id="")
        alias = replace(original, artist="Avril André")
        self.assertEqual(compare(original, alias).confidence, DIFFERENT)
        self.assertEqual(compare(original, alias, [("André d'Avril", "Avril André")]).confidence, CONFIRMED)
        self.assertEqual(compare(edition(), replace(alias, artist_id=RECORDING_ONE),
                                 [("André d'Avril", "Avril André")]).confidence, DIFFERENT)


if __name__ == "__main__":
    unittest.main()
