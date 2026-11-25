#!/usr/bin/python3 -B
"""Exerce les renommages réels et la conservation des reçus."""
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_import_queue import ImportQueue
from _musique_oz import album_group, grouped_path
from _parole_artist_groups import apply, last_group, preview, recover, undo
from _parole_index import Index, inventory


class ArtistGroups(unittest.TestCase):
    def setUp(self):
        self.base = Path(self.enterContext(TemporaryDirectory()))
        self.root = self.base / "music"
        self.state = self.base / "state"
        self.target = self.root / "Bob Marley"
        self.source = self.root / "Bob Marley & The Wailers" / "Exodus"
        self.source.mkdir(parents=True)
        (self.target / "Exodus").mkdir(parents=True)
        self.track = self.source / "a.opus"
        self.track.write_bytes(b"original audio")
        self.track.with_suffix(".lrc").write_bytes(b"original lyric")
        (self.source / "cover.jpg").write_bytes(b"original cover")
        (self.target / "Exodus/a.opus").write_bytes(b"different edition")
        self.index = Index(self.root, self.state)
        self.queue = ImportQueue(self.state, self.root)
        self.url = "https://example.test/exodus"
        self.job = self.queue.enqueue([("Exodus", self.url)])
        self.queue.album(self.job, {"url": self.url, "status": "partial", "tracks": [str(self.track)],
                                    "provenance": [{"path": str(self.track)}]})

    def test_keeps_editions_and_discography_receipts_and_allows_undo(self):
        expected = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        other = "ffffffff-bbbb-cccc-dddd-eeeeeeeeeeee"
        inventory(self.index, None, lambda *_: ("", "absent", {}), lambda: False, lambda _: None,
                  paths=[self.track], reader=lambda _: ({"title": "Exodus", "artist": self.source.parent.name,
                                                         "album": "Exodus", "musicbrainzalbumartistid": expected}, []))
        plan = preview(self.root, self.target.name, [self.source.parent.name, self.target.name])
        self.assertTrue(plan["moves"][0]["collision"])
        inode = self.track.stat().st_ino
        destination = self.root / plan["moves"][0]["destination"] / self.track.name
        receipt = apply(self.index, plan, self.queue, state=self.state)
        self.assertEqual(destination.stat().st_ino, inode)
        self.assertEqual(destination.with_suffix(".lrc").read_bytes(), b"original lyric")
        self.assertEqual((destination.parent / "cover.jpg").read_bytes(), b"original cover")
        self.assertEqual((self.target / "Exodus/a.opus").read_bytes(), b"different edition")
        self.assertEqual(self.queue.outcomes(self.job)[self.url]["tracks"], [str(destination)])
        self.assertEqual(grouped_path(self.root, self.track, database=self.index.path), destination)
        self.assertEqual(album_group("Bob Marley and The Wailers", "Exodus", database=self.index.path), "Bob Marley")
        self.assertIsNone(album_group("Bob Marley and The Wailers", "Autre édition", database=self.index.path))
        self.assertIsNone(album_group("The Wailers", "Exodus", database=self.index.path))
        self.assertIsNone(album_group("Bob Marley and The Wailers", "Exodus", database=self.index.path, artist_id=other))
        self.assertEqual(album_group("Bob Marley and The Wailers", "Exodus", database=self.index.path,
                                    artist_id=expected), "Bob Marley")
        self.assertEqual(last_group(self.index)[0], receipt)
        undo(self.index, receipt, self.queue, state=self.state)
        self.assertEqual(self.track.stat().st_ino, inode)
        self.assertEqual(grouped_path(self.root, destination, database=self.index.path), self.track)
        self.assertEqual(self.queue.outcomes(self.job)[self.url]["tracks"], [str(self.track)])
        self.assertIsNone(last_group(self.index))
        self.assertEqual(self.queue.settings(), (False, 4))

    def test_replays_the_same_rename_after_interruption(self):
        plan = preview(self.root, self.target.name, [self.source.parent.name])
        with patch("_parole_artist_groups.persist_paths", side_effect=OSError("interruption")):
            with self.assertRaises(OSError):
                apply(self.index, plan, self.queue, state=self.state)
        self.assertEqual(self.queue.settings(), (True, 4))
        self.assertEqual(recover(Index(self.root, self.state), self.queue, state=self.state), 1)
        self.assertEqual(recover(self.index, self.queue, state=self.state), 0)
        self.assertEqual(self.queue.settings(), (False, 4))
        destination = self.root / plan["moves"][0]["destination"] / self.track.name
        self.assertEqual(self.queue.outcomes(self.job)[self.url]["tracks"], [str(destination)])


if __name__ == "__main__":
    unittest.main()
