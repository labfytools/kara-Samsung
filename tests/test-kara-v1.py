"""Silent regression tests through Kara's real room, persistence and scoring APIs."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import ssl
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import wave

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox, atomic_json, events
from _kara_import_queue import ImportQueue, digest
from _kara_mobile_imports import MobileImports
from _kara_mobile_room import MobileRoom, ORGANIZER_ATTEMPTS, RoomError
from _kara_party import Party, Singer, Song, search_key
from _kara_remote_party import RemoteParty
from _kara_room_audio import RoomAudio, Route
from _kara_scoring import score_reference_line
from _kara_tv import Receiver, rotating_context


class BlackBoxTests(unittest.TestCase):
    def test_track_selection_and_per_turn_voice_consent_on_synthetic_pcm(self):
        with TemporaryDirectory() as directory:
            box = BlackBox(Path(directory) / "blackbox")
            box.start(consent=True, voice=True, tracks=["wifi"])
            audio = RoomAudio(box)
            with patch.object(audio, "_route_settings", return_value=("fixture", "device", {
                    "volume_percent": 45, "profile": "fixture", "mic_gain_percent": 100,
                    "chrome_echo": True, "chrome_noise": False})):
                reader = audio.open_turn("owner", "Alex", "a" * 16, "Essai", scoring=False, require_consent=True)
            try:
                audio.runner = object()
                route = audio.route
                now = time.monotonic()
                samples = bytes(1920)
                audio.record_playing(True)
                audio.record_received(route, samples, 48000, now, 0, now * 1000, None)
                audio.pcm(route, samples, 48000, now, 0, now * 1000)
                self.assertFalse(audio.consented())
                audio.ticket("owner", consent=True)
                audio.record_playing(False)
                audio.record_received(route, samples, 48000, now + .01, 1, (now + .01) * 1000, None)
                audio.record_playing(True)
                audio.record_received(route, samples, 48000, now + .02, 1, (now + .02) * 1000, None)
                audio.pcm(route, samples, 48000, now + .02, 1, (now + .02) * 1000)
                audio.return_pcm(route, samples, 1)
                self.assertEqual(audio.applied(route), 0)
                audio.enable_return()
                self.assertEqual(audio.applied(route), 45)
                audio.mute_return()
                self.assertEqual(audio.applied(route), 0)
            finally:
                audio.close_turn()
                reader.close()
                box.stop(wait=True)
            self.assertTrue(box.status(storage=True)["verified"])
            folder, = box.root.glob("session-*")
            capture = [event for event in events(folder / "events.jsonl") if event["event"] == "pcm"]
            self.assertEqual([(event["data"]["track"], event["data"]["sequence"]) for event in capture], [("wifi", 1)])

    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "blackbox"

    def cli(self, *args):
        result = subprocess.run([sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "bin/kara-blackbox"),
                                 "--root", str(self.root), *args], check=True, capture_output=True, text=True, timeout=12)
        return json.loads(result.stdout)

    def test_disabled_consent_score_replay_and_pcm_extraction(self):
        manager = BlackBox(self.root)
        manager.emit("party", "turn-start")
        self.assertFalse(self.root.exists())
        with self.assertRaises(ValueError):
            manager.start()
        manager.start(consent=True, voice=True)
        turn = "0123456789abcdef"
        context = {"turn": turn, "player": "Chanteuse", "song": "Fixture"}
        manager.emit("turbo", "window", **context, window=4, capture_begin=2., capture_end=2.02)
        decoded = [{"word": "bonsoir", "start": 0.2, "end": .47, "confidence": .95}]
        reference = {"words": [{"id": 0, "text": "Bonsoir", "normalized": "bonsoir", "start": .2}]}
        decisions = score_reference_line(reference, decoded)
        manager.emit("scoring", "line-decision", **context, observed=decoded, decisions=decisions,
                     reference=reference, source="voice", tolerance=1., token="must-not-be-written")
        block = struct.pack("<h", 512) * 320
        manager.pcm("whisper", 16000, 1, block, **context, sequence=1, capture_end=2.02)
        manager.emit("party", "turn-end", **context)
        manager.stop(wait=True)
        self.assertTrue(self.cli("validate")["valid"])
        self.assertTrue(self.cli("replay")["same"])
        session = self.root / self.cli("list")[0]
        self.assertNotIn("must-not-be-written", (session / "events.jsonl").read_text())
        window = next(item for item in events(session / "events.jsonl") if item["event"] == "window")
        destination = self.root.parent / "extrait.wav"
        self.assertEqual(self.cli("extract", "--id", str(window["id"]), "--output", str(destination))["coverage"], 1)
        with wave.open(str(destination), "rb") as source:
            self.assertEqual(source.readframes(320), block)
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        # A WAV with the right name and format must still be owned by the capture.
        recorded = next((session / "audio").glob("*.wav"))
        saved = recorded.with_suffix(".original")
        recorded.rename(saved)
        recorded.symlink_to(destination)
        rejected = self.root.parent / "refus.wav"
        result = subprocess.run([sys.executable, "-B",
                                 str(Path(__file__).resolve().parents[1] / "bin/kara-blackbox"),
                                 "--root", str(self.root), "extract", "--id", str(window["id"]),
                                 "--output", str(rejected)], capture_output=True, timeout=12)
        self.assertEqual(result.returncode, 1)
        self.assertFalse(rejected.exists())
        saved.replace(recorded)
        manager.start(consent=True)
        manager.pcm("whisper", 16000, 1, block, **context)
        manager.stop(wait=True)
        self.assertFalse(any(path.name == "audio" and path.parent.name != session.name
                             for path in self.root.glob("session-*/audio")))

    def test_restart_preserves_complete_rows_and_marks_interruption(self):
        self.root.mkdir(mode=0o700)
        name = "session-20260101T000000-0123456789ab"
        folder = self.root / name
        folder.mkdir(mode=0o700)
        atomic_json(folder / "manifest.json", {"format": 1, "status": "recording", "session": name})
        full = {"id": 1, "session": name, "component": "party", "event": "marker", "monotonic_ns": 12}
        (folder / "events.jsonl").write_bytes(json.dumps(full).encode() + b"\n{\"cut\":")
        manager = BlackBox(self.root)
        manager.recover()
        self.assertEqual(list(events(folder / "events.jsonl")), [full])
        self.assertFalse(manager.status()["latest"]["complete"])
        self.assertEqual(self.cli("validate")["status"], "incomplete")

    def test_disk_reserve_and_aborted_budget(self):
        import shutil
        with self.assertRaises(OSError):
            BlackBox(self.root, reserve=shutil.disk_usage(self.root.parent).free + 1).start(consent=True)
        manager = BlackBox(self.root, max_bytes=200, reserve=0)
        manager.start(consent=True)
        manager.emit("system", "large", content="x" * 1000)
        manager.stop(wait=True)
        self.assertFalse(manager.status()["latest"]["complete"])
        self.assertEqual(self.cli("validate")["status"], "incomplete")

    def test_nested_secrets_and_serialized_credentials_are_absent_from_session(self):
        manager = BlackBox(self.root)
        secret = "redaction-fixture-unique-123456"
        entry = {"X-API-Key": secret, "Set-Cookie": secret,
                 "nested": [{"dns_ovh_application_secret": secret},
                            {"detail": f"https://localhost/preview?token={secret}"},
                            {"detail": '{"refresh_token":"' + secret + '"}'},
                            {"detail": f"Authorization: Bearer {secret}"},
                            {"detail": f"-----BEGIN PRIVATE KEY-----\n{secret}"}]}
        manager.start(consent=True, configuration=entry)
        manager.emit("mobile", "error", player=f"https://localhost/preview?token={secret}", evidence=entry)
        manager.stop(wait=True)
        folder = next(self.root.glob("session-*"))
        for name in ("events.jsonl", "manifest.json", "summary.json", "incidents.jsonl", "REPORT.md"):
            self.assertNotIn(secret, (folder / name).read_text())

    def test_float_acoustic_index_and_extraction_preserve_waveform(self):
        from _kara_blackbox import FLOAT_WAV
        manager = BlackBox(self.root)
        manager.start(consent=True, voice=True)
        session = manager.voice_session()
        turn = "0123456789abcdef"
        clock = dict(driver=14, frame=1000, monotonic_ns=5000000000, xrun=0,
                     quantum=2, present=6, spa_headers=6, route=2, epoch=100)
        block = struct.pack("<4f", 0.1, -0.5, 1.25, -1.25)
        manager.pcm("monitor", 48000, 2, block, turn=turn, player="Fixture", song="Synthétique",
                    clock=clock, recording_session=session)
        manager.stop(wait=True)
        self.assertTrue(self.cli("validate")["valid"])
        folder = self.root / session
        item = next(row for row in events(folder / "events.jsonl") if row["event"] == "pcm")
        self.assertEqual(item["data"]["clock"], clock)
        destination = self.root.parent / "monitor.wav"
        extract = self.cli("extract-pcm", "--id", str(item["id"]), "--output", str(destination))
        self.assertEqual(extract["track"], "monitor")
        self.assertEqual(destination.read_bytes()[FLOAT_WAV.size:], block)
        capture = json.loads((folder / "manifest.json").read_text())["capture"]["acoustic"]
        self.assertEqual(capture["turns"][turn]["witness"]["coverage"], "unavailable")


class FakeController:
    """Replace only adapters that would start the model or touch real hardware."""
    def __init__(self, root):
        self.blackbox = BlackBox(root / "blackbox")
        self.party = Party(root / "players")
        self.remote_party = RemoteParty(self)
        self.room_audio = RoomAudio(self.blackbox)
        self.mobile_imports = SimpleNamespace(pending=lambda: False)
        self.album_import = SimpleNamespace(running=False, imports_snapshot=([], {}))
        self.closed = self.validating = self.party_ready = False
        self.align_fraction = 0.
        self.align_status = ""
        self.turn_phase = ""
        self.turn_music_restore = self.music_volume = None
        self.playback_state = "Stopped"
        self.failed_alignments = {}
        self.players_window = None
        self.mobile_players = {}
        self.calls = []
        self.batch = []
        self.available = set()
        self.game_score = SimpleNamespace(breakdown=None, requested=False)

    def song_available(self, song, *, unscored=False):
        return song.path in self.available

    def countdown(self):
        return 0

    def start_blackbox(self, consent, voice=False, tracks=None):
        self.blackbox.start(consent=consent, voice=voice, tracks=tracks)

    def stop_blackbox(self, reason="manual"):
        self.blackbox.stop(reason)

    def ensure_players_window(self):
        pass

    def can_skip_failed(self):
        return False

    def unscored_choices(self):
        return set()

    def mobile_event(self, action, snapshot):
        token = snapshot["token"]
        if action == "join":
            player = Singer(snapshot["name"], mobile_id=token)
            self.mobile_players[token] = player
            self.party.players.append(player)
        if action in ("choose", "clear"):
            self.mobile_players[token].song = self.receiver.room.song_for(snapshot["song"])
        self.remote_party.changed_selection()

    def begin_preparation(self, songs, *, for_party, force=False):
        self.calls.append("prepare")
        self.batch = songs
        self.validating = True
        return True

    def start_turn(self, player, *, independent=False):
        self.calls.append("start")
        self.party.active = player
        self.turn_phase = "awaiting-mic" if independent else "playing"
        self.playback_state = "Stopped" if independent else "Playing"
        self.blackbox.emit("party", "turn-start", player=player.name, song=player.song.title)

    def finish_turn(self, **kwargs):
        player = self.party.active
        player.result = None if player.training else {"summary": {"score": 87, "coverage": 92}}
        self.remote_party.turn_completed(player, "Fin du passage", played=True)
        self.party.active = None
        self.turn_phase = ""

    def player_command(self, action):
        self.calls.append(action)
        self.playback_state = "Paused" if action == "Pause" else "Playing"


class RoomTests(unittest.TestCase):
    def test_mobile_confirmed_edition_enters_the_shared_queue_only_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            control = FakeController(path)
            queue = ImportQueue(path / "state", path)
            imports = MobileImports(control)
            imports.bind_queue(queue)
            queue.configure(paused=True)
            imports.editions["edition"] = {"id": "MPREfixture", "title": "Album", "tracks": [{}]}
            request = {"handle": "edition", "add": False, "rights": True, "direct": True}
            first = imports.request("first", "Alex", request)
            second = imports.request("second", "Jo", request)
            self.assertEqual(first, second)
            self.assertEqual(first["state"], "queued")
            self.assertEqual(queue.count(), {"queued": 1})
            receipt, = ImportQueue(path / "state", path).requests()
            self.assertEqual(len(receipt["requests"]), 2)

    def test_preparation_runs_once_for_shared_choice_and_first_turn_can_open(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            control = FakeController(path)
            control.receiver = receiver = Receiver("127.0.0.1", b"", on_mobile=control.mobile_event)
            self.addCleanup(receiver.close)
            room = receiver.room
            songs = [Song(path / title, title, "Artiste", "Album") for title in ("Premier", "Suite")]
            for song in songs:
                song.path.touch()
            room.update_catalogue([(song, search_key(song.title)) for song in songs])
            first, second, third = [room.join(name)["token"] for name in ("Alex", "Jo", "Sam")]
            for token, song in ((first, songs[0]), (second, songs[0]), (third, songs[1])):
                match = room.search(token, song.title)["results"][0]["id"]
                room.choose(token, match, confirm=True)
            self.idle_until(lambda: len(control.party.players) == 3)
            remote = control.remote_party
            remote.tick(time.monotonic())
            self.assertEqual(control.batch, songs[:1])
            remote.tick(time.monotonic())
            self.assertEqual(control.calls, ["prepare"])
            control.available.add(songs[0].path)
            control.validating = False
            remote.background_path = None
            self.assertTrue(remote.can_open())
            remote.command("open", "Alex", {"request": "0" * 24, "revision": remote.revision})
            remote.tick(time.monotonic())
            self.assertEqual(control.party.active.name, "Alex")
            self.assertEqual(control.turn_phase, "awaiting-mic")
            self.assertNotIn(songs[1].path, control.available)
            with self.assertRaises(RoomError):
                remote.command("player-start", "Jo", {"request": "1" * 24, "turn": remote.turn_id})
            control.request_turn_start = lambda: control.calls.append("countdown")
            data = {"request": "2" * 24, "turn": remote.turn_id}
            remote.command("player-start", "Alex", data)
            remote.command("player-start", "Alex", data)
            self.assertEqual(control.calls.count("countdown"), 1)

    def test_prepare_before_confirmation_and_release_unselected_song(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            control = FakeController(path)
            control.receiver = receiver = Receiver("127.0.0.1", b"", on_mobile=control.mobile_event)
            self.addCleanup(receiver.close)
            room = receiver.room
            first = Song(path / "A", "Premier", "Artiste", "Album")
            second = Song(path / "B", "Second", "Artiste", "Album")
            first.path.touch(); second.path.touch()
            room.update_catalogue([(song, search_key(song.title)) for song in (first, second)])
            token = room.join("Alex")["token"]
            room.choose(token, room.search(token, first.title)["results"][0]["id"], confirm=False)
            self.idle_until(lambda: len(control.party.players) == 1)
            remote = control.remote_party
            self.assertEqual(remote.selected_turns(), [])
            remote.tick(time.monotonic())
            self.assertEqual(control.batch, [first])
            control.interrupt_preload = lambda: control.calls.append("cancel")
            room.choose(token, room.search(token, second.title)["results"][0]["id"], confirm=False, replace=True)
            remote.tick(time.monotonic())
            self.assertIn("cancel", control.calls)
            control.validating = False
            remote.background_path = None
            released = []
            def release(paths):
                released.extend(paths)
                return set(paths)
            control.release_prepared_stems = release
            remote.tick(time.monotonic())
            self.assertEqual(released, [first.path])
            self.assertEqual(control.batch, [second])

    def test_tv_display_reopens_once_after_start_or_manual_close(self):
        receiver = Receiver("127.0.0.1", b"TV")
        try:
            receiver.resume_tv_display()
            receiver.resume_tv_display()
            self.assertEqual(json.loads(receiver.tv_request())["tv_reload"], 1)
            receiver.request_tv_close()
            receiver.resume_tv_display()
            receiver.resume_tv_display()
            state = json.loads(receiver.tv_request())
            self.assertEqual((state["tv_close"], state["tv_reload"]), (1, 2))
        finally:
            receiver.close()

    def test_four_digit_code_expires_and_has_a_room_wide_attempt_budget(self):
        room = MobileRoom(None)
        first = room.join("Organisateur")["token"]
        second = room.join("Invité")["token"]
        with patch("_kara_mobile_room.randbelow", side_effect=(42, 719, 2000)):
            self.assertEqual(room.issue_admin_code(), "0042")
            for index in range(ORGANIZER_ATTEMPTS):
                with self.assertRaises(RoomError) as denied:
                    room.authorize((first, second)[index % 2], "9999", enroll=True)
                self.assertEqual(denied.exception.status, 403)
            with self.assertRaises(RoomError) as blocked:
                room.authorize(first, "0042", enroll=True)
            self.assertEqual(blocked.exception.status, 429)
            self.assertEqual(room.issue_admin_code(), "0719")
            with self.assertRaises(RoomError):
                room.authorize(second, "0042", enroll=True)
            self.assertEqual(room.authorize(first, "0719", enroll=True), "Organisateur")
            now = time.monotonic()
            self.assertEqual(room.issue_admin_code(), "2000")
        with patch("_kara_mobile_room.time.monotonic", return_value=now + 301):
            with self.assertRaises(RoomError) as expired:
                room.authorize(second, "2000", enroll=True)
            self.assertEqual(expired.exception.status, 403)
            self.assertEqual(room.authorize(first), "Organisateur")

    def test_certificate_generation_changes_on_new_handshake(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            certs = []
            for generation in ("first", "second"):
                folder = base / generation
                folder.mkdir()
                cert = folder / "fullchain.pem"
                subprocess.run(("openssl", "req", "-x509", "-newkey", "ed25519", "-nodes", "-days", "1",
                                "-keyout", str(folder / "privkey.pem"), "-out", str(cert),
                                "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost"),
                               check=True, capture_output=True)
                certs.append(cert.read_text())
            current = base / "current"
            current.symlink_to("first")
            context = rotating_context(current / "fullchain.pem", current / "privkey.pem")
            client = ssl.create_default_context(cadata="".join(certs))
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            receiver = Receiver("127.0.0.1", b"TV", https=(f"https://localhost:{port}", port, context))
            def peer_certificate():
                with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
                    with client.wrap_socket(connection, server_hostname="localhost") as secured:
                        return ssl.DER_cert_to_PEM_cert(secured.getpeercert(binary_form=True))
            try:
                self.assertEqual(peer_certificate(), certs[0])
                next_link = base / "next"
                next_link.symlink_to("second")
                next_link.replace(current)
                self.assertEqual(peer_certificate(), certs[1])
            finally:
                receiver.close()

    @staticmethod
    def idle_until(predicate, timeout=5):
        context = GLib.MainContext.default()
        until = time.monotonic() + timeout
        while not predicate():
            while context.pending():
                context.iteration(False)
            if time.monotonic() > until:
                raise AssertionError("La boucle Parole n’a pas reçu la commande")
            time.sleep(.002)
        while context.pending():
            context.iteration(False)

    def test_https_authenticated_party_repeat_and_reconnect(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            subprocess.run(("openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-keyout", str(path / "key"), "-out", str(path / "cert"), "-subj", "/CN=127.0.0.1",
                            "-addext", "subjectAltName=IP:127.0.0.1"), check=True, capture_output=True)
            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]
            origin = f"https://127.0.0.1:{port}"
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            tls.load_cert_chain(path / "cert", path / "key")
            client = ssl.create_default_context(cafile=str(path / "cert"))
            control = FakeController(path)
            control.mobile_imports = MobileImports(control)
            queue = ImportQueue(path / "state", path)
            control.mobile_imports.bind_queue(queue)

            def configure_imports(**settings):
                queue.configure(**settings)
                return True

            control.album_import.configure = configure_imports
            receiver = Receiver("127.0.0.1", b"__MOBILE_JOIN_URL__", https=(origin, port, tls),
                                on_mobile=control.mobile_event, imports=control.mobile_imports,
                                diagnostics=control.blackbox, commands=control.remote_party.dispatch,
                                display_key="c" * 64)
            control.receiver = receiver
            try:
                base = origin + receiver.path
                stable = origin + "/tv/etat"
                with self.assertRaises(HTTPError) as denied:
                    urlopen(stable, context=client, timeout=4)
                self.assertEqual(denied.exception.status, 403)
                with urlopen(Request(stable, headers={"Authorization": "Bearer " + "c" * 64}),
                             context=client, timeout=4) as response:
                    display = json.load(response)
                    self.assertEqual(response.headers["Access-Control-Allow-Origin"], "*")
                self.assertEqual(display["join_url"], base + "/jouer")
                self.assertRegex(display["organizer_pin"], r"^[0-9]{4}$")
                self.assertNotIn("organizer_pin", json.loads(receiver.tv_request()))

                def req(action, data=None, token=None, expected=200, custom_origin=origin, https=True):
                    headers = {"Origin": custom_origin}
                    if data is not None:
                        headers["Content-Type"] = "application/json"
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    url = (base if https else receiver.url) + action
                    call = Request(url, data=json.dumps(data).encode() if data is not None else None, headers=headers)
                    try:
                        with urlopen(call, context=client, timeout=6) as response:
                            self.assertEqual(response.status, expected)
                            content = response.read()
                            return json.loads(content) if action.startswith("/api/") else content
                    except HTTPError as failure:
                        with failure:
                            self.assertEqual(failure.code, expected)
                            return json.loads(failure.read())

                def command(pool, token, action, key, expected=200, **kwargs):
                    revision = control.remote_party.revision
                    response = pool.submit(req, "/api/admin/" + action, {"request": (key * 24)[:24], "revision": revision,
                                                                        **kwargs}, token, expected)
                    self.idle_until(response.done)
                    return response.result()

                a = req("/api/inscription", {"name": "Camille"})["token"]
                b = req("/api/inscription", {"name": "Morgan"})["token"]
                req("/api/inscription", {"token": a}, https=False, expected=403)
                code = receiver.room.issue_admin_code()
                req("/api/organisateur", {"code": code}, a, expected=403, custom_origin="https://other.invalid")
                req("/api/organisateur", {"code": code}, a)
                self.idle_until(lambda: len(control.party.players) == 2)
                self.assertEqual(req("/api/inscription", {"token": b})["token"], b)
                self.assertNotIn(a.encode(), req(""))
                self.assertIn(receiver.join_url.encode(), req(""))
                receiver.tv_request(client=True)
                for title in ("Premier", "Second"):
                    (path / title).touch()
                receiver.room.update_catalogue([(Song(path / name, name, "Artiste", "Album", 22.), search_key(name))
                                                for name in ("Premier", "Second")])
                songs = req("/api/recherche?mode=title", token=a)["results"]
                req("/api/choix", {"song_id": songs[0]["id"]}, a)
                req("/api/choix", {"song_id": songs[1]["id"]}, a)
                req("/api/choix", {"song_id": songs[1]["id"]}, b)
                req("/api/ordre", {"songs": [songs[1]["id"], songs[0]["id"]]}, a)
                req("/api/pret", {"ready": True}, a)
                req("/api/pret", {"ready": True}, b)
                with ThreadPoolExecutor(2) as pool:
                    own_mix = {"request": "6" * 24, "player": "Camille",
                               "music": 42, "voice": 55, "training": True, "original": 50}
                    with patch.object(control.room_audio, "_route_settings", return_value=("sink", "device", {})):
                        response = pool.submit(req, "/api/reglages-joueur", own_mix, b)
                        self.idle_until(response.done)
                        self.assertTrue(response.result()["ok"])
                    self.assertEqual(req("/api/etat", token=b)["me"]["mix"],
                                     {"music": 42, "voice": 55, "training": True, "original": 50})
                    self.assertEqual(req("/api/etat", token=a)["me"]["mix"]["music"], None)
                    command(pool, b, "audio-player-mix", "7", expected=403, player="Camille", music=100)
                    command(pool, a, "audio-player-mix", "8", player="Camille", music=30)
                    self.assertEqual(req("/api/etat", token=a)["me"]["mix"]["music"], 30)
                    # External catalogue responses are controlled here. The
                    # HTTPS identity, edition handle and approval queue are real.
                    artist = {"id": "UC" + 22 * "a", "name": "Artiste", "followers": ""}
                    release = {"id": "MPREfixture", "title": "Album", "year": "2026"}
                    releases = [release, {"id": "MPREautre", "title": "Autre album", "year": "2024"}]
                    edition = {**release, "artists": ["Artiste"], "tracks": [
                        {"title": "Morceau inédit", "track": 1, "artists": ["Interprète"]}], "cover": None}
                    with patch.object(control.mobile_imports, "catalogue",
                                      side_effect=([artist], {"name": "Artiste", "albums": releases}, edition)) as lookup:
                        req("/api/artistes-externes", {"query": "Ar"}, expected=401)
                        self.assertEqual(req("/api/artistes-externes", {"query": "Ar"}, b), [artist])
                        group = req("/api/albums-artiste", {"artist": artist["id"]}, b)
                        self.assertEqual(group["albums"], releases)
                        public = req("/api/edition-externe", {"album": release["id"]}, b)
                        self.assertEqual([call.args[0] for call in lookup.call_args_list], ["artistes", "albums", "edition"])
                    request = {"handle": public["handle"], "track": 0, "add": True, "rights": True}
                    req("/api/importer-album", {**request, "rights": False}, b, expected=400)
                    req("/api/importer-album", {**request, "track": 1}, b, expected=400)
                    job = req("/api/importer-album", request, b)["job"]
                    self.assertEqual(req("/api/importer-album", request, b)["job"], job)
                    self.assertEqual(control.mobile_imports.snapshot(token=b)[0]["state"], "approval")
                    selected = control.mobile_imports.jobs[job]["requests"][digest(b)][1]
                    self.assertEqual((selected["requested_title"], selected["requested_artist"]),
                                     ("Morceau inédit", "Interprète"))
                    command(pool, b, "import-cancel", "4", expected=403, job=job)
                    command(pool, b, "import-pause", "a1", expected=403)
                    command(pool, a, "import-pause", "a2")
                    command(pool, a, "import-concurrency", "13", batches=6)
                    self.assertEqual(queue.settings(), (True, 6))
                    command(pool, a, "import-resume", "14")
                    self.assertEqual(queue.settings(), (False, 6))
                    bulk = {"handle": group["handle"], "all": True, "rights": True}
                    req("/api/importer-album", {**bulk, "rights": False}, b, expected=400)
                    queued_bulk = req("/api/importer-album", bulk, b)["job"]
                    command(pool, a, "import-cancel", "5", job=job)
                    with patch.object(receiver, "commands") as dispatch:
                        participant = req("/api/importer-album", bulk, b)
                        batch = participant["job"]
                        self.assertEqual(batch, queued_bulk)
                        self.assertNotIn("direct", participant)
                        dispatch.assert_not_called()
                        organizer = req("/api/importer-album", bulk, a)
                        self.assertEqual(organizer, {"job": batch, "direct": True})
                        dispatch.assert_called_once_with("import-approve", "Camille", {"job": batch, "request": batch})
                    item = control.mobile_imports.snapshot(token=b)[0]
                    self.assertEqual((item["state"], item["albums"], item["tracks"]), ("approval", 2, None))
                    command(pool, a, "import-cancel", "0", job=batch)
                    command(pool, b, "blackbox-start", "a", expected=403, consent=True)
                    command(pool, a, "blackbox-start", "b", consent=True)
                    command(pool, a, "prepare", "c")
                    self.assertEqual([song.title for song in control.batch], ["Second", "Second", "Premier"])
                    self.assertEqual(control.remote_party.training_paths(), ({path / "Second"}, set()))
                    control.validating = False
                    control.party_ready = True
                    control.remote_party.prepared()
                    command(pool, a, "start", "d")
                    command(pool, a, "start", "d")
                    self.assertEqual(control.calls, ["prepare", "start"])
                    command(pool, a, "audio-player-mix", "9", expected=409, player="Morgan", training=False)
                    command(pool, a, "pause", "e")
                    command(pool, a, "resume", "f")
                    req("/api/retirer-choix", {}, b, expected=409)
                    command(pool, a, "next", "1", confirmed=True)
                    control.remote_party.tick(time.monotonic() + 4)
                    self.assertEqual(control.party.active.name, "Morgan")
                    command(pool, a, "end", "2", confirmed=True)
                    control.blackbox.stop(wait=True)
                    self.assertEqual(control.blackbox.status()["latest"]["reason"], "party-end")
                    self.assertTrue(req("/api/etat", token=a)["me"]["organizer"])
                    self.assertEqual(req("/api/etat", token=a)["party"]["results"][0]["summary"]["score"], 87)
                    self.assertIsNone(req("/api/etat", token=b)["party"]["results"][0]["summary"])
                    command(pool, a, "new", "3")
                    self.assertFalse(req("/api/etat", token=b)["me"]["ready"])
                    self.assertEqual(len(req("/api/etat", token=b)["me"]["songs"]), 1)
            finally:
                receiver.close()

    def test_discography_uses_one_queue_job_and_keeps_completed_albums_across_retry(self):
        root = Path(self.enterContext(TemporaryDirectory()))
        queue = ImportQueue(root / "state", root / "library")
        refresh = Mock()
        panel = SimpleNamespace(imports_snapshot=([], {}), launch_next=Mock(),
                                retry_job=lambda identity: queue.transition(identity, "partial", "queued", "Relance"))
        control = SimpleNamespace(blackbox=Mock(), remote_party=SimpleNamespace(locked=False),
                                  album_import=panel, refresh_after_import=refresh)
        imports = MobileImports(control)
        imports.bind_queue(queue)
        releases = {"name": "Artiste", "albums": [{"id": "MPREa", "title": "A"}, {"id": "MPREb", "title": "B"}]}
        with patch.object(imports, "catalogue", return_value=releases):
            group = imports.albums("UC" + "a" * 22)
        job = imports.request("joueur", "Camille", {"handle": group["handle"], "all": True, "rights": True})["job"]
        self.assertEqual(queue.count(), {})
        imports.action(job, "approve")
        self.assertEqual(queue.count(), {"queued": 1})
        identity = imports.jobs[job]["task_id"]
        targets = queue.job(identity)["targets"]
        self.assertEqual([title for title, _ in targets], ["A", "B"])
        self.assertTrue(queue.claim(identity))
        first = {"url": targets[0][1], "status": "success", "audio_complete": True, "tracks": ["/tmp/kara-a"]}
        queue.album(identity, first)
        queue.album(identity, {"url": targets[1][1], "status": "failure"})
        queue.finish(identity, "failure", "Partiel")
        task = SimpleNamespace(identity=identity, fraction=1., final_event="failure", outcomes=queue.outcomes(identity))
        imports.update(task, "settled", {})
        self.assertEqual(imports.snapshot(token="joueur")[0]["state"], "partial")
        imports.action(job, "retry")
        self.assertEqual(queue.count(), {"queued": 1})
        self.assertEqual(queue.outcomes(identity)[targets[0][1]], first)
        self.assertTrue(queue.claim(identity))
        queue.album(identity, {"url": targets[1][1], "status": "success",
                               "audio_complete": True, "tracks": ["/tmp/kara-b"]})
        queue.finish(identity, "success", "Terminé")
        task.final_event, task.outcomes = "success", queue.outcomes(identity)
        imports.update(task, "settled", {})
        imports.integrated(refresh.call_args.args[1], None, {Path("/tmp/kara-a"), Path("/tmp/kara-b")})
        self.assertEqual(imports.snapshot(token="joueur"), [])
        self.assertEqual(imports.snapshot(token="joueur", history=True)[0]["state"], "done")

    def test_audio_uses_bound_turn_and_independent_pcm_without_opening_pipewire(self):
        with TemporaryDirectory() as directory:
            room = RoomAudio(BlackBox(Path(directory) / "blackbox"))
            sender, reader = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addCleanup(sender.close)
            self.addCleanup(reader.close)
            room.route = route = Route("alice-session", "Alice", "0123456789abcdef", "Titre", "test",
                                       "test", {"volume_percent": 60}, 60, sender)
            data = struct.pack("<h", 1000) * 960
            room.pcm(route, data, 48000, 7., 0, 1200.)
            first = reader.recv(4096)
            room.volume(route, "20")
            room.pcm(route, data, 48000, 7.020, 1, 1220.)
            second = reader.recv(4096)
            self.assertEqual(first[8:], second[8:])
            self.assertAlmostEqual(struct.unpack_from("<d", second)[0], 7.020)
            self.assertEqual(room.applied(route), 20)
            room.close_turn()
            self.assertFalse(room.receiving())

    def test_training_keeps_wifi_capture_without_feeding_a_score_worker(self):
        tracks = []
        diagnostics = SimpleNamespace(emit=lambda *_args, **_data: None,
                                      pcm=lambda track, *_args, **_data: tracks.append(track))
        room = RoomAudio(diagnostics)
        settings = {"volume_percent": 60, "profile": "balanced"}
        with patch.object(room, "_route_settings", return_value=("sink", "device", settings)):
            reader = room.open_turn("owner", "Joueur", "turn", "Titre", scoring=False)
        self.addCleanup(reader.close)
        self.addCleanup(room.close_turn)
        data = struct.pack("<h", 1000) * 960
        room.record_playing(True)
        room.route.recording_consent = True
        for sequence in range(256):
            room.record_received(room.route, data, 48000, 10 + sequence / 50, sequence, 1000 + 20 * sequence, None)
            room.pcm(room.route, data, 48000, 10 + sequence / 50, sequence, 1000 + 20 * sequence)
        self.assertTrue(room.receiving())
        self.assertEqual(room.route.failed_blocks, 0)
        self.assertEqual(tracks, ["wifi"] * 256)
        reader.setblocking(False)
        with self.assertRaises(BlockingIOError):
            reader.recv(4096)


if __name__ == "__main__":
    unittest.main()
