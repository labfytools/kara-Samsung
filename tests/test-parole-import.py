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
from threading import Thread
import unittest
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FIXTURES = ROOT / "tests/fixtures/parole"
sys.path.insert(0, str(BIN))
from _kara_audio_identity import pcm_digest
from _kara_import_local import local_target
from _kara_import_queue import ImportQueue
from _kara_import_resources import DOWNLOADS, PROCESSING, lease, slot_count
from _kara_import_result import ImportIssue
from _parole_index import Index
from _parole_import_providers import ARTIST_SITES, OPEN_LICENSES
from _parole_sources import controls, seed

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

    def test_only_acknowledged_imports_move_to_history(self):
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
            self.assertEqual(queue.history(), [])
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

    def check_tracks(self, album, count):
        directory = self.library / self.config["artist"] / album
        tracks = sorted(directory.glob("*.opus"))
        self.assertEqual(len(tracks), count, list(directory.glob("*")))
        self.assertGreater((directory / "cover.jpg").stat().st_size, 0)
        for index, track in enumerate(tracks, 1):
            self.assertTrue(track.name.startswith(f"{index:02d} - "))
            self.assertIn("[00:00.20]Son créé pour le test.", track.with_suffix(".lrc").read_text())
            exported = json.loads(subprocess.check_output(
                [TOOLS["ffprobe"], "-v", "error", "-show_entries", "stream_tags", "-of", "json", str(track)]
            ))["streams"][0]["tags"]
            tags = {key.casefold(): value for key, value in exported.items()}
            self.assertEqual(tags["album"], album)
            self.assertEqual(tags["track"], str(index))
            self.assertEqual(tags["lyrics"], "Son créé pour le test.")
        return tracks

    def make_local_track(self, path, number, *, album="Album local"):
        path.parent.mkdir(exist_ok=True, parents=True)
        subprocess.run([TOOLS["ffmpeg"], "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"sine=frequency={220 + 110 * number}:duration=4", "-c:a", "flac",
                        "-metadata", f"artist={self.config['artist']}",
                        *(["-metadata", f"album={album}"] if album else []),
                        "-metadata", f"title=Son local {number}", "-metadata", f"track={number}", str(path)],
                       check=True)

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
        self.assertEqual([row["member"] for row in complete["provenance"]], ["01.flac", "02.flac"])
        self.assertEqual(complete["provenance"][0]["external_id"], partial["provenance"][0]["external_id"])
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(published.read_bytes(), before)
        self.assertEqual(pcm_digest(complete["tracks"][1]), pcm_digest(second))
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

    def test_search_import_and_album_already_present(self):
        command = [sys.executable, "-B", str(BIN / "_kara_catalogue.py")]
        artists = json.loads(subprocess.check_output([*command, "artistes", "Atelier"], cwd=self.temp, env=self.env))
        albums = json.loads(subprocess.check_output(
            [*command, "albums", artists["data"][0]["id"]], cwd=self.temp, env=self.env))
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
            self.until(lambda: slot_count(self.inbox / ".kara-traitements", PROCESSING) == 1)
            worker.kill()
            worker.communicate(timeout=10)
            self.assertEqual(queue.job(identity)["state"], "running")
            with lease(lock, wait=False) as fd:
                self.assertIsNone(fd)
            self.assertEqual(queue.outcomes(identity), {})
            self.assertEqual(list(self.library.rglob("*.lrc")), [])
        self.until(lambda: len(list((self.library / "Atelier test").rglob("*.opus"))) == 2)
        with lease(lock) as fd:
            self.assertTrue(queue.transition(identity, "running", "queued", "Interrompu"))
            self.assertTrue(queue.claim(identity))
            restarted = self.managed_worker(queue, identity, fd)
        stdout, stderr = restarted.communicate(timeout=35)
        self.assertEqual(restarted.returncode, 0, (stdout, stderr))
        self.assertEqual(queue.job(identity)["state"], "done")
        self.assertTrue(queue.job(identity)["refresh"])
        self.check_tracks("Deux pistes", 2)
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"soundPART01": 1, "soundPART02": 1})

    def test_four_lots_share_download_slots_and_finish_after_client_exit(self):
        self.config["transfer_delay"] = 2
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
        self.until(lambda: slot_count(self.inbox / ".kara-transferts", DOWNLOADS) == DOWNLOADS)
        self.assertEqual(queue.count(), {"running": 4})
        # Closing the progress pipes models GTK going away without stopping
        # the workers. Journals and SQLite results remain available on restart.
        for worker in workers:
            worker.stdout.close()
            worker.stdout = None
        for worker in workers:
            self.assertEqual(worker.wait(timeout=45), 0)
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
room = SimpleNamespace(update_catalogue=catalogues.append)
control = SimpleNamespace(receiver=SimpleNamespace(room=room), closed=False, music_window=None,
                          blackbox=SimpleNamespace(emit=lambda *args, **kwargs: None),
                          remote_party=SimpleNamespace(locked=False), import_refresh_tasks={},
                          import_refresh_timer=None, import_refresh_active=False)
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
        if catalogues:
            break
        time.sleep(.02)
    assert len(catalogues) == 1
    assert len(catalogues[0]) == 2
    assert mobile.jobs[request["job"]]["state"] == "done"
    assert panel.queue.job(desktop.identity)["refresh"] == 0
    assert panel.imports_snapshot[1]["tracks"] == 2
panel.closing = True
''', encoding="utf-8")
        subprocess.run(["dbus-run-session", "--", sys.executable, "-B", script, BIN], cwd=self.temp,
                       env=dict(self.env, GTK_A11Y="none"), timeout=35, check=True)


if __name__ == "__main__":
    unittest.main()
