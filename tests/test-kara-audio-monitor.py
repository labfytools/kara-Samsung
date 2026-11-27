#!/usr/bin/env python3
"""Drive the live WebSocket and writer with controlled source and sink delays."""
import asyncio
import json
import os
from pathlib import Path
import struct
import sys
import time
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from unittest.mock import Mock

import numpy as np
from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer
from scipy.signal import resample_poly

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _kara_micro_test as micro
import _kara_monitor_profiles as preferences
from _kara_room_audio import RoomAudio
from _kara_mobile_room import RoomError


class ProfileStoreTests(unittest.TestCase):
    def test_trials_have_bounded_ceilings_and_do_not_update_storage(self):
        self.assertEqual(len(preferences.MIX_TRIALS), 8)
        for name, trial in preferences.MIX_TRIALS.items():
            with self.subTest(name=name), patch.dict(os.environ, {"KARA_MIX_TRIAL": name}):
                room = RoomAudio(Mock())
                self.assertEqual(room.output, trial.output)
                self.assertEqual(room.ceiling, 50)
                with self.assertRaises(RoomError):
                    room.administrate("audio-ceiling", trial.ceiling + 1)
                room.administrate("audio-ceiling", trial.ceiling)
                self.assertEqual(room.ceiling, trial.ceiling)
                with self.assertRaises(RoomError):
                    room.administrate("audio-output", "analog" if trial.output == "default" else "default")

    def test_edenwood_trial_selects_its_own_sbc_sink(self):
        with patch.dict(os.environ, {"KARA_MIX_TRIAL": "edenwood-sbc-natif"}):
            room = RoomAudio(Mock())
        room.loop = room.runner = object()
        edenwood = {"index": 26, "name": "bluez_output.12_34_56_78_9A_BC.1", "description": "EDENWOOD PARTY 250 V2",
                    "properties": {"api.bluez5.codec": "sbc"}}
        with patch("_kara_room_audio.pulse_sinks", return_value=[edenwood]):
            sink, _, settings = room._route_settings()
            self.assertEqual(sink, edenwood["name"])
            self.assertFalse(settings["experimental"])
            self.assertEqual(settings["turn_return_drive"], 3.)
        with patch("_kara_room_audio.pulse_sinks", return_value=[{"name": "analog"}]):
            with self.assertRaisesRegex(RoomError, "EDENWOOD en SBC"):
                room._route_settings()

    def test_isolated_port_and_codec_identities(self):
        bt = "bluez_output.12_34_56_78_9A_BC.1"
        speaker = preferences.DEFAULTS | {"profile": "low-latency", "volume_percent": 60,
                                          "experimental": False}
        trial = preferences.DEFAULTS | {"pulse_buffer": "short"}
        with TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-profiles.json"
            preferences.update_profile(path, micro.ANALOG_TARGET, "analog-output-speaker", speaker)
            preferences.update_profile(path, bt, None, trial)
            saved = preferences.read_profiles(path)
            bt_key = preferences.device_key(bt.removesuffix(".1") + ".2")
            self.assertEqual(saved[bt_key], trial)
            self.assertEqual(saved[preferences.device_key(micro.ANALOG_TARGET, "analog-output-speaker")], speaker)
            self.assertNotIn(preferences.device_key(micro.ANALOG_TARGET, "analog-output-headphones"), saved)
            self.assertIsNone(preferences.device_key(micro.ANALOG_TARGET))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            preferences.update_profile(path, bt, None, None)
            self.assertEqual(list(preferences.read_profiles(path).values()), [speaker])

    def test_invalid_writes_preserve_previous_settings(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-profiles.json"
            path.write_bytes(b'{"version": 99, "devices": {}}')
            with self.assertRaises(ValueError):
                preferences.update_profile(path, micro.ANALOG_TARGET, "analog-output-speaker", preferences.DEFAULTS)
            self.assertEqual(path.read_bytes(), b'{"version": 99, "devices": {}}')
            with self.assertRaises(ValueError):
                preferences.validate_settings(preferences.DEFAULTS | {"profile": "stable", "pulse_buffer": "short"})


def packet(seq, value, rate=48000, ms=10, timestamp=None):
    return (struct.pack("<4sId", b"KTS1", seq, seq * ms if timestamp is None else timestamp)
            + struct.pack("<h", value) * (rate * ms // 1000))


class Player:
    def __init__(self, reports):
        self.stdin = self
        self.stderr = asyncio.StreamReader() if reports else None
        self.returncode = None
        self.closed = False
        self.reaped = False
        self.exit = asyncio.Event()
        self.pending_write = asyncio.Queue()
        self.gate = asyncio.Event()

    def write(self, data):
        self.pending_write.put_nowait(data)

    async def drain(self):
        await self.gate.wait()

    def close(self):
        self.closed = True

    def terminate(self):
        self.returncode = -15
        if self.stderr is not None:
            self.stderr.feed_eof()
        self.exit.set()

    async def wait(self):
        await self.exit.wait()
        self.reaped = True
        return self.returncode


class MonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        micro.ACTIVE = asyncio.Lock()
        micro.ACTIVE_SESSION = {"ws": None}
        self.players = []
        self.commands = []
        self.missing_pacat = False
        self.when_started = None

        async def spawn(*args, **kwargs):
            self.commands.append((args, kwargs))
            if self.missing_pacat and args[0] == "pacat":
                raise FileNotFoundError("pacat")
            self.assertTrue(all(old.reaped for old in self.players), "overlapping playback")
            player = Player(kwargs["stderr"] == asyncio.subprocess.PIPE)
            self.players.append(player)
            if self.when_started is not None:
                await self.when_started()
            return player

        self.enterContext(patch.object(micro.asyncio, "create_subprocess_exec", spawn))
        self.enterContext(patch.object(micro, "set_stream_volume"))
        self.client = TestClient(TestServer(micro.create_app("test", "https://kara.test")))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)

    async def connect(self, profile=None, rate=48000, echo_guard="0", volume=None):
        query = f"/audio?token=test&output=analog&rate={rate}"
        if profile is not None:
            query += f"&profile={profile}"
        if echo_guard is not None:
            query += f"&echo_guard={echo_guard}"
        if volume is not None:
            query += f"&volume={volume}"
        return await self.client.ws_connect(query, headers={"Origin": "https://kara.test"})

    async def levels(self, socket):
        await socket.send_str("levels")
        return await socket.receive_json(timeout=1)

    async def block(self, player, frames):
        audio = np.frombuffer(await asyncio.wait_for(player.pending_write.get(), 1),
                              dtype="<i2").reshape(-1, 2)
        self.assertEqual(audio.shape, (frames, 2))
        np.testing.assert_array_equal(audio[:, 0], audio[:, 1])
        # Mid-block amplitude distinguishes input packets at either capture rate.
        return int(audio[frames // 2, 0])

    async def stop_and_check(self, socket, player):
        await socket.send_str("stop")
        await socket.receive(timeout=1)
        await self.released()
        self.assertTrue(player.closed and player.reaped)

    async def released(self):
        async with asyncio.timeout(1):
            while micro.ACTIVE.locked():
                await asyncio.sleep(.001)
        self.assertIsNone(micro.ACTIVE_SESSION["ws"])

    async def test_preferences_authentication_port_change_and_stock_fallback(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "monitor-profiles.json"
            settings = preferences.DEFAULTS | {"profile": "low-latency", "volume_percent": 60,
                                               "experimental": False}
            port = "analog-output-speaker"
            preferences.update_profile(path, micro.ANALOG_TARGET, port, settings)
            self.enterContext(patch.object(micro, "output_port", side_effect=lambda sink: port))
            which = self.enterContext(patch.object(micro.shutil, "which", return_value="/usr/bin/pacat"))
            client = TestClient(TestServer(micro.create_app("test", "https://kara.test", profiles_path=path)))
            await client.start_server()
            self.addAsyncCleanup(client.close)
            uri = "/monitor-preferences?output=analog"
            self.assertEqual((await client.get(uri)).status, 403)
            preset = await (await client.get(uri + "&token=test")).json()
            self.assertTrue(preset["saved"])
            self.assertEqual(preset["settings"], settings)
            self.assertNotIn(micro.ANALOG_TARGET, json.dumps(preset))
            target = "/audio?token=test&output=analog&profile=low-latency&route=" + preset["route"]
            headers = {"Origin": "https://kara.test"}
            socket = await client.ws_connect(target, headers=headers)
            ready = await socket.receive_json(timeout=1)
            self.assertTrue(ready["ready"])
            self.assertEqual(ready["device"], "Sortie analogique du PC")
            self.assertIn(f"--device={micro.ANALOG_TARGET}", self.commands[-1][0])
            self.assertIn("--property=node.dont-fallback=true", self.commands[-1][0])
            port = "analog-output-headphones"
            with self.assertRaises(WSServerHandshakeError) as rejected:
                await client.ws_connect(target, headers=headers)
            self.assertEqual(rejected.exception.status, 409)
            self.assertFalse(self.players[0].reaped)
            fallback = await (await client.get(uri + "&token=test")).json()
            self.assertFalse(fallback["saved"])
            self.assertEqual(fallback["settings"], preferences.DEFAULTS)
            self.assertNotEqual(fallback["route"], preset["route"])
            which.return_value = None
            native = await (await client.get(uri + "&token=test")).json()
            self.assertEqual(native["settings"]["profile"], "stable")
            await self.stop_and_check(socket, self.players[0])

            bluetooth = "bluez_output.12_34_56_78_9A_BC.1"
            with patch.object(micro, "selected_target", return_value=bluetooth):
                route = await (await client.get("/monitor-preferences?token=test&output=default")).json()
                socket = await client.ws_connect(
                    "/audio?token=test&output=default&profile=balanced&route=" + route["route"], headers=headers)
            ready = await socket.receive_json(timeout=1)
            self.assertEqual(ready["device"], "Sortie PipeWire par defaut")
            self.assertIn(f"--device={bluetooth}", self.commands[-1][0])
            await self.stop_and_check(socket, self.players[-1])

    async def test_profiles_and_reference_restore(self):
        for name, block_ms, capacity in [("balanced", 10, 40), ("low-latency", 10, 20),
                                         (None, 20, 80)]:
            with self.subTest(profile=name):
                socket = await self.connect(name, rate=44100)
                settings = await socket.receive_json(timeout=1)
                self.assertEqual(settings["profile"], name or "stable")
                self.assertEqual(settings["block_ms"], block_ms)
                self.assertEqual(settings["queue_max_ms"], capacity)
                command, spawn_options = self.commands[-1]
                if name:
                    self.assertEqual(command[0], "pacat")
                    self.assertIn("--client-name=Kara retour micro", command)
                    self.assertIn("--latency-msec=20", command)
                    self.assertIn("--process-time-msec=5", command)
                    self.assertEqual(settings["requested_latency_ms"], 20)
                else:
                    self.assertEqual(command[0], "pw-cat")
                    self.assertEqual(command[command.index("--latency") + 1], "10ms")
                    self.assertEqual(settings["requested_latency_ms"], 10)
                self.assertEqual(spawn_options.get("pipesize"), os.sysconf("SC_PAGE_SIZE") if name else None)
                player = self.players[-1]
                player.gate.set()
                actual = []
                for seq, gap in enumerate([0, .002, .005, .002]):
                    await asyncio.sleep(gap)
                    await socket.send_bytes(packet(seq, 1000 + seq * 1000, 44100, block_ms))
                    actual.append(await self.block(player, 48 * block_ms))
                metrics = await self.levels(socket)
                self.assertEqual(metrics["sequence_missing"], 0)
                self.assertEqual(metrics["queue_drops"], 0)
                self.assertEqual(metrics["frames"], 4 * 441 * block_ms // 10)
                diag = metrics["diagnostic"]
                self.assertEqual(diag["timings_ms"]["receive_ms"]["n"], 4)
                self.assertIsNone(diag["timings_ms"]["queue_ms"]["p95"])
                self.assertIsNone(diag["submission_frames_per_s"])
                self.assertEqual(actual, sorted(set(actual)))
                await self.stop_and_check(socket, player)
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.connect("unknown")
        self.assertEqual(failure.exception.status, 400)
        self.assertEqual(len(self.players), 3)

    async def test_stable_available_after_experimental_startup_failure(self):
        self.missing_pacat = True
        with self.assertRaises(WSServerHandshakeError) as failure:
            await self.connect("balanced")
        self.assertEqual(failure.exception.status, 503)
        await self.released()
        socket = await self.connect()
        self.assertEqual((await socket.receive_json(timeout=1))["profile"], "stable")
        await self.stop_and_check(socket, self.players[-1])

    async def test_experimental_query_cannot_enable_monitoring_during_calibration(self):
        socket = await self.client.ws_connect(
            "/audio?token=test&output=analog&calibration=1&profile=low-latency&echo_guard=1",
            headers={"Origin": "https://kara.test"})
        self.assertTrue((await socket.receive_json(timeout=1))["ready"])
        await socket.send_bytes(packet(0, 1000))
        self.assertIn("error", await socket.receive_json(timeout=1))
        await socket.receive(timeout=2)
        await self.released()
        self.assertEqual(self.commands, [])

    async def test_new_profile_supersedes_a_pending_start(self):
        pending = asyncio.Event()
        continue_spawn = asyncio.Event()

        async def stall_first_start():
            self.when_started = None
            pending.set()
            await continue_spawn.wait()

        self.when_started = stall_first_start
        first = asyncio.create_task(self.connect("stable"))
        await asyncio.wait_for(pending.wait(), 1)
        old_offer = micro.ACTIVE_SESSION["monitor_offer"]
        second = asyncio.create_task(self.connect("balanced"))
        try:
            async with asyncio.timeout(1):
                while micro.ACTIVE_SESSION.get("monitor_offer") is old_offer:
                    await asyncio.sleep(.001)
        finally:
            continue_spawn.set()
        with self.assertRaises(WSServerHandshakeError) as failure:
            await first
        self.assertEqual(failure.exception.status, 409)
        socket = await asyncio.wait_for(second, 1)
        self.assertEqual((await socket.receive_json(timeout=1))["profile"], "balanced")
        self.assertTrue(self.players[0].closed and self.players[0].reaped)
        self.assertTrue(self.players[0].pending_write.empty())
        await self.stop_and_check(socket, self.players[-1])

    async def test_active_reconnect_reaps_old_player_before_new_audio(self):
        first = await self.connect("balanced")
        await first.receive_json(timeout=1)
        old_player = self.players[-1]
        await first.send_bytes(packet(0, 1000))
        await self.block(old_player, 480)  # Leave the first drain pending.
        await first.send_bytes(packet(1, 1001))
        reconnect = asyncio.create_task(self.connect("low-latency"))
        self.assertEqual((await first.receive(timeout=1)).type, WSMsgType.CLOSE)
        second = await asyncio.wait_for(reconnect, 1)
        self.assertEqual((await second.receive_json(timeout=1))["profile"], "low-latency")
        fresh = (await self.levels(second))["diagnostic"]
        self.assertEqual(fresh["queue_high_water_packets"], 0)
        self.assertEqual(fresh["relative_transit_ms"]["n"], 0)
        self.assertTrue(old_player.closed and old_player.reaped)
        self.assertTrue(old_player.pending_write.empty())
        new_player = self.players[-1]
        new_player.gate.set()
        await second.send_bytes(packet(0, 1000))
        await self.block(new_player, 480)
        await self.stop_and_check(second, new_player)

    async def test_echo_guard_default_gain_and_manual_stop_during_write(self):
        for profile, ms in [("stable", 20), ("balanced", 10), ("low-latency", 10)]:
            with self.subTest(profile=profile):
                socket = await self.connect(profile, echo_guard=None)
                settings = await socket.receive_json(timeout=1)
                self.assertTrue(settings["echo_guard"])
                self.assertEqual(settings["echo_guard_attenuation_db"], 9)
                args = self.commands[-1][0]
                if profile == "stable":
                    self.assertEqual(float(args[args.index("--volume") + 1]), .125)
                else:
                    self.assertIn("--volume=32768", args)
                player = self.players[-1]
                await socket.send_bytes(packet(0, 8000, ms=ms))
                actual = await self.block(player, ms * 48)
                baseline = micro.shape_monitor(np.array([8000 / 32768]), micro.monitor_peak(False))[0] * 32767
                self.assertAlmostEqual(20 * np.log10(actual / baseline), -9, places=2)
                await socket.send_bytes(packet(1, 9000, ms=ms))
                await self.stop_and_check(socket, player)
                self.assertTrue(player.pending_write.empty())

    async def test_local_diagnostics_is_opt_in_and_excludes_audio(self):
        async with self.client.get("/monitor-diagnostics?token=test") as disabled:
            self.assertEqual(disabled.status, 404)
        async with TestClient(TestServer(micro.create_app(
                "test", "https://kara.test", local_diagnostics=True))) as local:
            async with local.get("/monitor-diagnostics") as denied:
                self.assertEqual(denied.status, 403)
            socket = await local.ws_connect(
                "/audio?token=test&output=analog&profile=balanced",
                headers={"Origin": "https://kara.test"})
            self.assertTrue((await socket.receive_json(timeout=1))["local_diagnostics"])
            await socket.send_str("diagnostics:" + json.dumps({
                "capture": {"echoCancellation": True, "sampleRate": 48000, "deviceId": "private"},
                "client": {"sent": 10, "audio": [1, 2, 3], "timings_ms": {"rtt_ms": {"p50": 2.1}}}}))
            await self.levels(socket)
            result = await local.get("/monitor-diagnostics?token=test")
            snapshot = await result.json()
            self.assertEqual(snapshot["capture"], {"echoCancellation": True, "sampleRate": 48000})
            self.assertEqual(snapshot["client"]["sent"], 10)
            self.assertNotIn("audio", snapshot["client"])
            self.assertEqual(snapshot["client"]["timings_ms"]["rtt_ms"], {"p50": 2.1})
            self.assertTrue(snapshot["server"]["levels"])
            await self.stop_and_check(socket, self.players[-1])
            async with local.get("/monitor-diagnostics?token=test") as stopped:
                self.assertFalse((await stopped.json())["active"])

    async def test_protected_volume_is_capped_for_start_and_slider(self):
        socket = await self.connect("balanced", echo_guard="1", volume=180)
        await socket.receive_json(timeout=1)
        self.assertIn("--volume=65536", self.commands[-1][0])
        async with asyncio.timeout(1):
            while not micro.set_stream_volume.called:
                await asyncio.sleep(.001)
        await socket.send_str("volume:80")
        self.assertTrue((await self.levels(socket))["levels"])
        await self.stop_and_check(socket, self.players[-1])
        volumes = [entry.args[1] for entry in micro.set_stream_volume.call_args_list]
        self.assertEqual(volumes, [100, 80])

    async def test_framed_stable_rejects_duplicate_and_legacy_switch(self):
        socket = await self.connect("stable")
        await socket.receive_json(timeout=1)
        player = self.players[-1]
        player.gate.set()
        first = packet(0, 1000, ms=20)
        await socket.send_bytes(first)
        await self.block(player, 960)
        await socket.send_bytes(first)
        await socket.send_bytes(first[16:])
        await socket.send_bytes(packet(1, 2000, ms=20))
        await self.block(player, 960)
        metrics = await self.levels(socket)
        self.assertEqual(metrics["sequence_reordered"], 1)
        self.assertEqual(metrics["invalid_packets"], 1)
        self.assertEqual(metrics["frames"], 2 * 960)
        self.assertTrue(player.pending_write.empty())
        await self.stop_and_check(socket, player)

    async def test_room_worklet_clock_and_background_reason_survive_reconnect(self):
        timeline = []
        diagnostics = SimpleNamespace(emit=lambda *k, **data: timeline.append((k, data)),
                                      pcm=lambda *_a, **_data: None, active=lambda: False)
        room = RoomAudio(diagnostics)
        settings = preferences.DEFAULTS | {"profile": "low-latency", "experimental": False,
                                          "output_index": 4, "output_description": "Enceinte de test"}
        with patch.object(room, "_route_settings", return_value=("sink-test", "test", settings)):
            room.open_turn("player", "Joueur", "tour", "Titre")
        self.addCleanup(room.close_turn)
        client = TestClient(TestServer(micro.create_app("", "https://kara.test", room_audio=room)))
        await client.start_server()
        self.addAsyncCleanup(client.close)

        async def connect():
            room.route.claim = "fixture"
            room.route.armed_until = time.monotonic() + 5
            ws = await client.ws_connect("/audio?token=fixture", headers={"Origin": "https://kara.test"})
            ready = await ws.receive_json(timeout=1)
            self.assertEqual((ready["mode"], ready["block_ms"]), ("pipewire-native-48k-stereo", 10))
            self.assertEqual(self.commands[-1][0][0], "pw-cat")
            self.assertEqual(micro.set_stream_volume.call_args.args[1], 100)
            self.assertTrue(micro.set_stream_volume.call_args.kwargs["verify"])
            self.assertEqual(micro.set_stream_volume.call_args.kwargs["sink_index"], 4)
            self.assertEqual((ready["output_verified"], ready["device"]), (True, "Enceinte de test"))
            self.players[-1].gate.set()
            return ws

        def packet_v2(sequence, frame, send_ms):
            return struct.pack("<4sIdQ", b"KTS2", sequence, send_ms, frame) + struct.pack("<h", 2000) * 480

        ws = await connect()
        await ws.send_bytes(packet_v2(0, 480, 5.))
        await self.block(self.players[-1], 480)
        first_end = room.route.last_sample
        await ws.send_bytes(packet_v2(1, 960, 17.))
        await self.block(self.players[-1], 480)
        last_end = room.route.last_sample
        self.assertAlmostEqual(last_end - first_end, .01, places=8)
        room.record_test(True)
        levels = await self.levels(ws)
        self.assertEqual(levels["stage"], "microphone-test")
        self.assertGreater(levels["rms"], levels["guarded_rms"])
        self.assertGreater(levels["guarded_rms"], levels["return_rms"])
        self.assertGreater(levels["return_rms"], 0)
        self.assertFalse(room.test_passed())
        with self.assertRaises(RoomError):
            room.confirm_return("another-player")
        room.confirm_return("player")
        self.assertTrue(room.test_passed())
        await ws.send_str("stop:background")
        await ws.receive(timeout=1)
        await self.released()
        self.assertEqual([d["reason"] for k, d in timeline if k == ("audio", "disconnect")], ["background"])
        ws = await connect()
        self.assertFalse(room.test_passed())
        await ws.send_bytes(packet_v2(0, 480, 2.))
        await self.block(self.players[-1], 480)
        self.assertGreater(room.route.last_sample, last_end)
        await self.stop_and_check(ws, self.players[-1])

    async def test_44100_resample_keeps_continuity_across_packets(self):
        socket = await self.connect("balanced", rate=44100)
        await socket.receive_json(timeout=1)
        player = self.players[-1]
        player.gate.set()
        samples = (12000 * np.sin(np.arange(441 * 6) * (2 * np.pi * 3310 / 44100))).astype("<i2")
        played = []
        for seq, pcm in enumerate(np.split(samples, 6)):
            await socket.send_bytes(struct.pack("<4sId", b"KTS1", seq, seq * 10) + pcm.tobytes())
            written = await asyncio.wait_for(player.pending_write.get(), 1)
            played.append(np.frombuffer(written, dtype="<i2")[::2])
        continuous = resample_poly(samples.astype(np.float32) / 32768, 160, 147)
        expected = (micro.shape_monitor(continuous, micro.monitor_peak(False)) * 32767).astype("<i2")
        np.testing.assert_array_equal(np.concatenate(played)[12:], expected[:-12])
        await self.stop_and_check(socket, player)

    async def test_pulse_counters_are_separate_from_queue_starvation(self):
        socket = await self.connect("low-latency")
        await socket.receive_json(timeout=1)
        player = self.players[-1]
        player.stderr.feed_data(b"Pulse status\rBuffer metrics: maxlength=4194304, tlength=2944, "
                                b"prebuf=1924, minreq=1024\nStream under")
        await asyncio.sleep(0)
        player.stderr.feed_data(b"run.\nIgnored device metadata\nStream underrun.\r\n")
        await asyncio.sleep(0)
        metrics = await self.levels(socket)
        self.assertEqual(metrics["pulse_underruns"], 2)
        self.assertEqual(metrics["pulse_buffer_bytes"]["tlength"], 2944)
        self.assertEqual(metrics["source_starvations"], 0)
        await self.stop_and_check(socket, player)

    async def test_short_buffer_only_changes_the_pulse_latency_request(self):
        async def connect(profile):
            return await self.client.ws_connect(
                f"/audio?token=test&output=analog&profile={profile}&pulse_buffer=short",
                headers={"Origin": "https://kara.test"})

        with self.assertRaises(WSServerHandshakeError) as failure:
            await connect("stable")
        self.assertEqual(failure.exception.status, 400)
        socket = await connect("balanced")
        settings = await socket.receive_json(timeout=1)
        self.assertEqual((settings["requested_latency_ms"], settings["queue_max_ms"]), (15, 40))
        self.assertIn("--latency-msec=15", self.commands[-1][0])
        self.assertIn("--process-time-msec=5", self.commands[-1][0])
        await self.stop_and_check(socket, self.players[-1])

    async def test_recovery_burst_reaches_playback_only_after_old_pcm_is_discarded(self):
        socket = await self.connect("balanced")
        await socket.receive_json(timeout=1)
        player = self.players[-1]
        player.gate.set()
        clock = asyncio.get_running_loop().time
        start = clock()
        await socket.send_bytes(packet(0, 1000, timestamp=1000))
        first = await self.block(player, 480)
        await asyncio.sleep(.28)
        # The old frame waited in transport. Later frames catch up to the
        # sender's original cadence, with no shared clock origin.
        await socket.send_bytes(packet(1, 6000, timestamp=1010))
        await socket.send_bytes(packet(2, 1000, timestamp=1000 + (clock() - start) * 1000))
        self.assertEqual(await self.block(player, 480), first)
        self.assertTrue(player.pending_write.empty())
        metrics = await self.levels(socket)
        self.assertEqual(metrics["transport_stale_drops"], 1)
        self.assertEqual(metrics["queue_drops"], 0)
        self.assertEqual(metrics["sequence_missing"], 0)
        await self.stop_and_check(socket, player)

    async def test_burst_drops_only_oldest_unwritten_blocks(self):
        for profile, capacity in [("stable", 4), ("balanced", 4), ("low-latency", 2)]:
            with self.subTest(profile=profile):
                socket = await self.connect(profile)
                settings = await socket.receive_json(timeout=1)
                block_ms = settings["block_ms"]
                player = self.players[-1]
                await socket.send_bytes(packet(0, 1000, ms=block_ms))
                first = await self.block(player, 48 * block_ms)
                # Keep the first drain pending until all eight packets reach
                # the server, simulating a temporarily non-reading sink.
                for seq in range(1, 9):
                    await socket.send_bytes(packet(seq, 1000 + seq * 500, ms=block_ms))
                metrics = await self.levels(socket)
                self.assertEqual(metrics["queue_drops"], 8 - capacity)
                self.assertEqual(metrics["queue_est_ms"], capacity * block_ms)
                self.assertEqual(metrics["sequence_missing"], 0)
                diagnostic = metrics["diagnostic"]
                self.assertEqual(diagnostic["queue_high_water_packets"], capacity)
                self.assertEqual(diagnostic["timings_ms"]["stdin_write_ms"]["n"], 1)
                self.assertEqual(diagnostic["timings_ms"]["stdin_write_drain_ms"]["n"], 0)
                player.gate.set()
                kept = [await self.block(player, 48 * block_ms) for _ in range(capacity)]
                expected = (micro.shape_monitor(
                    np.array([(1000 + i * 500) / 32768 for i in range(9 - capacity, 9)], dtype=np.float32),
                    micro.monitor_peak(False)) * 32767).astype("<i2").tolist()
                self.assertLess(first, kept[0])
                self.assertEqual(kept, expected)
                self.assertTrue(player.pending_write.empty())
                await self.stop_and_check(socket, player)

    async def test_network_pause_loss_duplicates_and_bad_packets(self):
        socket = await self.connect("low-latency")
        await socket.receive_json(timeout=1)
        player = self.players[-1]
        player.gate.set()
        await socket.send_bytes(packet(0, 1000))
        first = await self.block(player, 480)
        await asyncio.sleep(.075)
        during = await self.levels(socket)
        self.assertEqual(during["source_starvations"], 1)
        self.assertGreater(during["source_starvation_ms"], 35)
        await socket.send_bytes(packet(2, 2000))
        self.assertGreater(await self.block(player, 480), first)
        await socket.send_bytes(packet(2, 4000))
        await socket.send_bytes(packet(1, 4000))
        await socket.send_bytes(packet(3, 4000, timestamp=float("nan")))
        await socket.send_bytes(packet(3, 4000, ms=20))
        await socket.send_bytes(packet(3, 3000))
        self.assertGreater(await self.block(player, 480), first)
        metrics = await self.levels(socket)
        self.assertEqual(metrics["source_starvations"], 1)
        self.assertEqual(metrics["sequence_missing"], 1)
        self.assertEqual(metrics["sequence_reordered"], 2)
        self.assertEqual(metrics["invalid_packets"], 2)
        self.assertEqual(metrics["frames"], 3 * 480)
        self.assertTrue(player.pending_write.empty())
        await socket.close()
        await self.released()
        self.assertTrue(player.closed and player.reaped)

    async def test_stalled_writer_releases_session(self):
        socket = await self.connect("low-latency")
        await socket.receive_json(timeout=1)
        await socket.send_bytes(packet(0, 1000))
        player = self.players[-1]
        await self.block(player, 480)
        result = await socket.receive(timeout=2)
        self.assertIn(result.type, (WSMsgType.CLOSE, WSMsgType.CLOSED))
        await self.released()
        self.assertTrue(player.closed and player.reaped)


class VolumeTests(unittest.TestCase):
    def test_stream_verification_requires_expected_output_unmuted_at_unity(self):
        stream = {"index": 5, "sink": 4, "mute": False,
                  "properties": {"node.name": "kara-return"}, "volume": {"mono": {"value": 65536}}}
        with patch.object(micro.subprocess, "check_output") as query, patch.object(micro.subprocess, "run") as run:
            query.return_value = json.dumps([stream])
            self.assertTrue(micro.set_stream_volume("kara-return", 100, verify=True, sink_index=4))
            self.assertFalse(micro.set_stream_volume("kara-return", 100, verify=True, sink_index=3))
            stream["mute"] = True
            query.return_value = json.dumps([stream])
            self.assertFalse(micro.set_stream_volume("kara-return", 100, verify=True, sink_index=4))
            run.assert_not_called()

    def test_only_owned_stream_can_receive_gain_change(self):
        rows = [{"index": 3, "properties": {"node.name": "Strawberry"}},
                {"index": 4, "properties": {"node.name": "pw-cat"}},
                {"index": 5, "properties": {"node.name": "kara-microphone-unique"}}]
        with patch.object(micro.subprocess, "check_output", return_value=json.dumps(rows)) as query, \
                patch.object(micro.subprocess, "run") as run:
            micro.set_stream_volume("kara-microphone-invalid", 160)
            run.assert_not_called()
            micro.set_stream_volume("kara-microphone-unique", 75)
            query.assert_called_with(("pactl", "--format=json", "list", "sink-inputs"), timeout=2)
            self.assertEqual(run.call_args.args[0], ("pactl", "set-sink-input-volume", "5", "75%"))
            rows.append({"index": 6, "properties": {"node.name": "kara-microphone-unique"}})
            query.return_value = json.dumps(rows)
            run.reset_mock()
            micro.set_stream_volume("kara-microphone-unique", 160)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
