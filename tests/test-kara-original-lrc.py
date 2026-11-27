#!/usr/bin/env python3
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_lrc import read_source
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
            self.assertEqual(read_source(song)[2], rows)
            self.assertEqual(lrc.read_bytes(), original)

if __name__ == "__main__":
    unittest.main()
