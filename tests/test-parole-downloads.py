#!/usr/bin/python3 -B
"""Qualification des créneaux d'import avec des originaux FLAC et un serveur local."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack, contextmanager
from hashlib import sha1
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic, sleep
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import unquote, urlsplit

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
import _kara_import_resources as resources
from _kara_import_queue import ImportQueue
from _kara_import_result import ImportIssue
from _parole_import_metrics import DownloadConcurrency
from _parole_index import Index
from _parole_sources import configure_limits, seed

# The child replaces only the transport and the fixture's allowed cadence.
# Admission, range verification, sorting and durable publication stay real.
WORKER = """
import contextlib
import http.client
import os
import runpy
import sys

import _kara_import_resources as resources
import _parole_archive as archive
import _parole_sources as sources

class LocalConnection(http.client.HTTPConnection):
    def connect(self):
        super().connect()
        self.tracked = self.sock
        if owner := resources.current_pause():
            owner.watch_socket(self.tracked)

@contextlib.contextmanager
def transport(host, timeout):
    assert host == "archive.org", host
    connection = LocalConnection("127.0.0.1", int(os.environ["KARA_FIXTURE_PORT"]), timeout=timeout)
    try:
        yield connection
    finally:
        connection.close()
        if hasattr(connection, "tracked") and (owner := resources.current_pause()):
            owner.forget_socket(connection.tracked)

sources.https_connection = transport
archive.INTERVAL = 0
# A tmpfs used by CI may be smaller than the conservative budget for
# twenty maximum-sized originals. The fixture stores kilobytes per slot.
resources.memory_download_limit = lambda: (20, "")
resources.storage_download_limit = lambda _: 20
kara = sys.argv.pop(1)
runpy.run_path(kara, run_name="__main__")
"""


class FixtureServer(ThreadingHTTPServer):
    request_queue_size = 64

    def __init__(self, originals):
        super().__init__(("127.0.0.1", 0), Handler)
        self.originals = originals
        self.lock = Lock()
        self.release = None
        self.delay = .95
        self.active = 0
        self.peak = 0
        self.offsets = Counter()
        self.received = 0
        self.prefixes = 0
        self.fail_first = False
        self.limited = Event()

    def reset(self):
        with self.lock:
            self.peak = self.active
            self.offsets.clear()
            self.received = 0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *_args):
        pass

    def body(self, status, data, mime, **headers):
        self.send_response(status)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        for name, value in headers.items():
            self.send_header(name, value)
        self.end_headers()

    def do_GET(self):
        _, route, identity, *file = unquote(urlsplit(self.path).path).split("/")
        server = self.server
        selected = server.originals if identity == "concurrence" else server.originals[:1]
        if route == "metadata":
            manifest = {"metadata": {"identifier": identity, "mediatype": "audio", "title": identity,
                                     "creator": "Atelier concurrence",
                                     "licenseurl": "https://creativecommons.org/publicdomain/zero/1.0/"},
                        "files": [entry for entry, _ in selected]}
            blob = json.dumps(manifest).encode()
            self.body(200, blob, "application/json")
            self.wfile.write(blob)
            return
        path = file[0]
        data = next(audio for info, audio in selected if info["name"] == path)
        offset = int(self.headers.get("Range", "bytes=0-").removeprefix("bytes=").removesuffix("-"))
        with server.lock:
            server.offsets[offset] += 1
            if server.fail_first and not server.limited.is_set():
                server.limited.set()
                self.body(429, b"", "text/plain", **{"Retry-After": "12"})
                return
            server.active += 1
            server.peak = max(server.peak, server.active)
        content = data[offset:]
        try:
            headers = {"Content-Range": f"bytes {offset}-{len(data) - 1}/{len(data)}"} if offset else {}
            self.body(206 if offset else 200, content, "audio/flac", **headers)
            mid = len(content) // 2
            self.wfile.write(content[:mid])
            self.wfile.flush()
            with server.lock:
                server.prefixes += 1
            if server.release is not None:
                server.release.wait(15)
            else:
                sleep(server.delay)
            self.wfile.write(content[mid:])
            with server.lock:
                server.received += len(content)
        except OSError:
            self.close_connection = True
        finally:
            with server.lock:
                server.active -= 1


class AdmissionTests(unittest.TestCase):
    def test_archive_can_take_a_fourth_slot_with_youtube_profile_occupied(self):
        with TemporaryDirectory() as temp, ExitStack() as leases:
            folder = Path(temp) / "slots"
            leases.enter_context(patch.object(resources, "RATE_DIRECTORY", Path(temp) / "rates"))
            leases.enter_context(patch.dict(resources.AUDIO_PROVIDERS, youtube=(3, 60)))
            leases.enter_context(patch.object(resources, "memory_download_limit", return_value=(20, "")))
            leases.enter_context(patch.object(resources, "storage_download_limit", return_value=20))
            for _ in range(3):
                provider = leases.enter_context(resources.resource_slot(folder / "youtube", 3))
                leases.enter_context(resources.audio_slot(folder, "youtube", provider_fd=provider))
            with resources.audio_slot(folder, "archive.org"):
                self.assertEqual(resources.slot_count(folder, 20), 4)
                self.assertEqual(resources.slot_count(folder / "youtube", 3), 3)

    def test_youtube_local_profile_is_bounded(self):
        self.assertEqual([resources.youtube_audio_slots(value) for value in ("", "0", "-1", "21", "٥", "a")],
                         [3] * 6)
        self.assertEqual([resources.youtube_audio_slots(str(value)) for value in (1, 5, 10, 15, 20)],
                         [1, 5, 10, 15, 20])

    def test_memory_pressure_caps_actual_admission(self):
        with TemporaryDirectory() as temp, patch.object(resources, "RATE_DIRECTORY", Path(temp) / "rates"), patch.object(
                resources, "memory_download_limit", return_value=(1, "Pression mémoire")):
            folder = Path(temp) / "slots"
            waiting, started = Event(), Event()

            def competing():
                with resources.audio_slot(folder, "archive.org", notice=waiting.set):
                    started.set()

            with ThreadPoolExecutor(1) as worker:
                with resources.audio_slot(folder, "youtube"):
                    future = worker.submit(competing)
                    self.assertTrue(waiting.wait(3))
                    self.assertEqual(resources.slot_count(folder, 20), 1)
                    self.assertFalse(started.is_set())
                future.result(timeout=3)
            self.assertTrue(started.is_set())

    def test_trials_require_real_gains_and_fall_back_under_pressure(self):
        gov = DownloadConcurrency(window=10)
        def sample(now, cap, active, amounts, resources=20):
            return gov.update(now, 20, cap, resources, active, 23, amounts)
        self.assertEqual(sample(0, 10, 10, (0, 0, 0, 0)), 10)
        self.assertEqual(sample(11, 10, 10, (1000, 10, 10, 0)), 15)
        self.assertEqual(sample(12, 15, 15, (1000, 10, 10, 0)), 15)
        self.assertEqual(sample(23, 15, 15, (2500, 25, 25, 0)), 20)
        self.assertEqual(sample(24, 20, 20, (2500, 25, 25, 0)), 20)
        self.assertEqual(sample(35, 20, 20, (3300, 42, 31, 0)), 15)
        self.assertEqual(sample(36, 15, 15, (3300, 42, 31, 0), resources=3), 3)
        self.assertEqual(sample(37, 3, 3, (3300, 42, 31, 1)), 2)

    def test_trial_recovers_without_waiting_for_album_publication(self):
        controller = DownloadConcurrency(window=10)
        def sample(now, limit, active, counters):
            return controller.update(now, 20, limit, 20, active, 20, counters)
        self.assertEqual(sample(0, 5, 5, (0, 0, 0, 1)), 3)
        self.assertEqual(sample(29, 3, 3, (500, 5, 0, 1)), 3)
        self.assertEqual(sample(31, 3, 3, (500, 5, 0, 1)), 3)
        self.assertEqual(sample(42, 3, 3, (1000, 10, 0, 1)), 5)
        # Starting another socket can take time, or the provider can cap
        # usage below the global trial. Never treat that as a gain.
        self.assertEqual(sample(43, 5, 3, (1100, 11, 0, 1)), 5)
        self.assertEqual(sample(50, 5, 3, (1400, 14, 0, 1)), 5)
        self.assertEqual(sample(62, 5, 3, (1700, 17, 0, 1)), 3)
        self.assertIn("sous-utilisé", controller.reason)

    def test_trial_retains_the_previous_values_of_live_gtk_counters(self):
        controller = DownloadConcurrency(window=10)
        counters = [0, 0, 0, 0]
        self.assertEqual(controller.update(0, 20, 3, 20, 3, 10, counters), 3)
        counters[0] += 4096
        counters[1] += 4
        self.assertEqual(controller.update(11, 20, 3, 20, 3, 8, counters), 5)

    def test_provider_cooldown_priority_and_ticket_names(self):
        with TemporaryDirectory() as temp, patch.object(resources, "RATE_DIRECTORY", Path(temp) / "rates"):
            folder = Path(temp) / "slots"
            queue = ImportQueue(Path(temp) / "state", Path(temp))
            auto = queue.enqueue_maintenance("maintenance-fixture", "Automatique", "https://archive.org/details/audio")
            self.assertEqual(queue.transfer_settings(auto), (10, 0))
            self.assertEqual(queue.enqueue([("Manuel", "https://archive.org/details/audio")]), auto)
            self.assertEqual(queue.transfer_settings(auto), (10, 1))
            queue.configure(downloads=1)
            self.assertEqual(ImportQueue(Path(temp)/"state", Path(temp)).download_limits(), (1, 1))
            resources.defer_service("archive.org", 12)
            with self.assertRaisesRegex(ImportIssue, "Temporisation"):
                with resources.audio_slot(folder, "archive.org"):
                    self.fail("Une source en temporisation a été admise")
            started = []
            with resources.download_scope(lambda: (1, True)):
                with ThreadPoolExecutor(2) as workers:
                    with resources.audio_slot(folder, "youtube"):
                        def competing(manual):
                            with resources.download_scope(lambda: (1, manual)):
                                with resources.audio_slot(folder, "api.jamendo.com"):
                                    started.append(manual)
                        first = workers.submit(competing, False)
                        second = workers.submit(competing, True)
                        deadline = monotonic() + 5
                        while len(list((folder / "waiting").iterdir())) < 2 and monotonic() < deadline:
                            sleep(.02)
                        self.assertEqual(len(list((folder / "waiting").iterdir())), 2)
                    first.result(timeout=5)
                    second.result(timeout=5)
            self.assertEqual(started, [True, False])
            # The service suffix is independent of tempfile's random chars.
            odd = folder / "waiting" / "0_0000-with_random_chars_archive.org"
            with resources.lease(odd):
                self.assertEqual(list(resources.pending_downloads(folder)), [(odd, "archive.org")])
            self.assertEqual(list(resources.pending_downloads(folder)), [])
            with patch.object(resources, "storage_download_limit", return_value=0), patch.object(
                    resources, "disk_usage", return_value=SimpleNamespace(free=0)):
                with self.assertRaisesRegex(ImportIssue, "Espace disque"):
                    with resources.audio_slot(folder, "youtube"):
                        self.fail("Transfert admis sans espace")


@unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "FFmpeg et FFprobe requis")
class FullEngineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.media = TemporaryDirectory(prefix="kara-matrix-")
        cls.addClassCleanup(cls.media.cleanup)
        with ThreadPoolExecutor(4) as encoders:
            cls.originals = tuple(encoders.map(cls.encode, range(1, 37)))

    @classmethod
    def encode(cls, number):
        name = f"piste-{number:02d}.flac"
        target = Path(cls.media.name) / name
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i",
                        f"anoisesrc=amplitude=0.08:duration=0.8:seed={number}", "-c:a", "flac",
                        "-metadata", "artist=Atelier concurrence", "-metadata", "album=Concurrence",
                        "-metadata", f"title=Piste {number:02d}", "-metadata", f"track={number}", str(target)],
                       check=True, timeout=15)
        blob = target.read_bytes()
        return ({"source": "original", "name": name, "size": len(blob),
                 "sha1": sha1(blob, usedforsecurity=False).hexdigest()}, blob)

    @contextmanager
    def server(self):
        with FixtureServer(self.originals) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield server
            finally:
                if server.release is not None:
                    server.release.set()
                server.shutdown()
                thread.join()

    def case(self, cap, editions):
        root = Path(self.enterContext(TemporaryDirectory(prefix=f"kara-cap-{cap}-")))
        music = root / "music"
        (music / "A_trier").mkdir(parents=True)
        index = Index(music, root / "state")
        seed(index)
        configure_limits(index, "Internet Archive", 1000, 0)
        queue = ImportQueue(root / "state", music)
        queue.configure(downloads=cap)
        queue.set_download_limit(cap)
        identity = queue.enqueue([(name, f"https://archive.org/details/{name}") for name in editions])
        return root, music, queue, identity

    @contextmanager
    def worker(self, case, server):
        root, music, queue, identity = case
        env = dict(os.environ, XDG_STATE_HOME=str(root/"state"), XDG_DATA_HOME=str(root/"data"),
                   XDG_CONFIG_HOME=str(root/"config"), XDG_CACHE_HOME=str(root/"cache"),
                   KARA_MUSIC_LIBRARY=str(music), PYTHONPATH=str(BIN), KARA_FIXTURE_PORT=str(server.server_port))
        with lease_job(queue, identity) as guard, (root / "worker.out").open("w+") as output:
            process = subprocess.Popen([sys.executable, "-B", "-c", WORKER, str(BIN/"kara"),
                                        "--suivi-json", "--import-file", identity, "--import-lease", str(guard)],
                                       env=env, pass_fds=(guard,), stdout=output, stderr=subprocess.STDOUT)
            try:
                yield process, output
            finally:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=10)

    def result(self, process, output, timeout=65, code=0):
        status = process.wait(timeout=timeout)
        output.seek(0)
        lines = output.read()
        self.assertEqual(status, code, lines[-3000:])
        return [json.loads(line) for line in lines.splitlines() if line.startswith("{")]

    def until(self, predicate, timeout=10):
        deadline = monotonic() + timeout
        while not predicate():
            self.assertLess(monotonic(), deadline, "La charge attendue n'a pas été atteinte.")
            sleep(.025)

    def test_benchmark_all_five_limits_through_publication(self):
        with self.server() as server:
            for cap in (3, 5, 10, 15, 20):
                with self.subTest(cap=cap):
                    server.reset()
                    case = self.case(cap, ["concurrence"])
                    start = monotonic()
                    with self.worker(case, server) as (process, output):
                        events = self.result(process, output)
                    elapsed = monotonic() - start
                    queue, identity = case[2:]
                    outcome, = queue.outcomes(identity).values()
                    self.assertTrue(outcome["audio_complete"], outcome["diagnostics"])
                    self.assertEqual((outcome["added"], len(set(outcome["tracks"]))), (36, 36))
                    self.assertEqual(server.peak, cap)
                    self.assertEqual(sum(event["size"] for event in events if event["event"] == "audio-bytes"),
                                     server.received)
                    receiving, started, peak = set(), set(), 0
                    for event in events:
                        if event["event"] == "audio-first-byte":
                            self.assertNotIn(event["track"], started)
                            started.add(event["track"])
                            receiving.add(event["track"])
                            peak = max(peak, len(receiving))
                        elif event["event"] == "audio-transfer-ended":
                            receiving.remove(event["track"])
                    self.assertEqual((len(started), receiving, peak), (36, set(), cap))
                    self.assertTrue(outcome["enrichment"]["import_lyrics"])
                    index = Index(case[1], case[0]/"state")
                    with index.db() as db:
                        self.assertEqual(db.execute("SELECT DISTINCT state FROM tasks WHERE kind='import_lyrics'").fetchone()[0],
                                         "queued")
                    print(f"{cap:2d} créneaux · pic {server.peak:2d} · {elapsed:.3f} s · "
                          f"{server.received / 2**20 / elapsed:.3f} Mio/s · {36 * 60 / elapsed:.1f} pistes/min",
                          flush=True)

    def test_pause_twenty_body_streams_and_resume_with_verified_ranges(self):
        with self.server() as server:
            server.release = Event()
            case = self.case(20, ["concurrence"])
            root, music, queue, identity = case
            with self.worker(case, server) as (process, output):
                self.until(lambda: server.peak == 20 and server.prefixes == 20)
                # These files fit inside Python's buffered writer. Give
                # readers time to receive the fragments, then let pause flush
                # them before checking the durable sizes below.
                sleep(.25)
                start = monotonic()
                queue.configure(paused=True)
                self.until(lambda: queue.pause_state() == "paused", timeout=5)
                events = self.result(process, output, timeout=5)
                self.assertEqual(events[-1]["event"], "suspended")
                print(f"Pause de 20 flux confirmée en {monotonic() - start:.3f} s.", flush=True)
            self.assertEqual(resources.slot_count(music / "A_trier/.kara-transferts", 20), 0)
            self.assertEqual(sum(part.stat().st_size > 0 for part in (music/"A_trier").rglob("*.part")), 20)
            reopened = ImportQueue(root/"state", music)
            reopened.configure(paused=False)
            server.release.set()
            with self.worker((root, music, reopened, identity), server) as (process, output):
                self.result(process, output)
            album, = reopened.outcomes(identity).values()
            self.assertTrue(album["audio_complete"])
            self.assertEqual(len(set(album["tracks"])), 36)
            self.assertEqual(sum(count for offset, count in server.offsets.items() if offset > 0), 20)
            self.assertEqual(len(list(music.rglob("*.flac"))), 36)

    def test_one_batch_231_albums_keeps_feeding_the_global_slots(self):
        with self.server() as server:
            server.release = Event()
            case = self.case(20, [f"edition-{number:03d}" for number in range(231)])
            with self.worker(case, server) as (process, output):
                # Hold the first bodies until enough catalogue workers have
                # supplied all twenty slots. Startup timing on a busy host
                # should not decide whether the single batch can feed them.
                self.until(lambda: server.peak == 20 and server.prefixes == 20)
                server.release.set()
                self.result(process, output, timeout=100)
            outcomes = case[2].outcomes(case[3])
            self.assertEqual(len(outcomes), 231)
            self.assertTrue(all(outcome["audio_complete"] for outcome in outcomes.values()))
            self.assertEqual(server.peak, 20)
            # The exact same fixture original is exposed under distinct
            # edition references. It must still only be published once.
            self.assertEqual(sum(outcome["added"] for outcome in outcomes.values()), 1)
            self.assertEqual(len(list(case[1].rglob("*.flac"))), 1)

    def test_http_429_stops_new_requests_for_that_provider(self):
        with self.server() as server:
            server.fail_first = True
            case = self.case(1, ["concurrence"])
            with self.worker(case, server) as (process, output):
                events = self.result(process, output, code=1)
            self.assertEqual(server.offsets, {0: 1})
            errors = [row for row in events if row["event"] == "audio-error"]
            self.assertTrue(errors)
            self.assertTrue(all(row["network"] is False for row in errors))
            self.assertEqual(case[2].job(case[3])["state"], "failed")


@contextmanager
def lease_job(queue, identity):
    with resources.lease(queue.directory / (identity + ".lock")) as guard:
        if not queue.claim(identity):
            raise AssertionError("Impossible de réserver le lot de qualification.")
        yield guard


if __name__ == "__main__":
    unittest.main()
