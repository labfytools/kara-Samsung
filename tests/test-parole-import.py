#!/usr/bin/python3 -B
"""Isolated import regressions with real local media, yt-dlp and FFmpeg."""
from collections import Counter
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Thread
import unittest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "bin"
FIXTURES = ROOT / "tests/fixtures/parole"
TOOLS = {name: shutil.which(name) for name in ("yt-dlp", "ffmpeg", "ffprobe")}


def playlist(name):
    return f"https://www.youtube.com/playlist?list={name}"


MISSING = ("WARNING: [youtube:tab] YouTube said: The playlist does not exist.\n"
           "ERROR: [youtube:tab] PLDOWN: YouTube said: The playlist does not exist.\n"
           "Détail complet de l’extracteur : " + "donnée " * 42 + "FIN DU DIAGNOSTIC")


class QuietHTTP(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


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
                        PYTHONPATH=str(FIXTURES), PAROLE_TEST_CONFIG=str(self.manifest))
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

    def test_direct_playlist_reimport_deduplicates_during_sort(self):
        url = playlist("PLPART")
        self.config["transfer_failures"].clear()
        self.update_config()
        self.run_import(url)
        _, items, _ = self.run_import(url)
        self.assertEqual((items[0]["status"], items[0]["added"]), ("present", 0))
        self.check_tracks("Deux pistes", 2)
        self.assertEqual(Counter(self.requests.read_text().splitlines()), {"soundPART01": 2, "soundPART02": 2})

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
assert retry_job is not first and len(retry_job.jobs) == 1
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


if __name__ == "__main__":
    unittest.main()
