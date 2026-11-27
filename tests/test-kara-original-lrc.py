#!/usr/bin/env python3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_game import GameScore
from _kara_lrc_game import read_source, score_source
from _kara_scoring import score_breakdown, score_reference_line
from _paroles_lrc import playback_lrc


class OriginalLRCTests(unittest.TestCase):
    def test_preserves_words_clock_and_written_characters(self):
        text = ("[offset:+100]\r\n"
                "[00:01.220][00:08.00]<00:01.220> E\u0301lan <00:01.999>aujourd’hui<00:02.100>\r\n"
                "[00:04]ligne seule\r\n")
        rows = playback_lrc(text)
        self.assertEqual(rows, [
            (1.12, " E\u0301lan aujourd’hui", ((1.12, 1, 6), (1.899, 7, 18))),
            (3.9, "ligne seule", ()),
            (7.9, " E\u0301lan aujourd’hui", ()),
        ])
        with TemporaryDirectory() as tmp:
            song = Path(tmp) / "title.opus"
            lrc = song.with_suffix(".lrc")
            original = b"\xef\xbb\xbf" + text.encode()
            lrc.write_bytes(original)
            score = score_source(read_source(song), 10)
            self.assertEqual(score["lines"][0]["words"][0]["start"], 1.12)
            self.assertIsNone(score["lines"][1]["words"][0]["start"])
            self.assertEqual(lrc.read_bytes(), original)

    def test_line_only_is_lexical_and_schedules_turbo_without_word_inference(self):
        source = ("[00:01]bonjour", "fixture", playback_lrc("[00:01]bonjour"))
        reference = score_source(source, 3.)
        line = reference["lines"][0]
        heard = [{"word": "bonjour", "start": 1.5, "confidence": .9}]
        decisions = score_reference_line(line, heard, lexical_only_allowed=True)
        score = score_breakdown(reference, {0: decisions})
        self.assertEqual((score["score"], score["lyrics"], score["timing"]), (100, 100, None))
        game = GameScore(Mock())
        game.set_round(["bonjour"], reference=reference)
        game.ready = True
        game.send_job = Mock(return_value=True)
        game.tick_round(*game.reference_clock[:1], 3., 3., 1., 100., *game.reference_clock[1:])
        job = game.send_job.call_args.args[0]
        self.assertEqual((job.line, job.word_times), (0, (None,)))


if __name__ == "__main__":
    unittest.main()
