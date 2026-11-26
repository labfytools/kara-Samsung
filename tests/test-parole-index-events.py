#!/usr/bin/python3 -B
"""Exercise the production inventory and its durable delivery transaction."""
from multiprocessing import get_context
import os
from pathlib import Path
from subprocess import CalledProcessError
import sys
from tempfile import TemporaryDirectory
from time import monotonic
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _parole_index import ARTWORK, MEDIA, PREPARATION, REMOVE, SIDECAR, Index, inventory, revision
from _parole_index_watch import EVENT, OVERFLOW, Watch


class Delivery(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.root = self.base / "music"
        self.path = self.root / "Artiste" / "Disque" / "01.flac"
        self.path.parent.mkdir(parents=True)
        self.path.write_bytes(b"version originale")
        self.index = Index(self.root, self.base / "state")
        self.tags = dict(title="Un", artist="Artiste, Invité", albumartist="Artiste",
                         album="Disque", track="1", duration_seconds=40, codec="flac")

    def notify(self, mask=MEDIA, *, proof=False, path=None, **facts):
        path = path or self.path
        proof = {str(path): {"token": revision(path.stat()), "tags": self.tags, **facts}} if proof else None
        self.index.notify([path], mask, proofs=proof, now=1)

    def take(self, **kwargs):
        size, events = self.index.events(3)
        self.assertGreater(size, 0)
        return inventory(self.index, None, kwargs.pop("cache", lambda *_: ("", "absent", {})),
                         kwargs.pop("stop", lambda: False), lambda *_: None, events=events,
                         reader=kwargs.pop("reader", lambda _: self.fail("FFprobe inattendu")), **kwargs)

    def rows(self):
        with self.index.db() as db:
            return db.execute("SELECT * FROM songs ORDER BY path").fetchall()

    def test_coalescing_restart_and_exact_receipt(self):
        self.notify(proof=True)
        self.notify(SIDECAR)
        self.notify(ARTWORK)
        self.path.with_suffix(".lrc").write_text("[00:12.00]Une ligne\n")
        (self.path.parent / "cover.jpg").write_bytes(b"fixture")
        self.index = Index(self.root, self.base / "state")
        result = self.take()
        self.assertEqual((result["transactions"], result["delivered"], result["combined"], result["receipts"]),
                         (1, 1, 2, 1))
        row, = self.rows()
        self.assertEqual((row["title"], row["artist"], row["owner"], row["lrc"]), ("Un", "Artiste, Invité", "Artiste", "local"))
        self.notify(proof=True)
        self.assertEqual(self.take()["changed"], 0)
        self.assertEqual(self.index.events(3), (0, []))

    def test_new_event_during_processing_keeps_its_serial(self):
        self.notify(proof=True)

        def update_during_inspection(*_):
            self.notify(PREPARATION)
            return "", "absent", {}

        self.assertEqual(self.take(cache=update_during_inspection)["delivered"], 0)
        self.assertEqual(self.take()["delivered"], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_process_death_between_song_and_ack(self):
        self.notify(proof=True)

        def fail(_db, _paths):
            os._exit(7)

        worker = get_context("fork").Process(target=self.take, kwargs={"plan_events": fail})
        worker.start()
        worker.join(5)
        if worker.is_alive():
            worker.kill()
            worker.join()
            self.fail("Le processus de fixture n’a pas atteint la transaction")
        self.assertEqual(worker.exitcode, 7)
        self.assertEqual(self.rows(), [])
        self.index = Index(self.root, self.base / "state")
        self.assertEqual(self.take()["delivered"], 1)
        self.assertEqual(len(self.rows()), 1)
        with self.index.db() as db:
            self.assertEqual(db.execute("PRAGMA quick_check").fetchone()[0], "ok")

    def test_changed_file_invalidates_receipt(self):
        self.notify(proof=True)
        self.path.write_bytes("version modifiée à deux".encode())
        result = self.take(reader=lambda _: ({**self.tags, "title": "Deux"}, []))
        self.assertEqual((result["receipts"], result["probes"]), (0, 1))
        self.assertEqual(self.rows()[0]["title"], "Deux")

    def test_file_modified_during_read_is_retried(self):
        self.notify()

        def concurrent_write(_):
            self.path.write_bytes("Écriture concurrente".encode())
            return self.tags, []

        self.assertEqual(self.take(reader=concurrent_write)["delivered"], 0)
        self.assertEqual(self.rows(), [])
        with self.index.db() as db:
            self.assertEqual(db.execute("SELECT attempts FROM index_events").fetchone()[0], 1)

    def test_corrupt_media_keeps_one_error_and_a_retry(self):
        self.notify()

        def corrupt(_):
            raise CalledProcessError(1, "ffprobe")

        result = self.take(reader=corrupt)
        self.assertEqual((result["errors"], result["delivered"]), (1, 0))
        self.assertEqual(len(self.rows()), 1)
        self.notify(proof=True)
        self.assertEqual(self.take()["delivered"], 1)
        self.assertEqual(len(self.rows()), 1)

    def test_move_keeps_previous_path_and_authorized_delete(self):
        self.notify(proof=True)
        self.take()
        old, = self.rows()
        dest = self.path.with_name("02.flac")
        self.path.rename(dest)
        self.notify(REMOVE)
        self.notify(path=dest, proof=True)
        self.assertEqual(self.take()["delivered"], 2)
        moved, = self.rows()
        self.assertEqual((moved["id"], moved["previous"]), (old["id"], str(self.path)))
        dest.unlink()
        self.notify(REMOVE, path=dest)
        self.assertEqual(self.take()["missing"], 1)
        self.assertEqual(self.rows(), [])

    def test_sidecar_only_and_kara_only_use_current_versions(self):
        self.notify(proof=True)
        self.take()
        self.path.with_suffix(".lrc").write_text("[00:12.00]Une ligne\n")
        self.notify(SIDECAR)
        with patch("_parole_index.collection_reader", side_effect=AssertionError("lecture Strawberry")):
            self.assertEqual(self.take()["phase_calls"]["sidecar_read"], 1)
            self.notify(PREPARATION)
            with patch("_parole_index.local_state", side_effect=AssertionError("lecture du LRC")):
                self.assertEqual(self.take(cache=lambda *_: ("nouveau", "prêt", {}))["changed"], 1)
        row, = self.rows()
        self.assertEqual((row["kara"], row["lrc"]), ("prêt", "local"))

    def test_inotify_delivers_external_files_and_directory_rename(self):
        self.notify(proof=True)
        self.take()
        watch = Watch(self.index, limit=32)
        self.addCleanup(watch.close)
        self.assertEqual(watch.poll(), ({}, False))
        self.assertIsNotNone(watch.fd)
        self.path.with_suffix(".lrc").write_text("[00:12.00]Nouvelle ligne\n")
        new = self.path.parent.with_name("Renommé")
        self.path.parent.rename(new)
        changed, gap = watch.poll()
        self.assertFalse(gap)
        self.assertEqual(changed[self.path] & (SIDECAR | REMOVE), SIDECAR | REMOVE)
        self.assertEqual(changed[new / self.path.name], MEDIA)
        (new / "02.flac").write_bytes(b"nouveau fichier")
        self.assertTrue(watch.poll()[0][new / "02.flac"] & MEDIA)

    def test_inotify_overflow_and_watch_limit_report_gap(self):
        watch = Watch(self.index, limit=32)
        self.addCleanup(watch.close)
        watch.start()
        with patch("_parole_index_watch.os.read", return_value=EVENT.pack(-1, OVERFLOW, 0, 0)):
            self.assertEqual(watch.poll(), ({}, True))
        self.assertIsNone(watch.fd)
        self.assertGreater(watch.next_start, monotonic())
        limited = Watch(self.index, limit=1)
        self.assertEqual(limited.poll(), ({}, True))
        self.assertIsNone(limited.fd)

    def test_dimension_counts_follow_song_and_artist_changes(self):
        self.notify(proof=True)
        second = self.path.with_name("02.flac")
        second.write_bytes(b"autre piste")
        self.notify(path=second, proof=True)
        self.take()
        with self.index.db() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM library_artists").fetchone()), ("Artiste", 2))
            self.assertEqual(db.execute("SELECT count(*) FROM library_editions").fetchone()[0], 1)
            db.execute("UPDATE songs SET owner='Rattaché', edition='autre' WHERE path=?", (str(second),))
        self.assertTrue(self.index.audit_dimensions())
        second.unlink()
        self.notify(REMOVE, path=second)
        self.take()
        self.assertTrue(self.index.audit_dimensions())
        with self.index.db() as db:
            self.assertEqual(tuple(db.execute("SELECT * FROM library_artists").fetchone()), ("Artiste", 1))


if __name__ == "__main__":
    unittest.main()
