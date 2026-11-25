#!/usr/bin/python3 -B
"""Validation de la reprise diagnostiquée sur une file SQLite isolée."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from _kara_import_queue import ImportQueue
from _parole_codex import validate
from _parole_import_diagnostics import ImportDiagnostics, classify, rendition, signatures
from _parole_index import Index
from _parole_maintenance import Journal

ID = "OLAK5uy_exampleFixture"
PLAYLIST = f"https://www.youtube.com/playlist?list={ID}"
ALIAS = f"https://music.youtube.com/playlist?list={ID}"
OTHER = "https://music.youtube.com/playlist?list=OLAK5uy_otherFixture"
VERSIONS = {"yt-dlp": "2026.11.10", "ffmpeg": "8.2.0"}


class Tests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.index = Index(self.root / "music", self.root / "state")
        self.queue = ImportQueue(self.root / "state", self.root / "music")
        self.journal = Journal(self.index.folder)
        self.addCleanup(self.journal.close)
        self.controller = SimpleNamespace(index=self.index, journal=self.journal, revision=0, output=self.web_catalogue)
        self.recovery = ImportDiagnostics(self.controller, self.queue, versions=lambda: VERSIONS)
        self.settings = {**self.index.settings(), "codex": True, "codex_auto": True}

    def web_catalogue(self, command, timeout):
        self.assertIn(ALIAS, command)
        return 0, b'{"data":{"title":"Album","tracks":[{"id":"abcdefghijk"}]}}'

    def fail(self, url, issue, job=None, *, tracks=None, interrupted=False):
        if job is None:
            job = self.queue.enqueue([("Album d’essai", url)])
        self.assertTrue(self.queue.claim(job))
        tracks = tracks or []
        self.queue.album(job, {
            "url": url, "title": "Album d’essai", "artist": "Groupe",
            "status": "partial" if tracks else "failure", "tracks": tracks,
            "audio_complete": False, "added": 0, "diagnostics": [issue],
        })
        self.queue.finish(job, "failure", "Échec", {"interrupted": interrupted})
        current = self.queue.job(job)
        self.queue.refreshed([(job, current["refresh"])])
        return job

    def tick(self, now):
        self.recovery.tick(self.settings, now)

    def dumped_task(self):
        with self.index.db() as db:
            return db.execute("SELECT subject,payload FROM tasks WHERE kind='codex'").fetchall()

    def result(self, subject, url=ALIAS):
        return {"research_id": subject, "identification": "Album", "status": "found", "sources": [
            {"url": "https://music.youtube.com/", "documentation": "", "terms": "", "category": "album", "adapter": "none"}],
            "missing": [], "matches": [], "contradictions": [], "confidence": "high", "proposal": "",
            "restrictions": [], "resolution": {"action": "catalogue", "candidate_url": url, "evidence": "Fiche concordante"}}

    def test_two_bounded_retries_persist_then_escalate_network(self):
        issue = {"category": "network", "stage": "Téléchargement · piste 5", "detail": "connection reset"}
        tracks = [str(self.root / "publiée.opus")]
        job = self.fail(PLAYLIST, issue, tracks=tracks)
        self.tick(100)
        self.assertEqual(self.queue.job(job)["state"], "partial")
        self.assertEqual(self.dumped_task(), [])
        self.tick(159)
        self.assertEqual(self.queue.job(job)["state"], "partial")
        self.tick(165)
        self.assertEqual(self.queue.job(job)["state"], "queued")
        self.fail(PLAYLIST, issue, job, tracks=tracks)
        # Restart carries the first retry and never restarts the budget.
        self.recovery = ImportDiagnostics(self.controller, self.queue, versions=lambda: VERSIONS)
        self.tick(200)
        self.tick(441)
        self.assertEqual(self.queue.job(job)["state"], "queued")
        self.fail(PLAYLIST, issue, job, tracks=tracks)
        self.tick(450)
        self.assertEqual(len(self.dumped_task()), 1)
        self.tick(751)
        self.assertEqual(self.queue.job(job)["state"], "review")
        self.assertEqual(self.queue.outcomes(job)[PLAYLIST]["tracks"], tracks)

    def test_rate_space_and_voluntary_stop_do_not_call_codex(self):
        issue = {"category": "network", "stage": "Téléchargement", "detail": "HTTP Error 429 retry-after: 600"}
        job = self.fail(PLAYLIST, issue)
        self.tick(100)
        self.tick(699)
        self.assertEqual(self.queue.job(job)["state"], "failed")
        self.tick(705)
        self.fail(PLAYLIST, issue, job)
        self.tick(800)
        self.tick(1405)
        self.fail(PLAYLIST, issue, job)
        self.tick(1411)
        self.assertEqual(self.queue.job(job)["state"], "review")
        space = self.fail(OTHER, {"stage": "Tri", "category": "space", "detail": "No space left on device"})
        self.tick(1500)
        self.assertEqual(self.queue.job(space)["state"], "review")
        stopped = self.fail(OTHER, issue, interrupted=True)
        self.tick(1510)
        self.assertEqual(self.queue.job(stopped)["state"], "review")
        self.assertEqual(self.dumped_task(), [])

    def test_similar_outages_delay_retries_without_stopping_manual_imports(self):
        issue = {"category": "network", "stage": "playlist", "detail": "HTTP Error 503"}
        jobs = [self.fail(f"https://music.youtube.com/playlist?list=OLAK5uy_fixture{n}", issue)
                for n in range(4)]
        self.tick(100)
        self.tick(165)
        self.assertTrue(all(self.queue.job(job)["state"] == "failed" for job in jobs))
        manual = self.queue.enqueue([("Manuel", OTHER)])
        self.assertTrue(self.queue.claim(manual))
        self.assertEqual(len(self.dumped_task()), 0)

    def test_shared_regression_single_research_and_redacted_facts(self):
        issue = {"category": "processing", "stage": "Résolution de la playlist",
                 "detail": "malformed JSON https://example.com/path?token=secret\nCookie: KEEP_OUT", "returncode": 2}
        first, second = self.fail(PLAYLIST, issue), self.fail(OTHER, issue)
        self.tick(125)
        row, = self.dumped_task()
        query = json.loads(row["payload"])
        self.assertEqual(query["failure"], "api")
        self.assertEqual(query["return_code"], 2)
        self.assertEqual(query["versions"], VERSIONS)
        self.assertNotIn("secret", row["payload"])
        self.assertNotIn("KEEP_OUT", row["payload"])
        for job in (first, second):
            self.assertIn("regroupé", self.queue.job(job)["message"])
        left = classify({**issue, "category": "playlist", "detail": "Playlist indisponible"}, {"url": PLAYLIST})
        right = classify({**issue, "category": "playlist", "detail": "Playlist indisponible"}, {"url": OTHER})
        self.assertNotEqual(signatures(left, VERSIONS), signatures(right, VERSIONS))

    def test_repair_requires_same_verified_edition_and_preserves_receipts(self):
        issue = {"category": "playlist", "stage": "Résolution de la playlist", "detail": "Playlist inexistante"}
        old = ["/fixture/piste-publiée.flac"]
        job = self.fail(PLAYLIST, issue, tracks=old)
        self.tick(100)
        row, = self.dumped_task()
        self.index.review("codex", row["subject"], "found", validate(self.result(row["subject"]), row["subject"], importing=True))
        self.queue.reconsider_import(row["subject"])
        self.tick(110)
        self.assertEqual(self.queue.job(job)["state"], "queued")
        self.assertEqual(self.queue.job(job)["targets"][0][1], ALIAS)
        self.assertEqual(self.queue.outcomes(job)[ALIAS]["tracks"], old)
        self.fail(ALIAS, issue, job, tracks=old)
        self.tick(120)
        self.assertEqual(self.queue.job(job)["state"], "review")
        self.assertIsNone(rendition(ALIAS + "&cookie=secret"))
        self.assertIsNone(rendition(ALIAS.replace("music.", "user@music.")))

    def test_different_edition_and_unavailable_codex_require_review(self):
        issue = {"category": "playlist", "stage": "playlist", "detail": "Playlist inexistante"}
        job = self.fail(PLAYLIST, issue)
        self.tick(100)
        row, = self.dumped_task()
        self.index.review("codex", row["subject"], "found", self.result(row["subject"], OTHER))
        self.queue.reconsider_import(row["subject"])
        self.tick(110)
        self.assertEqual(self.queue.job(job)["state"], "review")
        self.assertEqual(self.queue.job(job)["targets"][0][1], PLAYLIST)
        waiting = self.fail(OTHER, issue)
        self.tick(120)
        self.index.recover()
        pending = self.index.claim(("codex",), 10**10)
        self.index.finish(pending, "temporary", "quota_local", 3600)
        self.tick(425)
        self.assertEqual(self.queue.job(waiting)["state"], "review")

    def test_idempotent_authorized_admission_after_restart(self):
        first = self.queue.enqueue_maintenance("receipt", "Album", PLAYLIST)
        self.assertTrue(self.queue.claim(first))
        self.queue.finish(first, "success", "Terminé")
        resumed = ImportQueue(self.root / "state", self.root / "music")
        self.assertEqual(resumed.enqueue_maintenance("receipt", "Album", PLAYLIST), first)
        self.assertEqual(resumed.count(), {"done": 1})


if __name__ == "__main__":
    unittest.main()
