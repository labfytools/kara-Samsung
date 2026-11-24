"""HTTP/WebSocket integration tests; subprocesses are fake or harmless Python children, never playback."""
import asyncio
import signal
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from aiohttp import WSMsgType, WSServerHandshakeError
from aiohttp.test_utils import TestClient, TestServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _kara_audio_calibration as calibration
import _kara_micro_test as micro


REAL_SPAWN = asyncio.create_subprocess_exec


class FakeInput:
    def __init__(self, process):
        self.process = process
        self.writes = []
        self.closed = False

    def write(self, data):
        self.writes.append(data)
        self.process.written.set()

    async def drain(self):
        if self.process.mode == "drain_timeout":
            await asyncio.Future()
        if self.process.mode == "broken_pipe":
            raise BrokenPipeError()
        self.process.drained = True

    def close(self):
        self.closed = True
        if self.process.mode not in ("hang", "drain_timeout"):
            self.process.returncode = 1 if self.process.mode == "exit_error" else 0


class FakeProcess:
    def __init__(self, mode="ok", discovery=False):
        self.mode = mode
        self.returncode = 1 if mode == "stale_target" else None
        self.stdin = None if discovery else FakeInput(self)
        self.written = asyncio.Event()
        self.drained = False
        self.terminated = False
        self.killed = False
        self.reaped = False

    async def communicate(self):
        self.returncode = 1 if self.mode == "target_error" else 0
        return b"chosen.default.sink\n", b""

    async def wait(self):
        while self.returncode is None:
            await asyncio.sleep(0.001)
        self.reaped = True
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.killed = True
        self.returncode = -9


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        micro.ACTIVE = asyncio.Lock()
        micro.ACTIVE_SESSION = {"ws": None}
        self.commands = []
        self.players = []
        self.mode = "ok"
        self.spawned = asyncio.Event()

        async def spawn(*cmd, **kwargs):
            self.commands.append(cmd)
            discovery = cmd[0] == "pactl"
            if not discovery and self.mode == "startup_timeout":
                await asyncio.Future()
            process = FakeProcess(self.mode, discovery)
            if not discovery:
                self.players.append(process)
                self.spawned.set()
            return process

        self.patches = [patch.object(asyncio, "create_subprocess_exec", spawn),
                        patch.object(calibration, "STARTUP_TIMEOUT", 0.05),
                        patch.object(calibration, "DRAIN_TIMEOUT", 0.05),
                        patch.object(calibration, "EXIT_TIMEOUT", 0.05)]
        for item in self.patches:
            item.start()
        self.client = TestClient(TestServer(micro.create_app("secret", "https://kara.test")))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        for item in reversed(self.patches):
            item.stop()

    async def connect(self, query="", calibration_mode=True):
        suffix = "&calibration=1" if calibration_mode else ""
        return await self.client.ws_connect("/audio?token=secret" + suffix + query,
                                            headers={"Origin": "https://kara.test"})

    async def ready(self, **kwargs):
        ws = await self.connect(**kwargs)
        message = await ws.receive_json()
        self.assertTrue(message["ready"])
        return ws, message

    async def released(self):
        for _ in range(200):
            if not micro.ACTIVE.locked():
                return
            await asyncio.sleep(0.002)
        self.fail("Session lock not released")

    async def probe(self, ws, method):
        await ws.send_str("probe:" + method)
        binary = await ws.receive()
        self.assertEqual(binary.type, WSMsgType.BINARY)
        self.assertEqual(binary.data, calibration.generate_probe(method))
        stats = await ws.receive_json()
        self.assertEqual(stats["probe"], method)
        return binary.data, stats

    async def test_probe_reference_and_telemetry(self):
        ws, ready = await self.ready(query="&rate=44100")
        self.assertEqual(ready["target"], "chosen.default.sink")
        self.assertEqual(ready["rate"], 48000)
        data, stats = await self.probe(ws, "chirp")
        player = self.players[0]
        self.assertEqual(player.stdin.writes, [data])
        self.assertTrue(player.drained and player.stdin.closed and player.reaped)
        self.assertEqual(stats["configured_latency_ms"], 10)
        self.assertGreaterEqual(stats["request_to_write_ms"], stats["process_startup_ms"])
        self.assertAlmostEqual(stats["request_to_drain_ms"],
                               stats["request_to_write_ms"] + stats["drain_ms"])
        self.assertGreaterEqual(stats["request_to_exit_ms"], stats["request_to_drain_ms"])
        self.assertNotIn("startup_guard_ms", stats)
        cmd = self.commands[-1]
        self.assertNotIn("--sample-count", cmd)
        self.assertNotIn("--volume", cmd)
        self.assertEqual(cmd[cmd.index("--channels") + 1], "1")
        self.assertEqual(cmd[cmd.index("--target") + 1], ready["target"])
        self.assertIn("node.dont-move=true", cmd[cmd.index("--properties") + 1])
        self.assertIn("node.dont-fallback=true", cmd[cmd.index("--properties") + 1])
        self.assertIn("node.dont-reconnect=true", cmd[cmd.index("--properties") + 1])
        await ws.send_str("stop")
        await ws.receive()
        await self.released()

    async def test_auth_origin_rate_output_validation(self):
        for query, origin, status in [
                ("token=bad", "https://kara.test", 403),
                ("token=secret", "https://other.test", 403),
                ("token=secret&rate=no", "https://kara.test", 400),
                ("token=secret&rate=16000", "https://kara.test", 400),
                ("token=secret&output=other", "https://kara.test", 400)]:
            with self.subTest(query=query, origin=origin):
                with self.assertRaises(WSServerHandshakeError) as error:
                    await self.client.ws_connect("/audio?calibration=1&" + query,
                                                  headers={"Origin": origin})
                self.assertEqual(error.exception.status, status)
        self.assertEqual(self.commands, [])

    async def test_unsafe_commands_fail_closed(self):
        for command in [b"\x01\x00" * 4800, "monitor:on", "monitor:off",
                        "volume:180", "probe:other"]:
            with self.subTest(command=str(command)[:30]):
                ws, _ = await self.ready()
                if isinstance(command, bytes):
                    await ws.send_bytes(command)
                else:
                    await ws.send_str(command)
                self.assertEqual((await ws.receive_json())["error"], "unsupported_command")
                await ws.close()
                await self.released()
        self.assertEqual(self.players, [])
        self.assertTrue(all(cmd == ("pactl", "get-default-sink") for cmd in self.commands))

    async def test_ping_echo_and_bounds(self):
        for bad in ["ping:NaN", "ping:Infinity", "ping:-1", "ping:1e6",
                    "ping:" + "1" * 80, "ping:1\n"]:
            ws, _ = await self.ready()
            await ws.send_str(bad)
            self.assertEqual((await ws.receive_json())["error"], "invalid_or_rate_limited_ping")
            await ws.close()
            await self.released()
        ws, _ = await self.ready()
        await ws.send_str("ping:123.456789123456789")
        self.assertEqual((await ws.receive()).data, "pong:123.456789123456789")
        await ws.send_str("ping:124")
        self.assertEqual((await ws.receive_json())["error"], "invalid_or_rate_limited_ping")

    async def test_overlap_aborts_before_close(self):
        self.mode = "hang"
        ws, _ = await self.ready()
        await ws.send_str("probe:pn")
        await self.spawned.wait()
        await ws.send_str("probe:chirp")
        self.assertEqual((await ws.receive_json())["error"], "probe_in_progress")
        self.assertTrue(self.players[0].terminated)
        self.assertTrue(self.players[0].reaped)

    async def test_invalid_during_probe_reaps_before_error(self):
        self.mode = "hang"
        ws, _ = await self.ready()
        await ws.send_str("probe:pn")
        await self.spawned.wait()
        await ws.send_bytes(b"\0\0")
        self.assertEqual((await ws.receive_json())["error"], "unsupported_command")
        self.assertTrue(self.players[0].terminated and self.players[0].reaped)

    async def test_rate_limit(self):
        ws, _ = await self.ready()
        await self.probe(ws, "chirp")
        await ws.send_str("probe:pn")
        self.assertEqual((await ws.receive_json())["error"], "probe_rate_limited")
        self.assertEqual(len(self.players), 1)

    async def test_warmup_and_useful_limits(self):
        with patch.object(calibration, "PROBE_INTERVAL", 0):
            ws, _ = await self.ready()
            for method in ["warmup"] + ["chirp"] * 6 + ["pn"] * 6:
                await self.probe(ws, method)
            await ws.send_str("probe:pn")
            self.assertEqual((await ws.receive_json())["error"], "probe_limit")
            await ws.close()
            await self.released()
            ws, _ = await self.ready()
            await self.probe(ws, "warmup")
            await ws.send_str("probe:warmup")
            self.assertEqual((await ws.receive_json())["error"], "probe_limit")

    async def test_stop_disconnect_reconnect_and_exclusivity(self):
        for disconnect in [False, True]:
            self.mode = "hang"
            self.spawned.clear()
            ws, _ = await self.ready()
            for normal in [False, True]:
                with self.assertRaises(WSServerHandshakeError) as error:
                    await self.connect(calibration_mode=not normal)
                self.assertEqual(error.exception.status, 409)
            await ws.send_str("probe:pn")
            await self.spawned.wait()
            if disconnect:
                await ws.close()
            else:
                await ws.send_str("stop")
                await ws.receive()
            await self.released()
            self.assertTrue(self.players[-1].terminated and self.players[-1].reaped)
        self.mode = "ok"
        ws, _ = await self.ready(query="&output=analog")
        await self.probe(ws, "pn")
        self.assertEqual(self.commands[-1][self.commands[-1].index("--target") + 1],
                         calibration.ANALOG_TARGET)

    async def test_startup_drain_exit_and_stale_target_failures(self):
        for mode in ["startup_timeout", "drain_timeout", "broken_pipe", "hang",
                     "exit_error", "stale_target"]:
            with self.subTest(mode=mode):
                self.mode = mode
                ws, _ = await self.ready()
                before = len(self.commands)
                await ws.send_str("probe:chirp")
                self.assertEqual((await ws.receive_json())["error"], "probe_failed")
                # No fallback process is launched after a stale/failed target.
                self.assertEqual(len(self.commands), before + 1)
                if mode != "startup_timeout":
                    self.assertTrue(self.players[-1].reaped)
                await ws.close()
                await self.released()

    async def test_stubborn_cleanup_keeps_ownership_despite_repeated_cancellation(self):
        for action in ("stop", "disconnect", "invalid"):
            with self.subTest(action=action):
                self.mode = "hang"
                self.spawned.clear()
                entered = asyncio.Event()
                owners = {}
                real_target = calibration.selected_target
                real_close = calibration.close_process

                async def target(output):
                    owners["handler"] = asyncio.current_task()
                    return await real_target(output)

                async def close(process):
                    owners["worker"] = asyncio.current_task()
                    entered.set()
                    await real_close(process)

                with patch.object(calibration, "selected_target", target), \
                        patch.object(calibration, "close_process", close):
                    ws, _ = await self.ready(query="&output=analog")
                    await ws.send_str("probe:pn")
                    await self.spawned.wait()
                    player = self.players[-1]

                    def ignore_term():
                        player.terminated = True

                    original_kill = player.kill

                    def kill():
                        self.assertTrue(micro.ACTIVE.locked())
                        original_kill()

                    original_wait = player.wait

                    async def wait():
                        result = await original_wait()
                        self.assertTrue(micro.ACTIVE.locked())
                        return result

                    player.terminate = ignore_term
                    player.kill = kill
                    player.wait = wait
                    # EXIT_TIMEOUT expires first; cancellation must arrive inside
                    # the subsequent SIGTERM grace period, not ordinary playback.
                    await asyncio.wait_for(entered.wait(), 1)
                    if action == "disconnect":
                        await ws.close()
                    else:
                        await ws.send_str("stop" if action == "stop" else "invalid")
                    for _ in range(3):
                        await asyncio.sleep(0.01)
                        owners["worker"].cancel()
                        owners["handler"].cancel()
                        self.assertTrue(micro.ACTIVE.locked())
                        self.assertFalse(player.reaped)
                    with self.assertRaises(WSServerHandshakeError) as error:
                        await self.connect()
                    self.assertEqual(error.exception.status, 409)
                    try:
                        await asyncio.wait_for(asyncio.shield(owners["handler"]), 3)
                    except asyncio.CancelledError:
                        pass
                    self.assertTrue(player.terminated and player.killed and player.reaped)
                    self.assertFalse(micro.ACTIVE.locked())
                    await ws.close()

    async def test_real_stubborn_child_cleanup_defers_cancellation(self):
        # Install SIG_IGN before announcing readiness; no audio tools are used.
        process = await REAL_SPAWN(
            sys.executable, "-c",
            "import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
            "print('ready', flush=True); time.sleep(30)",
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
        try:
            self.assertEqual(await asyncio.wait_for(process.stdout.readline(), 2), b"ready\n")
            task = asyncio.create_task(calibration.close_process(process))
            for _ in range(3):
                await asyncio.sleep(0.02)
                task.cancel()
                self.assertIsNone(process.returncode)
                self.assertFalse(task.done())
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(task), 3)
            self.assertEqual(process.returncode, -signal.SIGKILL)
            self.assertEqual(await process.wait(), -signal.SIGKILL)
        finally:
            if process.returncode is None:
                process.kill()
            await process.wait()

    async def test_kill_lookup_race_still_reaps(self):
        player = FakeProcess("hang")
        player.terminate = lambda: None

        def vanished():
            player.returncode = 0
            raise ProcessLookupError()

        player.kill = vanished
        await calibration.close_process(player)
        self.assertTrue(player.reaped)

    async def test_target_discovery_failure(self):
        self.mode = "target_error"
        ws = await self.connect()
        self.assertEqual((await ws.receive_json())["error"], "target_unavailable")
        await ws.close()
        await self.released()
        self.assertFalse(self.players)

    async def test_time_bounds_release_ownership(self):
        for field, error in [("IDLE_TIMEOUT", "idle_timeout"),
                             ("SESSION_TIMEOUT", "session_timeout")]:
            with patch.object(calibration, field, 0.03):
                ws, _ = await self.ready()
                self.assertEqual((await ws.receive_json())["error"], error)
                await ws.close()
                await self.released()

    async def test_cleanup_exception_releases_lock(self):
        self.mode = "hang"
        ws, _ = await self.ready(query="&output=analog")
        await ws.send_str("probe:pn")
        await self.spawned.wait()
        real_close = calibration.close_process

        async def broken_close(process):
            await real_close(process)
            raise OSError("cleanup failed")

        with patch.object(calibration, "close_process", broken_close):
            await ws.send_bytes(b"\0\0")
            self.assertEqual((await ws.receive_json())["error"], "unsupported_command")
            await ws.close()
            await self.released()

    async def test_websocket_close_exception_releases_lock(self):
        ws, _ = await self.ready(query="&output=analog")
        server_ws = micro.ACTIVE_SESSION["ws"]
        real_close = server_ws.close

        async def broken_close(*args, **kwargs):
            await real_close(*args, **kwargs)
            raise OSError("socket close failed")

        with patch.object(server_ws, "close", broken_close):
            with self.assertLogs("aiohttp.server", level="ERROR"):
                await ws.send_str("stop")
                await ws.receive()
                await self.released()
                # Give aiohttp its turn to log the deliberately injected error.
                await asyncio.sleep(0.01)
        self.assertIsNone(micro.ACTIVE_SESSION["ws"])
        self.assertFalse(micro.ACTIVE_SESSION["calibration"])

    async def test_normal_audio_still_uses_live_stereo_path(self):
        # The normal path owns its existing pactl gain controls; fake those too.
        with patch.object(micro.subprocess, "check_output", return_value=""), \
                patch.object(micro.subprocess, "run"):
            ws = await self.connect(calibration_mode=False)
            ready = await ws.receive_json()
            self.assertEqual(ready["mode"], "pipewire-native-48k-stereo")
            await ws.send_bytes(b"\x10\x00" * 960)
            await asyncio.wait_for(self.players[-1].written.wait(), 0.5)
            cmd = self.commands[-1]
            self.assertEqual(cmd[cmd.index("--channels") + 1], "2")
            self.assertIn("--volume", cmd)
            self.assertEqual(len(self.players[-1].stdin.writes[0]), 3840)
            await ws.send_str("stop")
            await ws.receive()
            await self.released()

    async def test_public_static_route(self):
        with tempfile.TemporaryDirectory() as directory:
            page = Path(directory) / "page.html"
            asset = page.with_name("_kara_audio_calibration.js")
            asset.write_text("export const dsp = true;")
            with patch.object(micro, "PAGE", page):
                response = await self.client.get("/calibration.js")
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.text(), asset.read_text())
                self.assertEqual(response.content_type, "text/javascript")
                self.assertEqual(response.headers["Cache-Control"], "no-store")
                self.assertNotIn("Access-Control-Allow-Origin", response.headers)


class ReferenceTests(unittest.TestCase):
    def test_bounded_deterministic_references(self):
        for method in ("warmup", "chirp", "pn"):
            data = calibration.generate_probe(method)
            self.assertEqual(data, calibration.generate_probe(method))
            self.assertEqual(len(data), 9600)
            samples = np.frombuffer(data, dtype="<i2")
            self.assertLessEqual(np.max(np.abs(samples)), 5898)
            self.assertEqual(samples[0], 0)
            self.assertEqual(samples[-1], 0)
        self.assertEqual(calibration.generate_probe("warmup"), calibration.generate_probe("chirp"))
        with self.assertRaises(ValueError):
            calibration.generate_probe("live")


if __name__ == "__main__":
    unittest.main()
