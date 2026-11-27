#!/usr/bin/python3 -B
"""Isolated import regressions with real local media, yt-dlp and FFmpeg."""
from collections import Counter
from functools import partial
from fcntl import LOCK_EX, flock
from hashlib import sha1
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from tempfile import TemporaryDirectory
from threading import Event, Thread
from types import SimpleNamespace
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FIXTURES = ROOT / "tests/fixtures/parole"
sys.path.insert(0, str(BIN))
from _kara_audio_identity import pcm_digest
from _kara_import_local import local_target
from _kara_mobile_imports import MobileImports
from _kara_import_queue import ImportQueue, digest
from _kara_import_resources import MAX_DOWNLOADS, PROCESSING, YOUTUBE_DEFAULT_SLOTS, lease, slot_count
from _kara_import_result import ImportIssue
from _parole_index import Index, inventory
from _parole_import_metrics import LOOKUP, SUCCEEDED, TOTAL, observe, snapshot
from _parole_import_providers import ARCHIVE, ARTIST_SITES, JAMENDO, OPEN_LICENSES
from _parole_sources import controls, seed
from _parole_offer import create_plan, rank, source_order
from _parole_archive import parse_item as archive_item
from _parole_jamendo import parse_release as jamendo_edition
from _musique_oz import artist_spelling, probe

TOOLS = {name: shutil.which(name) for name in ("yt-dlp", "ffmpeg", "ffprobe")}


def playlist(name):
    return f"https://www.youtube.com/playlist?list={name}"


MISSING = ("WARNING: [youtube:tab] YouTube said: The playlist does not exist.\n"
           "ERROR: [youtube:tab] PLDOWN: YouTube said: The playlist does not exist.\n"
           "Détail complet de l’extracteur : " + "donnée " * 42 + "FIN DU DIAGNOSTIC")


class QuietHTTP(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


class QueueTests(unittest.TestCase):
    def test_mixed_album_window_keeps_other_sources_eligible(self):
        youtube = [playlist(f"PREMIER{i}") for i in range(231)]
        archive = "https://archive.org/details/audit-autorise"
        jamendo = "https://www.jamendo.com/album/48123"
        order = list(source_order([*youtube, archive, jamendo]))
        self.assertEqual(order[:4], [(1, youtube[0]), (232, archive), (233, jamendo), (2, youtube[1])])
        self.assertEqual(sorted(url for _, url in order), sorted([*youtube, archive, jamendo]))

    def test_resume_prepares_unattempted_targets_before_repeat_failures(self):
        interrupted, partial, ready, new, failed = (playlist(f"REPRISE{i}") for i in range(5))
        archive = "https://archive.org/details/album-mixte"
        previous = {interrupted: {"status": "suspended"}, partial: {"status": "partial"},
                    ready: {"status": "pending"}, failed: {"status": "failure"}}
        order = list(source_order([interrupted, partial, ready, new, failed, archive], previous))
        self.assertEqual(order, [(3, ready), (6, archive), (4, new), (1, interrupted), (2, partial), (5, failed)])

    def test_mobile_discography_approval_keeps_the_selected_artist(self):
        private = Path(self.enterContext(TemporaryDirectory()))
        queue = ImportQueue(private / "state", private)
        control = SimpleNamespace(blackbox=SimpleNamespace(emit=lambda *args, **kwargs: None))
        mobile = MobileImports(control)
        mobile.bind_queue(queue)
        artist_id = "UC" + "a" * 22
        mobile.discographies["catalogue"] = {"artist_id": artist_id, "name": "Bob Marley",
                                           "albums": [{"id": "MPREtest01", "title": "Exodus"}]}
        request = mobile.request("fixture-session", "Organisateur",
                                 {"handle": "catalogue", "all": True, "rights": True})
        restarted = MobileImports(control)
        restarted.bind_queue(queue)
        job = restarted.jobs[request["job"]]
        restarted.enqueue(job)
        self.assertEqual(queue.routes(job["task_id"]),
                         {"https://music.youtube.com/browse/MPREtest01": ("Bob Marley", artist_id, "MPREtest01")})

    def test_spelling_keys_preserve_collaborator_boundaries(self):
        for a, b in (("Bob Marley & The Wailers", "Bob Marley and the Wailers"),
                     ("Guy2bezbar", "GUY2BEZBAR"), ("R.E.M.", "R E M"),
                     ("Mafia K’1 Fry", "Mafia K'1 Fry"), ("Váld", "Vald"),
                     ("Kurupt", " KURUPT "), ("L2B", "L2b")):
            self.assertEqual(artist_spelling(a), artist_spelling(b))
        self.assertNotEqual(artist_spelling("R.E.M. & Vald"), artist_spelling("R.E.M."))
        self.assertNotEqual(artist_spelling("Bob Marley, Neville Willoughby"),
                            artist_spelling("Bob Marley & The Wailers"))

    def test_manual_bandcamp_reference_accepts_only_public_release_pages(self):
        local = "file:///tmp/album.zip"
        release = "https://label.bandcamp.com/album/album-test"
        self.assertIn("label.bandcamp.com", local_target(local, "Bandcamp", release))
        for private in (release + "?token=private", "https://label.bandcamp.com/download/code",
                        "https://label.bandcamp.com.example.com/album/test"):
            with self.subTest(private=private), self.assertRaises(ImportIssue):
                local_target(local, "Bandcamp", private)
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            queue = ImportQueue(root / "state", root)
            with self.assertRaises(ImportIssue):
                queue.enqueue([("Album", local + "?provider=Bandcamp&edition=" + release + "%3Ftoken=secret")])
            self.assertEqual(queue.count(), {})

    def test_integration_tracks_remain_recoverable_until_acknowledged(self):
        with TemporaryDirectory(prefix="parole-history-") as temporary:
            root = Path(temporary)
            queue = ImportQueue(root / "state", root)
            good, missing = playlist("GOOD"), playlist("MISSING")
            identity = queue.enqueue([("Réussi", good), ("Incomplet", missing)], journal="journal-local")
            self.assertTrue(queue.claim(identity))
            for url in (good, missing):
                queue.album(identity, dict(url=url, status="success", audio_complete=True, tracks=[url]))
            queue.finish(identity, "success", "Album téléchargé")
            row = queue.rows()[0]
            self.assertTrue(row["refresh"])
            self.assertEqual(queue.history()[0]["id"], identity)
            queue.integration_failed(identity, row["refresh"], [missing])
            self.assertEqual(queue.rows()[0]["state"], "partial")
            self.assertEqual(queue.outcomes(identity)[good]["status"], "success")
            self.assertEqual(queue.outcomes(identity)[missing]["diagnostics"][0]["stage"], "integration")
            queue.transition(identity, "partial", "queued", "Relance")
            self.assertTrue(queue.claim(identity))
            queue.album(identity, dict(url=missing, status="success", audio_complete=True, tracks=[missing]))
            queue.finish(identity, "success", "Intégré")
            queue.refreshed([(identity, queue.job(identity)["refresh"])])
            relaunched = ImportQueue(root / "state", root)
            self.assertEqual(relaunched.rows(), [])
            historical = relaunched.history()
            self.assertEqual(historical[0]["journal"], "journal-local")
            self.assertEqual(set(relaunched.outcomes(identity)), {good, missing})

    def test_history_cleanup_keeps_queue_receipts_and_updates_mobile(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="parole-history-")))
        queue = ImportQueue(root / "state", root)
        mobile = MobileImports(SimpleNamespace(album_import=SimpleNamespace(imports_snapshot=([], {}))))

        def add(number, event=None, *, acknowledge=True, linked=False):
            url = playlist(f"HISTORY{number}")
            request = None
            if linked:
                request = dict(id=f"{number:032x}", title="Mobile", targets=[("Album", url)], tracks=1,
                               state="done", message="Intégré", progress=1., requests={digest("member"): []},
                               notes={}, results={})
            job = queue.enqueue_maintenance("automatic-receipt", "Album", url) if number == 1 else queue.enqueue(
                [("Album", url)], request=request)
            if event is not None:
                self.assertTrue(queue.claim(job))
                if event != "cancelled":
                    queue.album(job, dict(url=url, status="partial" if event == "failure" else "success",
                                          tracks=[f"piste-{number}"]))
                queue.finish(job, event, event)
                if acknowledge:
                    queue.refreshed([(job, queue.job(job)["refresh"])])
            return job, url

        success, url = add(1, "success")
        linked, _ = add(2, "success", linked=True)
        cancelled, _ = add(3, "cancelled")
        previous_cancellation, _ = add(4, "cancelled")
        partial, pending_url = add(5, "failure")
        integrating, _ = add(6, "success", acknowledge=False)
        waiting, _ = add(7)
        mobile.bind_queue(queue)
        self.assertEqual(len(mobile.snapshot(organizer=True, history=True)), 6)
        self.assertEqual(len(mobile.snapshot(token="member", history=True)), 1)
        self.assertEqual(queue.totals(), (1, 2, 2, 3))

        self.assertEqual(queue.delete_history((linked, cancelled, waiting, partial, integrating)), 4)
        self.assertEqual(queue.history_policy(), (0, 1))
        self.assertEqual(mobile.snapshot(token="member", history=True), [])
        self.assertEqual([row["id"] for row in queue.rows()], [waiting])
        self.assertEqual(queue.count(visible=True), {"queued": 1})
        self.assertEqual(queue.totals(), (0, 0, 0, 1))
        self.assertEqual([row["id"] for row in queue.rows(include_hidden_refresh=True)], [integrating, waiting])
        self.assertNotIn(partial, [row["id"] for row in queue.diagnosis_candidates(time.time())])
        self.assertEqual(queue.delete_history(successful=True), 1)
        self.assertEqual([row["id"] for row in queue.history()], [previous_cancellation])
        self.assertEqual(queue.delete_history(), 1)
        self.assertEqual(queue.delete_history(), 0)

        restarted = ImportQueue(root / "state", root)
        mobile.bind_queue(restarted)
        self.assertEqual(mobile.snapshot(organizer=True, history=True), [])
        self.assertEqual(mobile.snapshot(token="member", history=True), [])
        self.assertEqual(restarted.history_policy(), (0, 3))
        self.assertEqual(restarted.enqueue_maintenance("automatic-receipt", "Album", url), success)
        self.assertEqual(restarted.outcomes(success)[url]["tracks"], ["piste-1"])
        self.assertEqual(restarted.outcomes(partial)[pending_url]["tracks"], ["piste-5"])
        self.assertTrue(restarted.transition(partial, "partial", "queued", "Reprendre"))
        self.assertTrue(restarted.claim(partial))
        self.assertEqual(restarted.enqueue([("Déjà en cours", pending_url)]), partial)
        self.assertEqual(restarted.job(waiting)["state"], "queued")
        restarted.refreshed([(integrating, restarted.job(integrating)["refresh"])])
        self.assertEqual(restarted.history(), [])
        self.assertEqual(restarted.totals(), (0, 1, 0, 2))

    def test_history_retention_uses_closure_date_and_persists(self):
        for days in (7, 30, 90):
            with self.subTest(days=days), TemporaryDirectory(prefix="parole-history-") as temporary:
                root = Path(temporary)
                queue = ImportQueue(root / "state", root)
                now = time.time()
                closed = []
                for age in (days + 1, days - 1):
                    identity = queue.enqueue([("Album", playlist(f"RETENTION{age}"))])
                    self.assertTrue(queue.transition(identity, "queued", "cancelled", "Clôturé"))
                    with queue.connect(write=True) as db:
                        db.execute("UPDATE import_history SET closed_at=? WHERE job=?", (now - age * 86400, identity))
                    closed.append(identity)
                self.assertEqual(queue.prune_history(), 0)
                self.assertEqual(queue.configure_history(days), 1)
                restarted = ImportQueue(root / "state", root)
                self.assertEqual(restarted.history_policy(), (days, 1))
                self.assertEqual([row["id"] for row in restarted.history()], [closed[1]])
                with restarted.connect(write=True) as db:
                    db.execute("UPDATE import_history SET closed_at=? WHERE job=?", (now - (days + 1) * 86400, closed[1]))
                self.assertEqual(restarted.prune_history(), 1)
                self.assertEqual(restarted.configure_history(0), 0)
                self.assertEqual(restarted.history_policy(), (0, 2))
                self.assertEqual(restarted.history(), [])

    def test_history_migrates_legacy_closed_lots_without_touching_imports(self):
        root = Path(self.enterContext(TemporaryDirectory(prefix="parole-history-")))
        queue = ImportQueue(root / "state", root)
        identity = queue.enqueue([("Album", playlist("LEGACY"))])
        self.assertTrue(queue.transition(identity, "queued", "cancelled", "Clôturé"))
        active = queue.enqueue([("En cours", playlist("ONGOING"))])
        self.assertTrue(queue.claim(active))
        before = queue.job(active)
        date = time.time() - 45 * 86400
        # Reconstruct the previous schema with an existing settlement receipt.
        with queue.connect(write=True) as db:
            db.execute("DROP TRIGGER import_history_settled")
            db.execute("DROP TABLE import_history")
            db.execute("DROP TABLE history_policy")
            db.execute("""INSERT INTO import_recovery_history(job,attempt,stamp,signature,phase,message)
                          VALUES (?,0,?,'','cancelled','Clôturé')""", (identity, date))
        migrated = ImportQueue(root / "state", root)
        self.assertEqual([row["id"] for row in migrated.history()], [identity])
        self.assertEqual(migrated.job(active), before)
        self.assertEqual(migrated.configure_history(30), 1)
        self.assertEqual(migrated.history(), [])

    def test_persistent_large_queue_and_shared_admission(self):
        with TemporaryDirectory(prefix="parole-queue-") as temporary:
            root = Path(temporary)
            queue = ImportQueue(root / "state", root / "collection")
            queue.configure(paused=True)
            lots = [queue.enqueue([(f"Artiste {artist}/{number}", playlist(f"AL{artist}_{number}"))
                                   for number in range(5)]) for artist in range(100)]
            self.assertEqual(queue.count(), {"queued": 100})
            self.assertEqual(queue.totals(), (0, 0, 0, 500))
            self.assertEqual(queue.enqueue([("Double", playlist("AL0_1"))]), lots[0])
            restarted = ImportQueue(root / "state", root / "collection")
            self.assertEqual(restarted.settings(), (True, 4))
            self.assertFalse(restarted.claim(lots[0]))
            restarted.configure(paused=False)
            self.assertEqual([restarted.claim(lot) for lot in lots[:5]], [True] * 4 + [False])
            self.assertEqual(restarted.count(), {"queued": 96, "running": 4})
            restarted.configure(batches=2)
            self.assertFalse(queue.claim(lots[5]))
            self.assertEqual(ImportQueue(root / "state", root / "other-collection").count(), {})


    def test_an_old_refresh_cannot_acknowledge_a_new_attempt(self):
        with TemporaryDirectory(prefix="parole-queue-") as temporary:
            root = Path(temporary)
            queue = ImportQueue(root / "state", root / "collection")
            identity = queue.enqueue([("Album", playlist("PLFIX"))])
            self.assertTrue(queue.claim(identity))
            queue.finish(identity, "failure", "Échec")
            generation = queue.job(identity)["refresh"]
            self.assertTrue(queue.transition(identity, "failed", "queued", "Relance"))
            self.assertTrue(queue.claim(identity))
            queue.finish(identity, "success", "Terminé")
            queue.refreshed([(identity, generation)])
            latest = queue.job(identity)["refresh"]
            self.assertGreater(latest, generation)
            queue.refreshed([(identity, latest)])
            self.assertEqual(queue.job(identity)["refresh"], 0)


@unittest.skipUnless(all(TOOLS.values()), "yt-dlp, FFmpeg and ffprobe are required")
class AlbumImportTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        media = Path(cls.enterClassContext(TemporaryDirectory(prefix="parole-media-")))
        subprocess.run([TOOLS["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=4", "-c:a", "libopus", str(media / "signal.opus")],
                       check=True)
        cls.signal = (media / "signal.opus").read_bytes()
        from PIL import Image
        cls.cover = media / "cover.jpg"
        Image.new("RGB", (200, 200), "#526c9c").save(cls.cover)
        server = ThreadingHTTPServer(("127.0.0.1", 0), partial(QuietHTTP, directory=media))
        cls.addClassCleanup(server.server_close)
        Thread(target=server.serve_forever, daemon=True).start()
        cls.addClassCleanup(server.shutdown)
        cls.media = f"http://127.0.0.1:{server.server_port}/signal.opus"

    def setUp(self):
        self.temp = Path(self.enterContext(TemporaryDirectory(prefix="parole-import-")))
        self.library = self.temp / "Bibliothèque isolée"
        self.inbox = self.library / "A_trier"
        self.inbox.mkdir(parents=True)
        self.manifest = self.temp / "input.json"
        self.requests = self.temp / "requests"
        self.requests.touch()
        self.env = dict(os.environ, KARA_MUSIC_LIBRARY=str(self.library),
                        KARA_CATALOGUE_PYTHON=sys.executable, XDG_STATE_HOME=str(self.temp / "state"),
                        XDG_DATA_HOME=str(self.temp / "data"), XDG_CONFIG_HOME=str(self.temp / "config"),
                        XDG_CACHE_HOME=str(self.temp / "cache"),
                        PATH=str(FIXTURES) + os.pathsep + os.environ["PATH"],
                        PYTHONPATH=str(FIXTURES), PAROLE_TEST_CONFIG=str(self.manifest), TMPDIR=str(self.temp))
        self.config = {
            "real_downloader": TOOLS["yt-dlp"], "artist": "Atelier test", "media": self.media,
            "requests": str(self.requests), "cover": str(self.cover), "temp": str(self.temp),
            "playlist_failures": {"PLDOWN": MISSING, "PLNETWORK": "ERROR: HTTP Error 503: Service Unavailable"},
            "transfer_failures": ["soundPART02"], "lyric_failures": [],
            "playlists": {
                "OLAK5uy_FIXTURE": {"title": "100% Signal: A", "browse": "MPREb_FIXTURE", "tracks": [
                    {"id": "soundGOOD01", "title": "Signal un", "catalogue_id": "discoGOOD01"},
                    {"id": "soundGOOD02", "title": "Signal deux", "catalogue_id": "discoGOOD02"},
                ]},
                "PLPART": {"title": "Deux pistes", "tracks": [
                    {"id": "soundPART01", "title": "Piste un"}, {"id": "soundPART02", "title": "Piste deux"},
                ]},
                "PLDOWN": {"title": "Retour", "tracks": [{"id": "soundDOWN01", "title": "Retour son"}]},
                "PLENRICH": {"title": "Enrich", "tracks": [{"id": "soundLRC001", "title": "LRC"}]},
            },
        }
        self.update_config()

    def update_config(self):
        self.manifest.write_text(json.dumps(self.config), encoding="utf-8")

    def hold_media_bodies(self):
        # Keep real response bodies open while observing leases. Delaying
        # extractor startup would exercise the resolution phase instead.
        audio, release = self.signal, Event()

        class HeldAudio(QuietHTTP):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(audio)))
                self.end_headers()
                try:
                    self.wfile.write(audio[:8192])
                    release.wait(18)
                    self.wfile.write(audio[8192:])
                except OSError:
                    pass

        server = self.enterContext(ThreadingHTTPServer(("127.0.0.1", 0), HeldAudio))
        Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        self.addCleanup(release.set)
        self.config["media"] = f"http://127.0.0.1:{server.server_port}/audio.opus"
        return release

    def run_import(self, *urls, expected=0, cwd=None, previous=None, env=None, command=None):
        command = [str(command or BIN / "kara"), "--suivi-json"]
        if previous:
            command.extend(["--reprendre", previous])
        command.extend(["--lot" if len(urls) > 1 else "--", *urls])
        result = subprocess.run(command, cwd=cwd or self.temp, env=env or self.env,
                                capture_output=True, text=True, timeout=75)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertEqual(result.returncode, expected, (result.stderr, events[-5:]))
        journal = next(event["path"] for event in events if event["event"] == "journal")
        return journal, [event for event in events if event["event"] == "album"], events[-1]

    def check_tracks(self, album, count, *, enriched=True):
        directory = self.library / self.config["artist"] / album
        tracks = sorted(directory.glob("*.opus"))
        self.assertEqual(len(tracks), count, list(directory.glob("*")))
        if enriched:
            self.assertGreater((directory / "cover.jpg").stat().st_size, 0)
        for index, track in enumerate(tracks, 1):
            self.assertTrue(track.name.startswith(f"{index:02d} - "))
            if enriched:
                self.assertIn("[00:00.20]Son créé pour le test.", track.with_suffix(".lrc").read_text())
            exported = json.loads(subprocess.check_output(
                [TOOLS["ffprobe"], "-v", "error", "-show_entries", "stream_tags", "-of", "json", str(track)]
            ))["streams"][0]["tags"]
            tags = {key.casefold(): value for key, value in exported.items()}
            self.assertEqual(tags["album"], album)
            self.assertEqual(tags["track"], str(index))
            if enriched:
                self.assertEqual(tags["lyrics"], "Son créé pour le test.")
            else:
                self.assertNotIn("lyrics", tags)
        return tracks

    def make_local_track(self, path, number, *, album="Album local"):
        path.parent.mkdir(exist_ok=True, parents=True)
        subprocess.run([TOOLS["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={220 + 110 * number}:duration=4", "-c:a", "flac",
                        "-metadata", f"artist={self.config['artist']}",
                        *(["-metadata", f"album={album}"] if album else []),
                        "-metadata", f"title=Son local {number}", "-metadata", f"track={number}", str(path)],
                       check=True)

    def test_local_import_applies_only_the_approved_album_artist(self):
        source = self.temp / "Édition fournisseur" / "01.flac"
        self.make_local_track(source, 1, album="Réédition confirmée")
        original = source.read_bytes()
        index = Index(self.library, self.temp / "state")
        with index.db() as db:
            db.execute("INSERT INTO associations VALUES(?,?,?,?)",
                       (self.config["artist"], "Réédition confirmée", "Artiste canonique", "validation-fixture"))
        queue = ImportQueue(self.temp / "state", self.library)
        url = source.as_uri()
        identity = queue.enqueue([("Réédition confirmée", url)])
        self.assertTrue(queue.claim(identity))
        with lease(queue.directory / f"{identity}.lock") as fd:
            worker = self.managed_worker(queue, identity, fd)
        output, errors = worker.communicate(timeout=30)
        self.assertEqual(worker.returncode, 0, (output, errors))
        outcome = queue.outcomes(identity)[url]
        self.assertTrue(outcome["audio_complete"])
        target, = map(Path, outcome["tracks"])
        self.assertEqual(target.relative_to(self.library).parts[:2], ("Artiste canonique", "Réédition confirmée"))
        tags, _ = probe(target)
        self.assertEqual((tags["artist"], tags["albumartist"], tags["title"]),
                         (self.config["artist"], "Artiste canonique", "Son local 1"))
        self.assertEqual(pcm_digest(source), pcm_digest(target))
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(outcome["provenance"][0]["tags"]["artist"], self.config["artist"])

        other = self.temp / "Édition hors règle" / "01.flac"
        self.make_local_track(other, 1, album="Édition hors règle")
        _, outcomes, _ = self.run_import(other.as_uri())
        separate, = map(Path, outcomes[0]["tracks"])
        self.assertEqual(separate.relative_to(self.library).parts[0], self.config["artist"])

    def test_local_directory_managed_job_resumes_one_missing_track(self):
        source = self.temp / "Édition d’origine"
        first, second = (source / name for name in ("01.flac", "02.flac"))
        self.make_local_track(first, 1)
        second.write_text("piste incomplète")
        original = first.read_bytes()
        queue = ImportQueue(self.temp / "state", self.library)
        url = source.as_uri()
        identity = queue.enqueue([("Album local", url)])

        def run_job(code):
            self.assertTrue(queue.claim(identity))
            with lease(queue.directory / f"{identity}.lock") as guard:
                done = subprocess.run([str(BIN / "kara"), "--suivi-json", "--import-file", identity,
                                       "--import-lease", str(guard)], env=self.env, pass_fds=(guard,),
                                      capture_output=True, text=True, timeout=75)
            self.assertEqual(done.returncode, code, (done.stdout, done.stderr))
            return queue.outcomes(identity)[url]

        partial = run_job(1)
        self.assertEqual((partial["provider"], partial["status"], partial["audio_complete"]),
                         ("Fichiers locaux", "partial", False))
        published = Path(partial["tracks"][0])
        self.assertEqual(pcm_digest(published), pcm_digest(first))
        before = published.read_bytes()
        second.unlink()
        self.make_local_track(second, 2)
        queue.transition(identity, "partial", "queued", "Relance du dossier")
        complete = run_job(0)
        self.assertEqual((complete["status"], complete["added"], complete["processed"]), ("success", 1, 1))
        with Index(self.library, self.temp / "state").db() as db:
            states = {(kind, subject): state for kind, subject, state in
                      db.execute("SELECT kind,subject,state FROM tasks")}
        self.assertTrue(complete["enrichment"]["import_lyrics"])
        for kind, subjects in complete["enrichment"].items():
            for subject in subjects:
                self.assertEqual(states[kind, subject], "queued")
        self.assertEqual([row["member"] for row in complete["provenance"]], ["01.flac", "02.flac"])
        self.assertEqual(complete["provenance"][0]["external_id"], partial["provenance"][0]["external_id"])
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(published.read_bytes(), before)
        self.assertEqual(pcm_digest(complete["tracks"][1]), pcm_digest(second))
        maintenance = Index(self.library, self.temp / "state")
        seed(maintenance)
        task = maintenance.claim(("import_lyrics",), time.time())
        self.assertEqual(maintenance.recover(), False)
        restarted = Index(self.library, self.temp / "state")
        self.assertEqual(restarted.claim(("import_lyrics",), time.time())["subject"], task["subject"])
        result = subprocess.check_output([sys.executable, "-B", str(BIN / "_parole_work.py"),
                                          "import_lyrics", task["subject"]], env=self.env, timeout=20)
        evidence = json.loads(result)
        self.assertEqual(evidence["state"], "complete")
        self.assertGreater(evidence["lyrics"]["found"], 0)
        self.assertEqual(evidence["requests"], evidence["lyrics"]["found"])
        self.assertEqual(evidence["lyrics"]["found"], evidence["lrc"]["found"])
        self.assertTrue(published.with_suffix(".lrc").is_file())
        self.assertEqual(pcm_digest(published), pcm_digest(first))
        # Simulate a retry on the original audio before enrichment. The new
        # process must find both independently verified forms in SQLite.
        published.with_suffix(".lrc").unlink()
        shutil.copyfile(first, published)
        cached = json.loads(subprocess.check_output([sys.executable, "-B", str(BIN / "_parole_work.py"),
                                                    "import_lyrics", task["subject"]], env=self.env, timeout=20))
        self.assertEqual(cached["requests"], 0)
        self.assertGreaterEqual(cached["cache_hits"], 1)
        self.assertTrue(published.with_suffix(".lrc").is_file())
        queue.refreshed([(identity, queue.job(identity)["refresh"])])
        self.assertEqual(queue.delete_history(successful=True), 1)
        self.assertEqual(ImportQueue(self.temp / "state", self.library).history(), [])
        self.assertEqual(queue.outcomes(identity)[url]["provenance"], complete["provenance"])
        _, items, _ = self.run_import(url)
        self.assertEqual((items[0]["status"], items[0]["processed"]), ("present", 0))
        self.assertEqual(self.requests.read_text(), "")

    def test_local_archive_preserves_pcm_and_supplied_assets(self):
        track = self.temp / "manifest" / "01.flac"
        self.make_local_track(track, 1, album="Album ZIP")
        lyrics = "[00:00.20]Paroles synchronisées d’origine.\n".encode()
        archive = self.temp / "Édition ZIP + #1.zip"
        with ZipFile(archive, "w") as output:
            output.write(track, "Album ZIP/01.flac")
            output.write(self.cover, "Album ZIP/cover.jpg")
            output.writestr("Album ZIP/01.lrc", lyrics)
        original = archive.read_bytes()
        _, items, _ = self.run_import(archive.as_uri())
        outcome = items[0]
        self.assertEqual((outcome["status"], outcome["added"]), ("success", 1))
        published = Path(outcome["tracks"][0])
        self.assertEqual(pcm_digest(published), pcm_digest(track))
        self.assertEqual(published.with_suffix(".lrc").read_bytes(), lyrics)
        self.assertEqual((published.parent / "cover.jpg").read_bytes(), self.cover.read_bytes())
        self.assertEqual(archive.read_bytes(), original)

    def test_local_archive_rejects_unsafe_paths_before_publication(self):
        track = self.temp / "manifest" / "01.flac"
        self.make_local_track(track, 1)
        archive = self.temp / "archive.zip"
        with ZipFile(archive, "w") as output:
            output.write(track, "Album/01.flac")
            output.writestr("../sortie.flac", b"fichier interdit")
        _, items, _ = self.run_import(archive.as_uri(), expected=1)
        self.assertEqual((items[0]["category"], items[0]["tracks"]), ("path", []))
        self.assertFalse(list(self.library.rglob("*.flac")))
        self.assertFalse((self.temp / "sortie.flac").exists())

    def test_bandcamp_local_route_preserves_declared_public_reference(self):
        source = self.temp / "Bandcamp"
        track = source / "01.flac"
        self.make_local_track(track, 1, album="Album du label")
        release = "https://label.bandcamp.com/album/album-test"
        target = local_target(source.as_uri(), "Bandcamp", release)
        _, items, _ = self.run_import(target)
        self.assertEqual((items[0]["status"], items[0]["provider"]), ("success", "Bandcamp"))
        evidence = items[0]["provenance"][0]
        self.assertEqual((evidence["edition_ref"], evidence["verification"]), (release, "manual"))
        self.assertEqual(pcm_digest(evidence["path"]), pcm_digest(track))
        idx = Index(self.library, state=self.temp / "state")
        seed(idx)
        controls(idx, "Bandcamp", "disabled")
        _, items, _ = self.run_import(target, expected=1)
        self.assertEqual((items[0]["category"], items[0]["tracks"]), ("path", []))
        self.assertEqual(self.requests.read_text(), "")

    def test_qobuz_local_route_imports_a_flac_zip_with_edition_reference(self):
        track = self.temp / "flac" / "01.flac"
        self.make_local_track(track, 1, album="Archive FLAC")
        archive = self.temp / "album-qobuz.zip"
        with ZipFile(archive, "w") as output:
            output.write(track, "01.flac")
        release = "https://www.qobuz.com/fr-fr/album/album-test/0060254719724"
        _, items, _ = self.run_import(local_target(archive.as_uri(), "Qobuz", release))
        self.assertEqual((items[0]["status"], items[0]["provider"]), ("success", "Qobuz"))
        evidence = items[0]["provenance"][0]
        self.assertEqual(evidence["edition_ref"], release)
        self.assertEqual(pcm_digest(evidence["path"]), pcm_digest(track))
        self.assertEqual(self.requests.read_text(), "")

    def test_soundcloud_single_download_does_not_import_its_neighbors(self):
        source = self.temp / "soundcloud"
        track = source / "original.flac"
        self.make_local_track(track, 1, album=None)
        self.make_local_track(source / "autre.flac", 2)
        original = track.read_bytes()
        release = "https://soundcloud.com/atelier-test/son-local"
        _, items, _ = self.run_import(local_target(track.as_uri(), "SoundCloud", release))
        self.assertEqual((items[0]["status"], items[0]["provider"], items[0]["added"]), ("success", "SoundCloud", 1))
        path = Path(items[0]["tracks"][0])
        self.assertEqual(items[0]["provenance"][0]["edition_ref"], release)
        self.assertEqual(path.parent.name, "Sans album")
        self.assertEqual(pcm_digest(path), pcm_digest(track))
        self.assertEqual(track.read_bytes(), original)
        self.assertEqual(self.requests.read_text(), "")

    def test_free_music_archive_local_route_requires_and_keeps_compatible_license(self):
        track = self.temp / "fma" / "01.flac"
        self.make_local_track(track, 1)
        release = "https://freemusicarchive.org/music/Artist/Album/Track/"
        license_url = OPEN_LICENSES["CC BY 4.0"]
        with self.assertRaises(ImportIssue):
            local_target(track.as_uri(), "Free Music Archive", release)
        with self.assertRaises(ImportIssue):
            local_target(track.as_uri(), "Free Music Archive", release,
                         "https://creativecommons.org/licenses/by-nd/4.0/")
        _, items, _ = self.run_import(local_target(track.as_uri(), "Free Music Archive", release, license_url))
        evidence = items[0]["provenance"][0]
        self.assertEqual((evidence["provider"], evidence["license"], evidence["edition_ref"]),
                         ("Free Music Archive", license_url, release.rstrip("/")))
        self.assertEqual(pcm_digest(evidence["path"]), pcm_digest(track))
        self.assertEqual(self.requests.read_text(), "")

    def test_artist_site_retains_public_download_reference(self):
        track = self.temp / "label" / "original.flac"
        self.make_local_track(track, 1)
        release = "https://records.example.org/musique/album-test/"
        for invalid in ("", "https://127.0.0.1/album", "https://records.example.org/music?signature=secret"):
            with self.assertRaises(ImportIssue):
                local_target(track.as_uri(), ARTIST_SITES, invalid)
        _, items, _ = self.run_import(local_target(track.as_uri(), ARTIST_SITES, release))
        self.assertEqual((items[0]["provider"], items[0]["status"]), (ARTIST_SITES, "success"))
        evidence = items[0]["provenance"][0]
        self.assertEqual((evidence["source"], evidence["verification"]), (release.rstrip("/"), "manual"))
        self.assertEqual(pcm_digest(evidence["path"]), pcm_digest(track))

    def test_internet_archive_search_download_and_retry_preserve_originals(self):
        # Each original has its own file identifier and API integrity checksum.
        source = self.temp / "archives"
        ids = {"01.flac": 1, "02.flac": 2}
        originals = {}
        files = []
        for name, position in ids.items():
            path = source / name
            self.make_local_track(path, position, album="Album d’archive")
            media = path.read_bytes()
            files.append(dict(name=name, size=str(len(media)), sha1=sha1(media, usedforsecurity=False).hexdigest(),
                              source="original", format="Flac"))
            originals[name] = str(path)
        files.append({"name": "02.mp3", "source": "derivative", "format": "VBR MP3"})
        license_url = OPEN_LICENSES["CC BY 4.0"]
        identifier = "controlled-archive-fixture"
        metadata = dict(identifier=identifier, title="Album d’archive", creator=self.config["artist"],
                        mediatype="audio", licenseurl=license_url)
        self.config.update(archive_item={"metadata": metadata, "files": files},
                           archive_originals=originals, archive_failures=["02.flac"])
        self.update_config()
        query = subprocess.check_output([str(BIN / "kara"), "--chercher-archives", self.config["artist"]], env=self.env)
        release = json.loads(query)["data"][0]
        self.assertEqual((release["formats"], release["tracks"], release["license"]), (["FLAC"], 2, license_url))
        url = release["url"]
        journal, results, _ = self.run_import(url, expected=1)
        self.assertEqual((results[0]["status"], results[0]["added"]), ("partial", 1))
        self.config["archive_failures"].clear()
        self.update_config()
        _, results, _ = self.run_import(url, previous=journal)
        self.assertEqual((results[0]["status"], results[0]["added"], results[0]["processed"]), ("success", 1, 1))
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"01.flac": 1, "02.flac": 2})
        for record in results[0]["provenance"]:
            self.assertEqual(record["external_id"], f"{identifier}/{record['member']}")
            self.assertEqual(record["license"], license_url)
            self.assertEqual(pcm_digest(record["path"]), pcm_digest(originals[record["member"]]))
        measured = snapshot(Index(self.library, self.temp / "state"))["Internet Archive"]
        self.assertEqual((measured[LOOKUP]["samples"], measured[TOTAL]["samples"], measured[SUCCEEDED]["total"]), (1, 2, 1))

    def test_internet_archive_never_opens_a_restricted_file_or_license(self):
        target = "https://archive.org/details/restricted-fixture"
        metadata = dict(identifier="restricted-fixture", title="Archive", creator="Auteur", mediatype="audio",
                        licenseurl=OPEN_LICENSES["CC0 1.0"])
        media = {"name": "file.flac", "source": "original", "private": "true", "size": "4", "sha1": "a" * 40}
        for case in [dict(metadata=metadata, files=[media]),
                     dict(metadata=metadata | {"licenseurl": "https://creativecommons.org/licenses/by-nd/4.0/"},
                          files=[media | {"private": "false"}])]:
            self.config["archive_item"] = case
            self.update_config()
            _, results, _ = self.run_import(target, expected=1)
            self.assertEqual((results[0]["category"], results[0]["tracks"]), ("path", []))
        self.assertEqual(self.requests.read_text(), "")

    def test_jamendo_uses_both_permissions_and_retries_only_failed_tracks(self):
        source = self.temp / "jamendo"
        originals = {}
        tracks = []
        license_url = OPEN_LICENSES["CC BY 4.0"]
        for position, identifier in enumerate(("30123", "30124"), 1):
            path = source / f"{identifier}.flac"
            self.make_local_track(path, position, album="Album Jamendo")
            tracks.append({"id": identifier, "position": str(position), "duration": "4", "name": f"Son local {position}",
                           "license_ccurl": license_url, "audio": "https://stream.jamendo.com/example",
                           "audiodownload": f"https://storage.jamendo.com/download/track/{identifier}/flac/",
                           "audiodownload_allowed": True})
            originals[identifier] = str(path)
        release = {"id": "1820", "name": "Album Jamendo", "artist_name": self.config["artist"],
                   "zip_allowed": True, "tracks": tracks}
        self.config.update(jamendo_release=release, jamendo_originals=originals, jamendo_failures=["30124"])
        self.update_config()
        env = dict(self.env, KARA_JAMENDO_CLIENT_ID="fixtureid")
        url = "https://www.jamendo.com/album/1820/album-jamendo"
        no_client = dict(self.env)
        no_client.pop("KARA_JAMENDO_CLIENT_ID", None)
        _, denied, _ = self.run_import(url, env=no_client, expected=1)
        self.assertEqual(denied[0]["category"], "dependency")
        for subject, permission in ((release, "zip_allowed"), (tracks[1], "audiodownload_allowed")):
            subject[permission] = "true"
            self.update_config()
            _, denied, _ = self.run_import(url, env=env, expected=1)
            self.assertEqual((denied[0]["category"], denied[0]["tracks"]), ("path", []))
            subject[permission] = True
        self.assertEqual(self.requests.read_text(), "")
        self.update_config()
        query = subprocess.check_output([str(BIN / "kara"), "--chercher-jamendo", self.config["artist"]], env=env)
        result, = json.loads(query)["data"]
        self.assertEqual((result["provider"], result["tracks"], result["formats"]), (JAMENDO, 2, ["FLAC"]))
        journal, outcomes, _ = self.run_import(url, expected=1, env=env)
        self.assertEqual((outcomes[0]["status"], outcomes[0]["added"]), ("partial", 1))
        self.config["jamendo_failures"].clear()
        self.update_config()
        _, outcomes, _ = self.run_import(url, previous=journal, env=env)
        self.assertEqual((outcomes[0]["status"], outcomes[0]["added"], outcomes[0]["processed"]), ("success", 1, 1))
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"30123": 1, "30124": 2})
        for evidence in outcomes[0]["provenance"]:
            self.assertEqual(evidence["license"], license_url)
            self.assertEqual(pcm_digest(evidence["path"]), pcm_digest(originals[evidence["external_id"]]))

    def equivalent_catalogues(self):
        source = self.temp / "equivalent"
        license_url = OPEN_LICENSES["CC BY 4.0"]
        release_id = "b90181d2-ff52-4bfb-aeb5-f8c66c8fed25"
        archive_files, jamendo_tracks = [], []
        archive_originals, jamendo_originals = {}, {}
        for number in (1, 2):
            name, track_id = f"{number:02}.flac", str(39010 + number)
            archive_audio, jamendo_audio = source / "archive" / name, source / "jamendo" / name
            self.make_local_track(archive_audio, number, album="Édition commune")
            self.make_local_track(jamendo_audio, number, album="EDITION COMMUNE")
            code = f"FRABC190000{number}"
            archive_files.append({"name": name, "size": str(archive_audio.stat().st_size),
                                  "sha1": sha1(archive_audio.read_bytes(), usedforsecurity=False).hexdigest(),
                                  "source": "original", "track": str(number), "length": "4",
                                  "title": f"Son local {number}", "isrc": code, "artist": self.config["artist"]})
            jamendo_tracks.append({"id": track_id, "position": str(number), "duration": "4", "name": f"Son local {number}",
                                   "license_ccurl": license_url, "audiodownload_allowed": True, "isrc": code,
                                   "audiodownload": f"https://storage.jamendo.com/download/track/{track_id}/flac/"})
            archive_originals[name], jamendo_originals[track_id] = str(archive_audio), str(jamendo_audio)
        metadata = {"identifier": "edition-fixture", "mediatype": "audio", "creator": self.config["artist"],
                    "title": "Édition commune", "licenseurl": license_url, "musicbrainz_releaseid": release_id}
        archive = {"metadata": metadata, "files": archive_files}
        jamendo = {"id": "48123", "name": "EDITION COMMUNE", "artist_name": self.config["artist"],
                   "zip_allowed": True, "tracks": jamendo_tracks, "musicbrainz_releaseid": release_id}
        self.config.update(archive_item=archive, archive_originals=archive_originals, archive_failures=["02.flac"],
                           jamendo_release=jamendo, jamendo_originals=jamendo_originals)
        self.update_config()
        index = Index(self.library, self.temp / "state")
        seed(index)
        offers = [
            {"url": "https://archive.org/details/edition-fixture", "provider": ARCHIVE, "price": 0,
             "formats": ["FLAC"], "facts": archive_item("edition-fixture", archive).facts},
            {"url": "https://www.jamendo.com/album/48123", "provider": JAMENDO, "price": 0,
             "formats": ["FLAC"], "facts": jamendo_edition("album", jamendo).facts},
        ]
        env = dict(self.env, KARA_JAMENDO_CLIENT_ID="fixtureid")
        return index, offers, env

    def test_confirmed_equivalent_provider_fills_only_missing_position(self):
        index, offers, env = self.equivalent_catalogues()
        index.configure(import_strategy="preferred", import_preferred=ARCHIVE)
        target = create_plan(index, offers)
        journal, partial, _ = self.run_import(target, env=env, expected=1)
        self.assertEqual((partial[0]["status"], partial[0]["provider"]), ("partial", ARCHIVE))
        first = Path(partial[0]["tracks"][0])
        before = first.read_bytes()
        _, done, _ = self.run_import(target, env=env, previous=journal)
        self.assertEqual((done[0]["status"], done[0]["provider"], done[0]["added"]), ("success", JAMENDO, 1))
        requests = self.requests.read_text().splitlines()
        self.assertCountEqual(requests[:2], ["01.flac", "02.flac"])
        self.assertEqual(requests[2:], ["02.flac", "39012"])
        self.assertEqual({Path(path).parent for path in done[0]["tracks"]}, {first.parent})
        self.assertEqual(first.read_bytes(), before)
        self.assertEqual({proof["provider"] for proof in done[0]["provenance"]}, {ARCHIVE, JAMENDO})
        _, replay, _ = self.run_import(target, env=env)
        self.assertEqual((replay[0]["status"], replay[0]["processed"]), ("present", 0))
        self.assertEqual(self.requests.read_text().splitlines(), requests)

    def test_speed_strategy_accounts_for_admission_on_verified_offers(self):
        index, offers, _ = self.equivalent_catalogues()
        index.configure(import_strategy="speed")
        for source in (ARCHIVE, JAMENDO):
            observe(index, source, {TOTAL: 3., SUCCEEDED: 1})
        waits = {ARCHIVE: 12., JAMENDO: 0.}
        fast, queued = rank(index, offers, admission_waits=waits)
        self.assertEqual((fast.offer["provider"], fast.estimate), (JAMENDO, 3.))
        self.assertEqual((queued.offer["provider"], queued.estimate), (ARCHIVE, 15.))
        index.configure(import_strategy="preferred", import_preferred=ARCHIVE)
        self.assertEqual(rank(index, offers, admission_waits=waits)[0].offer["provider"], ARCHIVE)

    def test_confirmed_plan_reuses_existing_release_before_any_transfer(self):
        index, offers, env = self.equivalent_catalogues()
        _, partial, _ = self.run_import(offers[0]["url"], env=env, expected=1)
        self.assertEqual(partial[0]["added"], 1)
        original = partial[0]["tracks"][0]
        # The first two tracks have independent transfer slots. Preserve
        # the observed order while checking that the retry adds only Jamendo.
        initial_requests = self.requests.read_text().splitlines()
        self.assertCountEqual(initial_requests, ["01.flac", "02.flac"])
        index.configure(import_strategy="preferred", import_preferred=JAMENDO)
        _, complete, _ = self.run_import(create_plan(index, offers), env=env)
        self.assertEqual((complete[0]["status"], complete[0]["provider"], complete[0]["added"]), ("success", JAMENDO, 1))
        self.assertIn(original, complete[0]["tracks"])
        self.assertEqual(self.requests.read_text().splitlines(), [*initial_requests, "39012"])

    def test_canonical_provenance_follows_a_renamed_file_and_avoids_a_second_copy(self):
        track = self.temp / "canonical-source" / "01.flac"
        self.make_local_track(track, 1, album="Album canonique")
        first_target = local_target(track.as_uri(), "Bandcamp", "https://label.bandcamp.com/album/album-canonique")
        _, imported, _ = self.run_import(first_target)
        published = Path(imported[0]["tracks"][0])
        index = Index(self.library, self.temp / "state")

        def scan():
            return inventory(index, None, lambda *_: ("", "absent", {}), lambda: False, lambda *_: None)

        scan()
        with index.db() as db:
            first = dict(db.execute("SELECT * FROM canonical_references").fetchone())
        self.assertTrue(first["song"])
        renamed = published.with_name("Nom corrigé.flac")
        published.rename(renamed)
        manual = renamed.with_suffix(".lrc")
        manual.write_bytes(b"[00:00.20]Correction manuelle.")
        before = renamed.read_bytes()
        scan()
        archive = self.temp / "canonical-second.zip"
        with ZipFile(archive, "w") as output:
            output.write(track, "01.flac")
            output.writestr("01.lrc", "[00:00.10]Copie d’une autre source.")
        second_target = local_target(archive.as_uri(), "Qobuz", "https://www.qobuz.com/fr-fr/album/canonique/0060254719724")
        _, reused, _ = self.run_import(second_target)
        self.assertEqual((reused[0]["status"], reused[0]["added"], reused[0]["tracks"]), ("present", 0, [str(renamed)]))
        self.assertEqual(renamed.read_bytes(), before)
        self.assertEqual(manual.read_bytes(), b"[00:00.20]Correction manuelle.")
        self.assertEqual(list(renamed.parent.glob("*.flac")), [renamed])
        with index.db() as db:
            catalogue = [dict(row) for row in db.execute("SELECT * FROM canonical_references ORDER BY provider")]
        self.assertEqual([row["provider"] for row in catalogue], ["Bandcamp", "Qobuz"])
        self.assertEqual({row["recording"] for row in catalogue}, {first["recording"]})
        self.assertEqual({row["edition"] for row in catalogue}, {first["edition"]})
        self.assertEqual({row["path"] for row in catalogue}, {str(renamed)})
        self.assertEqual(json.loads(catalogue[1]["tags"])["album"], "Album canonique")

        deluxe = self.temp / "canonical-deluxe" / "01.flac"
        self.make_local_track(deluxe, 1, album="Album canonique Deluxe")
        self.assertEqual(pcm_digest(deluxe), pcm_digest(track))
        _, other, _ = self.run_import(deluxe.as_uri())
        self.assertEqual(other[0]["added"], 1)
        self.assertNotEqual(Path(other[0]["tracks"][0]).parent, renamed.parent)
        with index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM canonical_editions").fetchone()[0], 2)

    def test_search_import_and_album_already_present(self):
        command = [sys.executable, "-B", str(BIN / "_kara_catalogue.py")]
        artists = json.loads(subprocess.check_output([*command, "artistes", "Atelier"], cwd=self.temp, env=self.env))
        albums = json.loads(subprocess.check_output(
            [*command, "albums", artists["data"][0]["id"]], cwd=self.temp, env=self.env))
        self.assertEqual(albums["data"]["artist_id"], artists["data"][0]["id"])
        release = albums["data"]["albums"][0]
        edition = json.loads(subprocess.check_output(
            [*command, "edition", release["id"]], cwd=self.temp, env=self.env))["data"]
        self.assertEqual(edition["title"], release["title"])
        self.assertEqual([track["title"] for track in edition["tracks"]], ["Signal un", "Signal deux"])
        url = release["url"]
        _, items, _ = self.run_import(url, cwd=ROOT)
        self.assertEqual((items[0]["status"], items[0]["added"]), ("success", 2))
        tracks = self.check_tracks("100% Signal: A", 2)
        before = {path: path.read_bytes() for path in [*tracks, tracks[0].with_suffix(".lrc"),
                                                       tracks[0].parent / "cover.jpg"]}
        _, items, final = self.run_import(url)
        self.assertEqual((items[0]["category"], final["event"]), ("present", "skip"))
        self.assertEqual(Counter(self.requests.read_text().splitlines()),
                         {"soundGOOD01": 1, "soundGOOD02": 1})
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_discography_route_survives_restart_and_preserves_supplier_credits(self):
        queue = ImportQueue(self.temp / "state", self.library)
        url = "https://music.youtube.com/browse/MPREb_FIXTURE"
        routes = {url: ("Artiste de référence", "UC" + "a" * 22, "MPREb_FIXTURE"),
                  playlist("PLPART"): ("Album hors sélection", "UC" + "a" * 22, "MPREb_FIXTURE")}
        identity = queue.enqueue([("Signal", url)], artist_routes=routes)
        restarted = ImportQueue(self.temp / "state", self.library)
        self.assertEqual(restarted.routes(identity), {url: routes[url]})
        self.assertTrue(restarted.claim(identity))
        with lease(restarted.directory / f"{identity}.lock") as fd:
            worker = self.managed_worker(restarted, identity, fd)
        output, error = worker.communicate(timeout=30)
        self.assertEqual(worker.returncode, 0, (output, error))
        album = restarted.outcomes(identity)[url]
        self.assertEqual(len(album["tracks"]), 2)
        for value in album["tracks"]:
            path = Path(value)
            self.assertEqual(path.relative_to(self.library).parts[0], "Artiste de référence")
            tags, _ = probe(path)
            self.assertEqual(tags["artist"], self.config["artist"])
            self.assertEqual(tags["albumartist"], self.config["artist"])

    def test_kara_local_downloader_is_used_for_playlist_and_transfer(self):
        directory = self.temp / "data/kara/tools"
        directory.mkdir(parents=True)
        shutil.copy2(FIXTURES / "yt-dlp", directory / "yt-dlp")
        path = self.temp / "path"
        path.mkdir()
        rejected = path / "yt-dlp"
        rejected.write_text("#!/bin/sh\nexit 87\n", encoding="utf-8")
        rejected.chmod(0o700)
        env = dict(self.env, PATH=str(path) + os.pathsep + self.env["PATH"])
        _, items, _ = self.run_import(playlist("PLENRICH"), env=env)
        self.assertEqual((items[0]["status"], items[0]["added"]), ("success", 1))
        self.assertEqual(self.requests.read_text().splitlines(), ["soundLRC001"])

    def test_mixed_batch_preserves_diagnostics_and_retries_only_failed_tracks(self):
        successful, interrupted, missing = map(playlist, ("OLAK5uy_FIXTURE", "PLPART", "PLDOWN"))
        bad_url = "https://example.org/album"
        journal, items, _ = self.run_import(interrupted, missing, bad_url,
                                            playlist("PLNETWORK"), successful, expected=1)
        partial, unavailable, invalid, network, success = items
        self.assertEqual([item["status"] for item in items], ["partial", "failure", "failure", "failure", "success"])
        self.assertEqual([item["category"] for item in items], ["partial", "playlist", "path", "network", None])
        self.assertEqual(partial["diagnostics"][0]["category"], "download")
        retained = Path(partial["remaining"]) / ".download/soundPART02.opus"
        self.assertEqual(retained.read_bytes(), b"Unpublished postprocessor draft")
        self.assertIn("FIN DU DIAGNOSTIC", unavailable["diagnostics"][0]["detail"])
        self.assertIn("FIN DU DIAGNOSTIC", Path(journal).read_text())
        self.assertEqual(Path(journal).stat().st_mode & 0o777, 0o600)
        original = Path(partial["tracks"][0])
        first_bytes = original.read_bytes()
        self.config["transfer_failures"].clear()
        del self.config["playlist_failures"]["PLDOWN"]
        self.update_config()
        _, retry, _ = self.run_import(interrupted, missing, successful, previous=journal)
        self.assertEqual([(item["status"], item["added"]) for item in retry], [("success", 1), ("success", 1)])
        self.assertEqual(original.read_bytes(), first_bytes)
        self.assertTrue(retained.is_file())
        self.check_tracks("Deux pistes", 2)
        self.check_tracks("Retour", 1)
        self.assertEqual(Counter(self.requests.read_text().splitlines()),
                         {"soundGOOD01": 1, "soundGOOD02": 1, "soundPART01": 1, "soundPART02": 2, "soundDOWN01": 1})

    def test_youtube_limit_stops_a_new_batch_without_blocking_a_local_import(self):
        self.config["transfer_errors"] = {"soundLRC001":
            "Video unavailable. This content isn't available, try again later. "
            "Your account has been rate-limited by YouTube for up to an hour."}
        self.update_config()
        blocked = playlist("PLENRICH")
        _, albums, _ = self.run_import(blocked, expected=1)
        self.assertEqual(albums[0]["category"], "rate")
        self.assertEqual(self.requests.read_text().splitlines(), ["soundLRC001"])

        queue = ImportQueue(self.temp / "state", self.library)
        local = self.temp / "source.flac"
        self.make_local_track(local, 1)
        identity = queue.enqueue([("Autre album", playlist("OLAK5uy_FIXTURE")), ("Local", local.as_uri())])
        worker = self.launch_managed(queue, identity)
        _, stderr = worker.communicate(timeout=25)
        self.assertEqual(worker.returncode, 1, stderr)
        outcomes = queue.outcomes(identity)
        self.assertEqual(outcomes[playlist("OLAK5uy_FIXTURE")]["category"], "provider_hold")
        self.assertEqual(outcomes[local.as_uri()]["status"], "success")
        self.assertEqual(self.requests.read_text().splitlines(), ["soundLRC001"])

    def test_enrichment_retry_uses_published_tracks_without_downloading(self):
        self.config["lyric_failures"] = ["Enrich"]
        self.update_config()
        url = playlist("PLENRICH")
        journal, items, _ = self.run_import(url, expected=1)
        self.assertTrue(items[0]["audio_complete"])
        self.assertEqual(items[0]["status"], "partial")
        self.assertTrue(all("LRCLIB" in item["detail"] for item in items[0]["diagnostics"]))
        self.assertEqual({item["category"] for item in items[0]["diagnostics"]}, {"processing"})
        self.config["lyric_failures"].clear()
        # Even playlist resolution is unnecessary when only enrichment failed.
        self.config["playlist_failures"]["PLENRICH"] = MISSING
        self.update_config()
        _, retry, _ = self.run_import(url, previous=journal)
        self.assertEqual((retry[0]["status"], retry[0]["added"]), ("success", 0))
        self.check_tracks("Enrich", 1)
        self.assertEqual(self.requests.read_text().splitlines(), ["soundLRC001"])

    def test_playlist_that_omits_an_announced_track_stays_partial(self):
        self.config["playlists"]["PLPART"]["tracks"][1]["hidden"] = True
        self.update_config()
        _, items, _ = self.run_import(playlist("PLPART"), expected=1)
        self.assertEqual((items[0]["status"], items[0]["added"]), ("partial", 1))
        self.assertEqual(items[0]["diagnostics"][0]["category"], "playlist")
        self.check_tracks("Deux pistes", 1)

    def test_missing_sorter_and_invalid_inbox_do_not_start_download(self):
        checkout = self.temp / "copie déplacée" / "bin"
        shutil.copytree(BIN, checkout, ignore=shutil.ignore_patterns("musique-tri", "__pycache__", "_kara_dtw"))
        _, items, error = self.run_import(playlist("PLPART"), command=checkout / "kara", expected=1)
        self.assertFalse(items)
        self.assertEqual(error["category"], "dependency")
        self.assertIn(str(checkout / "musique-tri"), error["detail"])
        self.inbox.rmdir()
        self.inbox.symlink_to(self.temp, target_is_directory=True)
        _, items, error = self.run_import(playlist("PLPART"), expected=1)
        self.assertFalse(items)
        self.assertEqual(error["category"], "path")
        self.assertEqual(self.requests.read_text(), "")

    def test_missing_external_downloader_is_reported_per_album(self):
        deps = self.temp / "deps"
        deps.mkdir()
        for name in ("ffmpeg", "ffprobe"):
            (deps / name).symlink_to(TOOLS[name])
        _, items, _ = self.run_import(playlist("PLPART"), env=dict(self.env, PATH=str(deps)), expected=1)
        self.assertEqual(items[0]["category"], "dependency")
        self.assertIn("yt-dlp", items[0]["diagnostics"][0]["detail"])
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_direct_playlist_reimport_reuses_published_audio(self):
        url = playlist("PLPART")
        self.config["transfer_failures"].clear()
        self.update_config()
        self.run_import(url)
        _, items, _ = self.run_import(url)
        self.assertEqual((items[0]["status"], items[0]["added"]), ("present", 0))
        self.check_tracks("Deux pistes", 2)
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"soundPART01": 1, "soundPART02": 1})

    def test_concurrent_imports_publish_each_track_once(self):
        self.config["transfer_failures"].clear()
        self.update_config()
        arguments = [BIN / "kara", "--suivi-json", "--", playlist("PLPART")]
        processes = []
        try:
            for _ in range(2):
                processes.append(subprocess.Popen(arguments, cwd=self.temp, env=self.env, text=True,
                                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE))
            published = []
            for process in processes:
                stdout, stderr = process.communicate(timeout=45)
                self.assertEqual(process.returncode, 0, stderr)
                records = [json.loads(line) for line in stdout.splitlines()]
                published.extend(record["added"] for record in records if record["event"] == "album")
            self.assertEqual(sorted(published), [0, 2])
            self.check_tracks("Deux pistes", 2)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
                    process.communicate(timeout=10)

    def managed_worker(self, queue, identity, fd):
        child = subprocess.Popen([BIN / "kara", "--suivi-json", "--import-file", identity,
                                  "--import-lease", str(fd)], env=self.env, cwd=self.temp,
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, pass_fds=(fd,))
        self.addCleanup(self.stop_worker, child)
        return child

    @staticmethod
    def stop_worker(worker):
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=10)

    def until(self, predicate, seconds=20):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.04)
        self.fail("La fixture n’a pas atteint l’état attendu")

    def launch_managed(self, queue, identity):
        with lease(queue.directory / f"{identity}.lock") as fd:
            self.assertTrue(queue.claim(identity))
            return self.managed_worker(queue, identity, fd)

    def await_pause(self, queue, *workers):
        start = time.monotonic()
        self.assertTrue(queue.configure(paused=True))
        self.assertEqual(queue.pause_state(), "suspending")
        self.until(lambda: queue.pause_state() == "paused", seconds=5)
        print(f"Pause confirmée en {time.monotonic() - start:.3f} s ({len(workers)} moteur(s)).", flush=True)
        for worker in workers:
            stdout, stderr = worker.communicate(timeout=5)
            self.assertEqual(worker.returncode, 0, (stdout, stderr))
            if worker.args[0] == BIN / "kara":
                self.assertEqual(json.loads(stdout.splitlines()[-1])["event"], "suspended")
        for resource, capacity in ((".kara-transferts", MAX_DOWNLOADS), (".kara-traitements", PROCESSING)):
            self.assertEqual(slot_count(self.inbox / resource, capacity), 0)

    def test_pause_real_audio_preserves_ranges_and_resumes_after_restart(self):
        original = self.temp / "transfert.opus"
        subprocess.run([TOOLS["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi",
                        "-i", "sine=frequency=440:duration=12", "-c:a", "libopus",
                        "-b:a", "192k", "-vbr", "off", str(original)], check=True)
        audio = original.read_bytes()
        self.config["fixed_transfer_buffer"] = True
        for concurrent in (1, 2):
            with self.subTest(concurrent=concurrent):
                release = Event()
                offsets = []

                class ThrottledAudio(QuietHTTP):
                    def do_GET(self):
                        start = int(self.headers.get("Range", "bytes=0-").split("=")[1].split("-")[0])
                        offsets.append(start)
                        self.send_response(206 if self.headers.get("Range") else 200)
                        if self.headers.get("Range"):
                            self.send_header("Content-Range", f"bytes {start}-{len(audio) - 1}/{len(audio)}")
                        self.send_header("Content-Length", str(len(audio) - start))
                        self.end_headers()
                        try:
                            self.wfile.write(audio[start:start + 196608])
                            release.wait(12)
                            self.wfile.write(audio[start + 196608:])
                        except OSError:
                            pass

                with ThreadingHTTPServer(("127.0.0.1", 0), ThrottledAudio) as server:
                    server_thread = Thread(target=server.serve_forever)
                    server_thread.start()
                    try:
                        self.config["media"] = f"http://127.0.0.1:{server.server_port}/audio.opus"
                        queue = ImportQueue(self.temp / "state", self.library)
                        identities = []
                        for number in range(concurrent):
                            key = f"PLPAUSE{concurrent}{number}"
                            self.config["playlists"][key] = {"title": key, "tracks": [
                                {"id": f"pause{concurrent}{number:05d}", "title": key}]}
                            identities.append(queue.enqueue([(key, playlist(key))]))
                        self.update_config()
                        workers = [self.launch_managed(queue, identity) for identity in identities]
                        self.until(lambda: sum(file.stat().st_size > 0
                                               for file in self.inbox.rglob("*.part")) == concurrent)
                        self.assertEqual(slot_count(self.inbox / ".kara-transferts", MAX_DOWNLOADS), concurrent)
                        self.await_pause(queue, *workers)
                        parts = {part: part.read_bytes() for part in self.inbox.rglob("*.part")}
                        self.assertEqual(len(parts), concurrent)
                        time.sleep(.2)
                        self.assertTrue(all(path.read_bytes() == data for path, data in parts.items()))
                        restarted = ImportQueue(self.temp / "state", self.library)
                        self.assertEqual(restarted.pause_state(), "paused")
                        for identity in identities:
                            self.assertFalse(restarted.claim(identity))
                            self.assertEqual(restarted.job(identity)["state"], "queued")
                            self.assertTrue(Path(restarted.job(identity)["journal"]).is_file())
                        self.assertIsNone(restarted.enqueue_maintenance("automatic", "Auto", playlist("AUTO")))
                        release.set()
                        restarted.configure(paused=False)
                        for identity in identities:
                            worker = self.launch_managed(restarted, identity)
                            stdout, stderr = worker.communicate(timeout=25)
                            self.assertEqual(worker.returncode, 0, (stdout, stderr))
                            outcome, = restarted.outcomes(identity).values()
                            self.assertEqual(outcome["status"], "success")
                            track, = self.check_tracks(outcome["title"], 1, enriched=False)
                            self.assertEqual(pcm_digest(track), pcm_digest(original))
                        self.assertEqual(sum(offset > 0 for offset in offsets), concurrent)
                    finally:
                        release.set()
                        server.shutdown()
                        server_thread.join()

    def test_pause_during_ffmpeg_preserves_complete_staging_and_large_lot(self):
        source = self.temp / "Source"
        self.make_local_track(source / "01.flac", 1)
        self.make_local_track(source / "02.flac", 2)
        queue = ImportQueue(self.temp / "state", self.library)
        first = self.temp / "Premier"
        self.make_local_track(first / "01.flac", 1, album="Premier album")
        targets = [("Premier", first.as_uri()), ("Source", source.as_uri())]
        targets.extend((f"Album {i}", playlist(f"WAIT{i}")) for i in range(318))
        identity = queue.enqueue(targets)
        # Use known completed outcomes to keep this large-lot fixture short.
        for title, url in targets[2:]:
            queue.album(identity, dict(url=url, title=title, status="present", tracks=[]))
        tools = self.temp / "slow"
        tools.mkdir()
        marker = self.temp / "ffmpeg-pid"
        wrapper = tools / "ffmpeg"
        wrapper.write_text("""#!/usr/bin/python3 -B
import os, sys
from pathlib import Path
args = sys.argv[1:]
if any(os.environ['PAUSE_STAGE'] in arg for arg in args) and 'pcm_f32le' in args and Path(os.environ['FFMPEG_ARMED']).exists():
    Path(os.environ['FFMPEG_MARKER']).write_text(str(os.getpid()))
    args.insert(args.index('-i'), '-re')
os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *args])
""")
        wrapper.chmod(0o700)
        armed = self.temp / "armed"
        armed.touch()
        self.env.update(PATH=str(tools) + os.pathsep + self.env["PATH"], REAL_FFMPEG=TOOLS["ffmpeg"],
                        FFMPEG_ARMED=str(armed), FFMPEG_MARKER=str(marker),
                        PAUSE_STAGE=digest("local:" + digest(source.as_uri())))
        worker = self.launch_managed(queue, identity)
        self.until(lambda: marker.exists() and bool(marker.read_text()))
        pid = int(marker.read_text())
        self.until(lambda: Path(f"/proc/{pid}/comm").exists() and
                   Path(f"/proc/{pid}/comm").read_text().strip() == "ffmpeg")
        # Album admission is concurrent. Wait for the independent receipt
        # before interrupting the intentionally slowed PCM subprocess.
        self.until(lambda: first.as_uri() in queue.outcomes(identity), seconds=3)
        original = queue.outcomes(identity)[first.as_uri()]
        self.await_pause(queue, worker)
        self.assertFalse(Path(f"/proc/{pid}").exists())
        self.assertEqual(queue.outcomes(identity)[source.as_uri()]["status"], "suspended")
        staged, = list(self.inbox.glob(".kara-local-*/*.flac"))
        content = staged.read_bytes()
        armed.unlink()
        restarted = ImportQueue(self.temp / "state", self.library)
        restarted.configure(paused=False)
        worker = self.launch_managed(restarted, identity)
        stdout, stderr = worker.communicate(timeout=25)
        self.assertEqual(worker.returncode, 0, (stdout, stderr))
        outcomes = restarted.outcomes(identity)
        self.assertEqual(outcomes[first.as_uri()], original)
        self.assertEqual(len(outcomes), len(targets))
        tracks = sorted(Path(path) for path in outcomes[source.as_uri()]["tracks"])
        self.assertEqual(len(tracks), 2)
        self.assertIn(content, [track.read_bytes() for track in tracks])
        self.assertEqual(len(list((self.library / self.config["artist"]).rglob("*.flac"))), 3)

    def test_youtube_releases_audio_slots_before_ffmpeg_and_resumes(self):
        # Hold the actual FFmpeg metadata postprocessor. yt-dlp must keep
        # its completed draft and a processing lease, with both audio
        # leases free for a different track.
        tools = self.temp / "slow-youtube"
        tools.mkdir()
        marker = self.temp / "conversion-pid"
        wrapper = tools / "ffmpeg"
        wrapper.write_text('''#!/usr/bin/python3 -B
import os, sys, time
from pathlib import Path
if '-metadata' in sys.argv and any('.download' in value for value in sys.argv):
    Path(os.environ['KARA_TEST_CONVERSION']).write_text(str(os.getpid()))
    time.sleep(20)
os.execv(os.environ['REAL_FFMPEG'], [os.environ['REAL_FFMPEG'], *sys.argv[1:]])
''')
        wrapper.chmod(0o700)
        original_path = self.env["PATH"]
        self.env.update(PATH=str(tools) + os.pathsep + original_path, REAL_FFMPEG=TOOLS["ffmpeg"],
                        KARA_TEST_CONVERSION=str(marker))
        queue = ImportQueue(self.temp / "state", self.library)
        url = playlist("PLENRICH")
        identity = queue.enqueue([("Enrich", url)])
        worker = self.launch_managed(queue, identity)
        self.until(lambda: marker.exists() and bool(marker.read_text()))
        pid = int(marker.read_text())
        self.assertEqual(slot_count(self.inbox / ".kara-traitements", PROCESSING), 1)
        self.assertEqual(slot_count(self.inbox / ".kara-transferts", MAX_DOWNLOADS), 0)
        self.assertEqual(slot_count(self.inbox / ".kara-transferts/youtube", MAX_DOWNLOADS), 0)
        self.await_pause(queue, worker)
        journal = Path(queue.job(identity)["journal"])
        events = [json.loads(line) for line in journal.read_text().splitlines()]
        audio = [row["event"] for row in events if row["event"].startswith("audio-") and row["event"] != "audio-phase"]
        self.assertEqual(audio[:3], ["audio-queued", "audio-started", "audio-first-byte"])
        self.assertTrue(audio[3:-1] and set(audio[3:-1]) == {"audio-bytes"})
        self.assertEqual(audio[-1], "audio-transfer-ended")
        self.assertEqual(sum(row["size"] for row in events if row["event"] == "audio-bytes"), len(self.signal))
        self.assertFalse(Path(f"/proc/{pid}").exists())
        draft, = self.inbox.glob(".kara-reprise-*/.download/*.opus")
        self.assertGreater(draft.stat().st_size, 1024)
        self.env["PATH"] = original_path
        queue.configure(paused=False)
        worker = self.launch_managed(queue, identity)
        stdout, stderr = worker.communicate(timeout=25)
        self.assertEqual(worker.returncode, 0, (stdout, stderr))
        self.assertEqual(queue.outcomes(identity)[url]["status"], "success")
        self.check_tracks("Enrich", 1, enriched=False)

    def test_pause_during_import_lyrics_releases_maintenance_and_resumes(self):
        from _parole_import_enrichment import schedule

        marker = self.temp / "lyrics-active"
        track = self.library / self.config["artist"] / "Pause paroles" / "01.flac"
        self.make_local_track(track, 1, album="Pause paroles")
        pcm = pcm_digest(track)
        queue = ImportQueue(self.temp / "state", self.library)
        index = Index(self.library, self.temp / "state")
        subject, = schedule(index, [str(track)])["import_lyrics"]
        self.config["lyric_wait"] = str(marker)
        self.update_config()
        script = """
from pathlib import Path
from time import time
from types import SimpleNamespace
from unittest.mock import patch
from _kara_paths import STATE, music_library
from _kara_import_queue import ImportQueue
from _parole_maintenance import Maintenance
maintenance = Maintenance(music_library(), Path('collection-fixture'), state=STATE, import_queue=ImportQueue())
maintenance.current_settings = maintenance.index.settings()
with patch('_parole_maintenance.disk_usage', return_value=SimpleNamespace(free=50 * 1024 ** 3)):
    assert maintenance.tick_import_enrichment(maintenance.current_settings, time())
"""
        env = dict(self.env, PYTHONPATH=str(BIN) + os.pathsep + str(FIXTURES))

        def launch():
            worker = subprocess.Popen([sys.executable, "-B", "-c", script], cwd=self.temp, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            self.addCleanup(self.stop_worker, worker)
            return worker

        active = launch()
        self.until(marker.exists)
        self.await_pause(queue, active)
        with index.db() as db:
            task = db.execute("SELECT state,attempts FROM tasks WHERE subject=?", (subject,)).fetchone()
            self.assertEqual(tuple(task), ("suspended", 0))
        self.assertFalse(track.with_suffix(".lrc").exists())
        del self.config["lyric_wait"]
        self.update_config()
        queue.configure(paused=False)
        resumed = launch()
        stdout, stderr = resumed.communicate(timeout=20)
        self.assertEqual(resumed.returncode, 0, (stdout, stderr))
        with index.db() as db:
            self.assertEqual(db.execute("SELECT state FROM tasks WHERE subject=?", (subject,)).fetchone()[0], "done")
        self.assertTrue(track.with_suffix(".lrc").is_file())
        self.assertEqual(pcm_digest(track), pcm)

    def test_lost_worker_after_sort_starts_reuses_publication_receipt(self):
        self.config["transfer_failures"].clear()
        self.update_config()
        queue = ImportQueue(self.temp / "state", self.library)
        identity = queue.enqueue([("Deux pistes", playlist("PLPART"))])
        lock = queue.directory / f"{identity}.lock"
        with (self.library / ".labfy-music.lock").open("a") as library_lock:
            flock(library_lock, LOCK_EX)
            self.assertTrue(queue.claim(identity))
            with lease(lock) as fd:
                worker = self.managed_worker(queue, identity, fd)
            # The real sorter is blocked under the library lock. Simulate the
            # import process disappearing while its admitted child is alive.
            waiting = Event()

            def sorter_started():
                for line in worker.stdout:
                    if "En attente de la bibliothèque" in line:
                        waiting.set()
                        break

            reader = Thread(target=sorter_started, daemon=True)
            reader.start()
            self.assertTrue(waiting.wait(20))
            reader.join()
            # The first sorter owns one track; the second conversion may
            # finish while it waits. Hold the mutation lease until both are
            # staged so restart must reuse both without another transfer.
            self.until(lambda: len(list(self.inbox.glob(".kara-reprise-*/*.opus"))) == 2)
            worker.kill()
            worker.communicate(timeout=10)
            self.assertEqual(queue.job(identity)["state"], "running")
            with lease(lock, wait=False) as fd:
                self.assertIsNone(fd)
            self.assertEqual(queue.outcomes(identity), {})
            self.assertEqual(list(self.library.rglob("*.lrc")), [])
        self.until(lambda: len(list((self.library / "Atelier test").rglob("*.opus"))) == 1)
        with lease(lock) as fd:
            self.assertTrue(queue.transition(identity, "running", "queued", "Interrompu"))
            self.assertTrue(queue.claim(identity))
            restarted = self.managed_worker(queue, identity, fd)
        stdout, stderr = restarted.communicate(timeout=35)
        self.assertEqual(restarted.returncode, 0, (stdout, stderr))
        self.assertEqual(queue.job(identity)["state"], "done")
        self.assertTrue(queue.job(identity)["refresh"])
        self.check_tracks("Deux pistes", 2, enriched=False)
        self.assertEqual(list(self.library.rglob("*.lrc")), [])
        self.assertTrue(queue.outcomes(identity)[playlist("PLPART")]["enrichment"]["import_lyrics"])
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"soundPART01": 1, "soundPART02": 1})

    def test_one_lot_downloads_tracks_from_several_albums(self):
        release = self.hold_media_bodies()
        targets = []
        for number in range(YOUTUBE_DEFAULT_SLOTS):
            key = f"PLPARALLEL{number}"
            self.config["playlists"][key] = {"title": key, "tracks": [
                {"id": f"parallel{number:03d}", "title": key}]}
            targets.append((key, playlist(key)))
        self.update_config()
        queue = ImportQueue(self.temp / "state", self.library)
        identity = queue.enqueue(targets)
        worker = self.launch_managed(queue, identity)
        self.until(lambda: slot_count(self.inbox / ".kara-transferts", MAX_DOWNLOADS) == YOUTUBE_DEFAULT_SLOTS)
        release.set()
        stdout, stderr = worker.communicate(timeout=35)
        self.assertEqual(worker.returncode, 0, (stdout, stderr))
        self.assertEqual(queue.totals(), (YOUTUBE_DEFAULT_SLOTS,) * 4)
        self.assertEqual(Counter(self.requests.read_text().splitlines()),
                         {f"parallel{number:03d}": 1 for number in range(YOUTUBE_DEFAULT_SLOTS)})

    def test_single_cli_album_uses_configured_track_profile(self):
        release = self.hold_media_bodies()
        name = "PLCLISLOTS"
        self.config["playlists"][name] = {"title": "Album CLI", "tracks": [
            {"id": f"cli{number:08d}", "title": f"Piste {number}"} for number in range(5)]}
        self.update_config()
        env = dict(self.env, KARA_YOUTUBE_AUDIO_SLOTS="5")
        worker = subprocess.Popen([str(BIN / "kara"), "--suivi-json", "--", playlist(name)], env=env,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        self.addCleanup(self.stop_worker, worker)
        self.until(lambda: slot_count(self.inbox / ".kara-transferts", MAX_DOWNLOADS) == 5)
        release.set()
        stdout, stderr = worker.communicate(timeout=35)
        self.assertEqual(worker.returncode, 0, (stdout, stderr))
        album, = (row for row in map(json.loads, stdout.splitlines()) if row["event"] == "album")
        self.assertEqual(album["added"], 5)

    def test_four_lots_share_download_slots_and_finish_after_client_exit(self):
        release = self.hold_media_bodies()
        targets = []
        for number in range(4):
            title = f"Album {number}"
            key = f"PLMASS{number}"
            self.config["playlists"][key] = {"title": title, "tracks": [
                {"id": f"mass0000{number}{index}0", "title": f"Titre {index}"} for index in range(2)]}
            targets.append(playlist(key))
        self.update_config()
        queue = ImportQueue(self.temp / "state", self.library)
        workers, ids = [], []
        for url in targets:
            identity = queue.enqueue([("", url)])
            ids.append(identity)
            self.assertTrue(queue.claim(identity))
            with lease(queue.directory / f"{identity}.lock") as fd:
                workers.append(self.managed_worker(queue, identity, fd))
        self.until(lambda: slot_count(self.inbox / ".kara-transferts", MAX_DOWNLOADS) == YOUTUBE_DEFAULT_SLOTS)
        self.assertEqual(queue.count(), {"running": 4})
        # Closing the progress pipes models GTK going away without stopping
        # the workers. Journals and SQLite results remain available on restart.
        for worker in workers:
            worker.stdout.close()
            worker.stdout = None
        release.set()
        for worker, identity in zip(workers, ids):
            self.assertEqual(worker.wait(timeout=45), 0, queue.outcomes(identity))
        restarted = ImportQueue(self.temp / "state", self.library)
        self.assertEqual(restarted.count(), {"done": 4})
        self.assertEqual(restarted.totals(), (4, 8, 4, 4))
        self.assertTrue(all(restarted.job(identity)["refresh"] for identity in ids))
        self.assertEqual(len(self.requests.read_text().splitlines()), 8)

    def test_low_disk_blocks_transfers(self):
        wrapper = self.temp / "low_disk.py"
        wrapper.write_text(
            "import runpy, shutil, sys\n"
            "from pathlib import Path\n"
            "real = shutil.disk_usage\n"
            "shutil.disk_usage = lambda path: real(path)._replace(free=0) if 'A_trier' in str(path) else real(path)\n"
            "kara = sys.argv.pop(1)\n"
            "sys.path.insert(0, str(Path(kara).parent))\n"
            "runpy.run_path(kara, run_name='__main__')\n")
        result = subprocess.run([sys.executable, wrapper, BIN / "kara", "--suivi-json", "--", playlist("PLPART")],
                                cwd=self.temp, env=self.env, capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 1, result.stderr)
        album = next(row for row in map(json.loads, result.stdout.splitlines()) if row["event"] == "album")
        self.assertEqual(album["category"], "space")
        self.assertEqual(self.requests.read_text(), "")
        self.assertEqual(list(self.inbox.iterdir()), [])

    def test_shortcut_uses_moved_checkout_from_foreign_cwd(self):
        checkout = self.temp / "copie déplacée $ 100%" / "bin"
        checkout.mkdir(parents=True)
        for name in ("installer-parole", "_kara_paths.py"):
            shutil.copy2(BIN / name, checkout)
        stub = checkout / "paroles-strawberry"
        marker = self.temp / "desktop-launched"
        stub.write_text("#!/usr/bin/python3\nfrom pathlib import Path\n" + f"Path({str(marker)!r}).touch()\n")
        stub.chmod(0o755)
        result = subprocess.check_output([checkout / "installer-parole"], cwd="/", env=self.env, text=True)
        desktop = Path(result.strip())
        self.assertIn(r"\\$ 100%%", desktop.read_text())
        for tool, arguments in (("desktop-file-validate", []), ("gio", ["launch"])):
            if shutil.which(tool):
                subprocess.run([tool, *arguments, desktop], cwd="/", env=self.env, check=True)
        if shutil.which("gio"):
            import time
            for _ in range(30):
                if marker.is_file():
                    break
                time.sleep(0.1)
            self.assertTrue(marker.is_file())

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK"), "set PAROLE_TEST_GTK=1 in a GTK session")
    def test_gtk_details_and_retry_button(self):
        script = self.temp / "gtk_test.py"
        script.write_text('''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, sys.argv[1])
from _kara_import import AlbumImport

def wait_for(predicate):
    deadline = time.monotonic() + 20
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        time.sleep(.02)
    raise AssertionError("GTK did not reach the expected state")

window = Gtk.Window()
panel = AlbumImport()
window.set_child(panel)
panel.set_reveal_child(True)
window.present()
assert panel.start_queue([("Un succès", sys.argv[2]), ("Playlist en échec", sys.argv[3])])
first = panel.imports[0]
wait_for(lambda: first.done)
assert first.details_button.get_visible() and first.retry_button.get_visible()
assert first.failed_jobs == [("Playlist en échec", sys.argv[3])]
first.details_button.emit("clicked")
details = next(item for item in Gtk.Window.list_toplevels() if item.get_title() == "Détails de l’import")
scroll = details.get_child().get_first_child()
buffer = scroll.get_child().get_buffer()
assert "FIN DU DIAGNOSTIC" in buffer.get_text(*buffer.get_bounds(), True)
details.get_child().get_last_child().emit("clicked")
journal = next(item for item in Gtk.Window.list_toplevels() if item.get_title() == "Journal de l’import")
buffer = journal.get_child().get_child().get_buffer()
assert "FIN DU DIAGNOSTIC" in buffer.get_text(*buffer.get_bounds(), True)
journal.close()
details.close()
path = Path(os.environ["PAROLE_TEST_CONFIG"])
config = json.loads(path.read_text())
del config["playlist_failures"]["PLDOWN"]
path.write_text(json.dumps(config))
first.retry_button.emit("clicked")
assert not first.retry_button.get_visible()
retry_job = panel.imports[-1]
assert retry_job is not first and retry_job.identity == first.identity
wait_for(lambda: retry_job.done)
assert retry_job.outcomes[sys.argv[3]]["status"] == "success"
window.close()
''', encoding="utf-8")
        env = dict(self.env, GTK_A11Y="none")
        subprocess.run(["dbus-run-session", "--", sys.executable, "-B", script, BIN,
                        "https://music.youtube.com/browse/MPREb_FIXTURE", playlist("PLDOWN")],
                       cwd=self.temp, env=env, timeout=55, check=True)
        self.assertEqual(Counter(self.requests.read_text().splitlines()),
                         {"soundGOOD01": 1, "soundGOOD02": 1, "soundDOWN01": 1})


    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK"), "set PAROLE_TEST_GTK=1 in a GTK session")
    def test_mobile_queue_pause_low_disk_and_library_refresh(self):
        script = self.temp / "gtk_shared.py"
        script.write_text('''
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib
from concurrent.futures import ThreadPoolExecutor
from importlib.machinery import SourceFileLoader
from importlib.util import module_from_spec, spec_from_loader
from pathlib import Path
from types import MethodType, SimpleNamespace
from unittest.mock import patch
import sys
import time

sys.path.insert(0, sys.argv[1])
from _kara_import import AlbumImport
from _kara_import_result import ImportIssue
from _kara_mobile_imports import MobileImports

loader = SourceFileLoader("parole_window_fixture", str(Path(sys.argv[1]) / "paroles-strawberry"))
spec = spec_from_loader(loader.name, loader)
module = module_from_spec(spec)
loader.exec_module(module)
window = module.LyricsWindow
catalogues = []
room = SimpleNamespace(update_catalogue=lambda rows, **_kwargs: catalogues.append(rows))
control = SimpleNamespace(receiver=SimpleNamespace(room=room), closed=False, music_window=None,
                          blackbox=SimpleNamespace(emit=lambda *args, **kwargs: None),
                          remote_party=SimpleNamespace(locked=False), import_refresh_tasks={},
                          import_refresh_timer=None, import_refresh_active=False, import_published_paths=set(),
                          import_removed_paths=set())
for name in ("refresh_after_import", "reload_after_import"):
    setattr(control, name, MethodType(getattr(window, name), control))

panel = AlbumImport()
assert panel.configure(paused=True)
mobile = MobileImports(control)
control.album_import, control.mobile_imports = panel, mobile
mobile.bind_queue(panel.queue)
panel.on_update = mobile.update
album_id = "MPREb_FIXTURE"
url = f"https://music.youtube.com/browse/{album_id}"
desktop = panel.enqueue([("Édition fixture", url)])
for number in range(40):
    mobile.discographies["fixture"] = {"name": f"Artiste {number}", "albums": [
        {"id": f"MPREb_{number}", "title": f"Album {number}"}]}
    mobile.request("token", "participant", {"handle": "fixture", "all": True, "rights": True})
mobile.discographies["fixture"] = {"name": "Artiste fixture", "albums": [{"id": album_id, "title": "Édition fixture"}]}
request = mobile.request("token", "participant", {"handle": "fixture", "all": True, "rights": True})
mobile.action(request["job"], "approve")
assert panel.queue.count() == {"queued": 1}
assert mobile.jobs[request["job"]]["task_id"] == desktop.identity

# Recreate both frontends with the same queue and the same scoped state.
panel.closing = True
panel = AlbumImport()
mobile = MobileImports(control)
control.album_import, control.mobile_imports = panel, mobile
mobile.bind_queue(panel.queue)
panel.on_update = mobile.update
assert len(mobile.snapshot(organizer=True)) >= 40
assert panel.queue.settings() == (True, 4)
with patch("_kara_import.check_space", side_effect=ImportIssue("space", "Fixture disque plein")):
    assert panel.configure(paused=False)
    assert panel.queue.count() == {"queued": 1}
    assert "Fixture disque plein" in panel.queue_status.get_text()
with ThreadPoolExecutor(max_workers=1) as pool:
    control.tag_reader = pool
    panel.launch_next(force=True)
    deadline = time.monotonic() + 25
    context = GLib.MainContext.default()
    while time.monotonic() < deadline:
        while context.pending():
            context.iteration(False)
        panel.launch_next()
        if catalogues and not control.import_refresh_active:
            break
        time.sleep(.02)
    assert len(catalogues) == 1
    assert len(catalogues[0]) == 2
    assert mobile.jobs[request["job"]]["state"] == "done"
    assert panel.queue.job(desktop.identity)["refresh"] == 0
    panel.launch_next(force=True)
    assert panel.imports_snapshot[1]["tracks"] == 0
    assert panel.imports_snapshot[1]["progress"] == 0
panel.closing = True
''', encoding="utf-8")
        subprocess.run(["dbus-run-session", "--", sys.executable, "-B", script, BIN], cwd=self.temp,
                       env=dict(self.env, GTK_A11Y="none"), timeout=35, check=True)

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK"), "set PAROLE_TEST_GTK=1 in a GTK session")
    def test_import_window_uses_available_height_with_a_long_queue(self):
        validation = """
import runpy
import time
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
from _kara_import import AlbumImport

controller = runpy.run_path("BIN/paroles-strawberry", run_name="parole_layout_fixture")["LyricsWindow"]
owner = Gtk.Window()
owner.album_import = panel = AlbumImport()
owner.import_window = None
owner.import_toggle = Gtk.ToggleButton()
owner.import_toggle.connect("toggled", panel.toggle)
panel.queue.configure(paused=True)
with panel.queue.connect(write=True) as db:
    for number in range(350):
        panel.queue.enqueue([(f"Lot {number}", f"https://www.youtube.com/playlist?list=LAYOUT{number}")], db=db)
panel.launch_next(force=True)
owner.present()
controller.open_import(owner)
window = owner.import_window
assert window.get_child() is panel
adjustment = panel.import_view.get_vadjustment()
context = GLib.MainContext.default()
deadline = time.monotonic() + 3
while not panel.import_view.get_mapped() and time.monotonic() < deadline:
    context.iteration(False)
    time.sleep(.01)

def layout(height):
    # Exercise GTK size allocation independently of a compositor's tiling
    # policy. A tiled Wayland toplevel can ignore application size requests.
    window.allocate(900, height, -1, None)
    return adjustment.get_page_size()

short = layout(440)
tall = layout(820)
assert tall > short + 250, (short, tall)
assert adjustment.get_upper() > tall
assert len(panel.by_id) == 350
footer = panel.history_button.compute_bounds(window)[1]
assert footer.get_y() + footer.get_height() > window.get_height() - 40
with panel.queue.connect(write=True) as db:
    db.execute("UPDATE jobs SET state='cancelled'")
panel.launch_next(force=True)
context = GLib.MainContext.default()
deadline = time.monotonic() + 3
while panel.import_view.get_vscrollbar().get_child_visible() and time.monotonic() < deadline:
    while context.pending():
        context.iteration(False)
    time.sleep(.01)
assert adjustment.get_upper() <= adjustment.get_page_size()
assert not panel.import_view.get_vscrollbar().get_child_visible()
window.destroy()
owner.destroy()
""".replace("BIN/paroles-strawberry", str(BIN / "paroles-strawberry"))
        subprocess.run(["dbus-run-session", "--", sys.executable, "-B", "-c", validation], cwd=self.temp,
                       env=dict(self.env, PYTHONPATH=str(BIN), GTK_A11Y="none"), timeout=20, check=True)

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK"), "set PAROLE_TEST_GTK=1 in a GTK session")
    def test_history_window_multiselect_success_filter_and_confirmation(self):
        validation = """
import gi
gi.require_version('Gtk', '4.0')
from gi.repository import GLib, Gtk
from _kara_import import AlbumImport
from _kara_import_queue import ImportQueue

panel = AlbumImport()
owner = Gtk.Window(child=panel)
queue = panel.queue
assert panel.configure(paused=True)

def archive(index, state):
    job = queue.enqueue([(f'Clôture {index}', f'https://www.youtube.com/watch?v=history{index:04}')])
    assert queue.transition(job, 'queued', state, 'Clôturé')
    return job

success = archive(0, 'done')
selection = [archive(1, 'failed'), archive(2, 'cancelled')]
last = archive(3, 'partial')
individual = archive(4, 'review')
panel.launch_next(force=True)
assert set(panel.by_id) == {selection[0], last, individual}
panel.by_id[individual].remove_button.emit('clicked')
assert individual not in panel.by_id
panel.show_history(panel.history_button)
history = panel.history_window

def traverse(widget):
    yield widget
    child = widget.get_first_child()
    while child is not None:
        yield from traverse(child)
        child = child.get_next_sibling()

def button(widget, label):
    return next(item for item in traverse(widget) if isinstance(item, Gtk.Button) and item.get_label() == label)

rows = next(item for item in traverse(history) if isinstance(item, Gtk.ListBox))
for row in traverse(rows):
    if isinstance(row, Gtk.ListBoxRow) and row.get_child().identity in selection:
        rows.select_row(row)
assert len(rows.get_selected_rows()) == 2
button(history, 'Supprimer la sélection').emit('clicked')
assert {row['id'] for row in queue.history()} == {success, last}
assert set(panel.by_id) == {last}
assert panel.imports_snapshot[1]['errors'] == 1
button(history, 'Effacer les importations réussies').emit('clicked')
assert [row['id'] for row in queue.history()] == [last]

def respond(label):
    button(history, 'Effacer tout l’historique').emit('clicked')
    context = GLib.MainContext.default()
    while context.pending():
        context.iteration(False)
    dialog = next(win for win in Gtk.Window.list_toplevels() if win.get_transient_for() == history)
    button(dialog, label).emit('clicked')
    while context.pending():
        context.iteration(False)

respond('Annuler')
assert len(queue.history()) == 1
respond('Effacer')
assert queue.history() == []
assert rows.get_row_at_index(0) is None
assert panel.by_id == {}
assert panel.imports_snapshot[1]['errors'] == 0
assert panel.overall.get_fraction() == 0
assert ImportQueue().rows() == []
retention = next(item for item in traverse(history) if isinstance(item, Gtk.DropDown))
retention.set_selected(2)
assert ImportQueue().history_policy() == (30, 4)
assert panel.imports_snapshot[1]['history_revision'] == 4
history.close()
assert panel.history_window is None
owner.destroy()
"""
        subprocess.run(["dbus-run-session", "--", sys.executable, "-B", "-c", validation], cwd=self.temp,
                       env=dict(self.env, PYTHONPATH=str(BIN), GTK_A11Y="none"), timeout=20, check=True)


if __name__ == "__main__":
    unittest.main()
