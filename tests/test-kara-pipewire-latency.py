#!/usr/bin/env python3
"""V5 validation on an isolated PipeWire daemon and on the packet boundary."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
from time import monotonic, sleep
import unittest

import numpy as np

HERE = Path(__file__).resolve().parent
BIN = HERE.parent / "bin"
sys.path.insert(0, str(BIN))
import _kara_pipewire_latency as bench

spec = importlib.util.spec_from_file_location("voice_fixtures", HERE / "test-kara-voice-witness.py")
voice = importlib.util.module_from_spec(spec)
spec.loader.exec_module(voice)

NODE = "kara-microphone-0123456789abcdef"
SINK = "kara-v5-null-sink"

CONFIG = """
context.properties = {
  core.daemon = true core.name = pipewire-0 default.clock.rate = 48000
  default.clock.quantum = QUANTUM default.clock.min-quantum = 32 support.dbus = false
}
context.spa-libs = { audio.convert.* = audioconvert/libspa-audioconvert support.* = support/libspa-support }
context.modules = [
  { name = libpipewire-module-protocol-native }
  { name = libpipewire-module-client-node }
  { name = libpipewire-module-adapter }
  { name = libpipewire-module-spa-node-factory }
  { name = libpipewire-module-link-factory }
  { name = libpipewire-module-access }
  { name = libpipewire-module-metadata }
]
context.objects = [
  { factory = spa-node-factory args = { factory.name = support.node.driver
      node.name = kara-test-driver node.driver = true priority.driver = 30000
      node.group = kara-v51-mic-group } }
  { factory = adapter args = { factory.name = support.null-audio-sink node.name = kara-v5-null-sink
      media.class = Audio/Sink audio.position = [ FL FR ]
      adapter.auto-port-config = { mode = dsp monitor = true position = preserve } } }
]
"""


def waveform(digital_delay=174.25, output_delay=57.5, *, control=False, reflection=False,
             weak=False, distortion=False, muted_speaker=False):
    direct = voice.syllable(205)
    rng = np.random.default_rng(305)
    mic = rng.normal(0, .00025, 96000)
    monitor = np.zeros(96000)

    def add(target, samples, time):
        start = round(bench.FRAMES_PER_MS * (460 + time))
        target[start:start + len(samples)] += samples

    add(mic, direct, 0)
    if reflection:
        add(mic, .11 * direct, 49)
    if not control:
        returned = direct
        if distortion:
            returned = .28 * np.tanh(5 * np.convolve(direct, [.63, .24, -.09, .2], mode="full")[:len(direct)])
            returned *= np.linspace(.65, 1.1, len(returned))
        add(monitor, (.035 if weak else .21) * returned, digital_delay)
        if not muted_speaker:
            add(mic, (.034 if weak else .14) * returned, digital_delay + output_delay)
    return np.vstack((mic, monitor, monitor * .92)).astype("=f4")


def unit_trial(samples):
    return np.pad(samples, ((0, 0), (0, 4 * bench.RATE - samples.shape[1])))


def end_process(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


class PacketTests(unittest.TestCase):
    def read_packets(self, chunks):
        fd = os.memfd_create("kara-v5-synthetic-packets")
        try:
            os.write(fd, b"".join(chunks))
            os.lseek(fd, 0, os.SEEK_SET)
            # epoll does not accept a regular memory file. Use a real pipe,
            # keeping these small fixtures below its atomic write capacity.
            read, write = os.pipe()
            try:
                os.write(write, os.read(fd, 4096))
                os.close(write)
                write = None
                capture = bench.GraphCapture(read)
                try:
                    return capture.read_frames(171)
                finally:
                    capture.close()
            finally:
                os.close(read)
                if write is not None:
                    os.close(write)
        finally:
            os.close(fd)

    def packet(self, start, frames, ns, *, driver=17, xrun=0):
        return (bench.HEADER.pack(b"KPW1", frames, 48000, driver, bench.ROUTE_KARA, 0x700, start, ns, xrun)
                + b"\0" * (12 * frames))

    def test_variable_cycles_and_arbitrary_capture_start(self):
        start, values, route, present = self.read_packets([
            self.packet(43912, 64, 1000), self.packet(43976, 128, 1100)])
        self.assertEqual(start, 43912)
        self.assertEqual(values.shape, (3, 171))
        self.assertEqual(route, {bench.ROUTE_KARA})
        self.assertEqual(present, 7)

    def test_loss_driver_change_and_xrun_are_rejected(self):
        for second, error in ((self.packet(65, 128, 1100), "graph_sample_gap"),
                              (self.packet(64, 128, 1100, driver=18), "graph_clock_changed"),
                              (self.packet(64, 128, 1100, xrun=1024), "graph_xrun")):
            with self.subTest(error=error), self.assertRaisesRegex(bench.CaptureError, error):
                self.read_packets([self.packet(0, 64, 1000), second])

    def test_signal_and_mic_only_reflection(self):
        signal = unit_trial(waveform(control=True, reflection=True))
        quiet = [.00025, 0, 0]
        control = bench.inspect_trial(0, signal, {bench.ROUTE_NONE}, quiet, "control", 0)
        self.assertEqual(control["status"], "control_ready", control)
        result = bench.inspect_trial(0, signal, {bench.ROUTE_KARA}, quiet, "diagnostic", 250)
        self.assertEqual(result["reason"], "no_monitor_event", result)
        self.assertIsNone(result["acoustic_interval"])

    def test_monitor_signal_can_be_confirmed_without_an_acoustic_arrival(self):
        samples = unit_trial(waveform(muted_speaker=True, distortion=True, reflection=True))
        quiet = [.00025, 0, 0]
        row = bench.inspect_trial(400, samples, {bench.ROUTE_KARA}, quiet, "diagnostic", 256)
        self.assertEqual(row["status"], "graph_supported", row)
        self.assertAlmostEqual(row["graph_interval"]["ms"], 174.25, delta=.12)
        self.assertIsNone(row["monitor_to_witness_graph"])
        self.assertIsNone(row["physical_output_delay"])
        self.assertIsNone(row["acoustic_interval"])
        self.assertEqual(row["reason"], "no_unique_delayed_acoustic_match")
        mixed = bench.inspect_trial(400, samples, {bench.ROUTE_MIXED}, quiet, "diagnostic", 256)
        self.assertEqual(mixed["reason"], "kara_route_not_exclusive", mixed)
        self.assertIsNone(mixed["graph_interval"])

    def test_active_route_requires_all_three_pcm_planes(self):
        packet = bytearray(self.packet(0, 128, 1000))
        packet[21] = 1
        with self.assertRaisesRegex(bench.CaptureError, "unusable_graph_packet"):
            self.read_packets([packet])

    def test_alignment_does_not_depend_on_phase_within_energy_bins(self):
        values = unit_trial(waveform(weak=True, distortion=True, reflection=True))
        values[1:] = np.roll(values[1:], 256, axis=1)
        for offset in (0, 64, 104, 184):
            with self.subTest(offset=offset):
                row = bench.inspect_trial(offset, np.roll(values, offset, axis=1),
                                          {bench.ROUTE_KARA}, [.00025, 0, 0], "diagnostic", 260)
                self.assertEqual(row["status"], "graph_supported", row)
                self.assertAlmostEqual(row["acoustic_interval"]["ms"], 231.75, delta=.15)


@unittest.skipUnless(all(shutil.which(tool) for tool in ("pipewire", "pw-dump", "pw-link", "cc", "pkg-config")),
                     "requires PipeWire and C toolchain")
class IsolatedGraphTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tools = tempfile.TemporaryDirectory(prefix="kara-v5-tools-")
        cls.addClassCleanup(cls.tools.cleanup)
        cls.helper = Path(cls.tools.name) / "witness"
        cls.feeder = Path(cls.tools.name) / "feeder"
        bench.build_helper(cls.helper)
        flags = shlex.split(subprocess.check_output(
            ["pkg-config", "--cflags", "--libs", "libpipewire-0.3"], text=True))
        subprocess.run(["cc", "-std=gnu17", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(HERE / "kara-pipewire-feed.c"), "-o", str(cls.feeder), *flags], check=True)

    def test_blackbox_separate_taps_keep_the_running_output_clock(self):
        from _kara_acoustic_capture import AcousticTurn
        from _kara_blackbox import BlackBox, events, wave_layout
        from unittest.mock import patch
        with self.graph(256) as (env, root), self.player(env, waveform()) as player:
            witness_node = "kara-isolated-witness-fixture"
            with self.player(env, waveform(), node=witness_node, fault="driver-group", connect=False):
                def output_driver():
                    data = json.loads(subprocess.check_output(["pw-dump"], env=env, timeout=3))
                    sink = next(node for node in data if node.get("info", {}).get("props", {}).get("node.name") == SINK)
                    self.assertEqual(sink["info"]["state"], "running")
                    return sink["info"]["props"].get("node.driver-id", sink["id"])

                before = output_driver()
                recorder = BlackBox(root / "blackbox")
                recorder.start(consent=True, voice=True, tracks=["monitor", "witness"])
                session = recorder.voice_session()
                turn = AcousticTurn(recorder, session, {"turn": "0123456789abcdef", "player": "Fixture",
                                                      "song": "Synthétique"}, SINK, helper=self.helper,
                                    witness=witness_node, channel="MONO")
                with patch.dict(os.environ, env):
                    turn.start()
                    player.send_signal(signal.SIGUSR1)
                    sleep(2)
                    self.assertEqual(output_driver(), before)
                    turn.stop()
                    for thread in turn.threads:
                        thread.join(timeout=4)
                        self.assertFalse(thread.is_alive())
                recorder.emit("party", "turn-end", turn="0123456789abcdef")
                recorder.stop(wait=True)
                folder = root / "blackbox" / session
                manifest = json.loads((folder / "manifest.json").read_text())
                capture = manifest["capture"]["acoustic"]
                tracks = capture["turns"]["0123456789abcdef"]
                self.assertFalse(capture["cross_device_calibrated"])
                for name, channels in (("monitor", 2), ("witness", 1)):
                    self.assertEqual(tracks[name]["coverage"], "recorded", tracks)
                    self.assertGreater(tracks[name]["frames"], 4800)
                    track = next((folder / "audio").glob(f"*-{name}-*.wav"))
                    rate, actual, frames, width, _ = wave_layout(track)
                    self.assertEqual((rate, actual, width), (48000, channels, 4))
                    self.assertEqual(frames, tracks[name]["frames"])
                rows = [row for row in events(folder / "events.jsonl") if row["event"] == "pcm"]
                self.assertTrue(all(row["data"]["clock"]["quantum"] == 256 for row in rows))

    @contextmanager
    def graph(self, quantum):
        with tempfile.TemporaryDirectory(prefix="kara-v5-pw-") as directory:
            root = Path(directory)
            config = root / "test.conf"
            config.write_text(CONFIG.replace("QUANTUM", str(quantum)))
            env = {**os.environ, "PIPEWIRE_RUNTIME_DIR": directory, "PIPEWIRE_REMOTE": "pipewire-0"}
            with subprocess.Popen(["pipewire", "-c", str(config)], env=env, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.PIPE) as server:
                try:
                    deadline = monotonic() + 4
                    while not (root / "pipewire-0").exists():
                        if server.poll() is not None or monotonic() > deadline:
                            self.fail("isolated PipeWire did not start")
                        sleep(.02)
                    yield env, root
                finally:
                    end_process(server)

    @contextmanager
    def player(self, env, signal_values, *, fault="none", node=NODE, connect=True):
        # Synthetic fixtures are held in an anonymous RAM fd. The bench and
        # test produce no audio files, even for the deliberately broken graph.
        memory = os.memfd_create("v5-generated")
        try:
            os.write(memory, signal_values.tobytes())
            os.lseek(memory, 0, os.SEEK_SET)
            with subprocess.Popen([str(self.feeder), node, str(signal_values.shape[1]), fault],
                                  stdin=memory, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) as player:
                try:
                    for channel in (("left", "right") if connect else ()):
                        port = "FL" if channel == "left" else "FR"
                        deadline = monotonic() + 4
                        while True:
                            linked = subprocess.run(["pw-link", f"{node}:playback-{channel}", f"{SINK}:playback_{port}"],
                                                    env=env, capture_output=True, timeout=2)
                            if not linked.returncode:
                                break
                            if player.poll() is not None or monotonic() > deadline:
                                self.fail(f"fixture did not link: {linked.stderr!r}")
                            sleep(.02)
                    yield player
                finally:
                    end_process(player)
        finally:
            os.close(memory)

    def campaign(self, env, player, stage, *, control=None, diagnostic=None, begin_delay=0):
        if begin_delay:
            sleep(begin_delay)
        opts = ["--control", str(control)] if control else []
        if diagnostic:
            opts.extend(("--diagnostic", str(diagnostic)))
        command = [sys.executable, str(BIN / "_kara_pipewire_latency.py"), "--mode", stage,
                   "--helper", str(self.helper), "--mic", NODE, "--mic-channel", "MONO", "--sink", SINK, *opts]
        with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE) as process:
            log = bytearray()
            output = bytearray()
            try:
                with selectors.DefaultSelector() as selector:
                    selector.register(process.stdout, selectors.EVENT_READ, output)
                    selector.register(process.stderr, selectors.EVENT_READ, log)
                    deadline = monotonic() + (66 if stage == "measure" else 27)
                    while selector.get_map():
                        wait = deadline - monotonic()
                        if wait <= 0:
                            self.fail(f"graph campaign stalled: {log.decode()}")
                        for entry, _ in selector.select(wait):
                            data = os.read(entry.fd, 8192)
                            if not data:
                                selector.unregister(entry.fileobj)
                                continue
                            old = log.count(b"MAINTENANT")
                            entry.data.extend(data)
                            if log.count(b"MAINTENANT") > old:
                                player.send_signal(signal.SIGUSR1)
                return process.wait(timeout=2), json.loads(output), log.decode()
            finally:
                end_process(process)

    def test_real_monitor_deformed_weak_echo_and_delayed_capture_start(self):
        with self.graph(256) as (env, root):
            with self.player(env, waveform(control=True, reflection=True), connect=False) as producer:
                code, control, log = self.campaign(env, producer, "control")
            self.assertEqual(code, 0, log)
            saved = root / "control.json"
            saved.write_text(json.dumps(control))
            for row in control["trials"]:
                self.assertEqual(row["routes"], [bench.ROUTE_NONE])
                self.assertIsNone(row["graph_interval"])
            with self.player(env, waveform(weak=True, distortion=True, reflection=True)) as producer:
                code, report, log = self.campaign(env, producer, "diagnostic", control=saved, begin_delay=.37)
                self.assertEqual(code, 0, log)
                self.assertEqual(report["summary"]["status"], "pipewire_diagnostic_ready")
                self.assertEqual(report["timing"]["observed_quantum_frames"], [256, 256])
                for row in report["trials"]:
                    self.check_measured_row(row, 256)

                diagnostic = root / "diagnostic.json"
                diagnostic.write_text(json.dumps(report))
                code, complete, log = self.campaign(env, producer, "measure", control=saved, diagnostic=diagnostic)
                self.assertEqual(code, 0, log)
                summary = complete["summary"]
                self.assertEqual((summary["status"], summary["attempts"]), ("pipewire_intervals", 10))
                self.assertEqual(summary["graph_interval"]["n"], 10)
                self.assertAlmostEqual(summary["acoustic_interval"]["median_ms"], 231.75, delta=.15)
                self.assertIsNone(summary["physical_output_delay"])

    def check_measured_row(self, row, quantum):
        # This null sink is the driver. Its monitor is visible to the
        # witness on the next cycle after playback input. This offset is
        # intentionally neither hidden by a tolerance nor subtracted in V5.
        visibility = quantum / bench.FRAMES_PER_MS
        self.assertEqual(row["kara_output_evidence"], "same_syllable_on_exclusive_kara_route")
        for metric, delay in (("graph_interval", 174.25 + visibility),
                              ("monitor_to_witness_graph", 57.5 - visibility),
                              ("acoustic_interval", 231.75)):
            measured = row[metric]
            self.assertIsNotNone(measured, (metric, row))
            self.assertAlmostEqual(measured["ms"], delay, delta=.15)
            self.assertLessEqual(measured["range_ms"][0], delay)
            self.assertGreaterEqual(measured["range_ms"][1], delay)
        self.assertIsNone(row["physical_output_delay"])

    def test_buffer_delivery_stall_and_larger_graph_quantum_keep_sample_alignment(self):
        with self.graph(512) as (env, _):
            with self.player(env, waveform(weak=True, distortion=True, reflection=True)) as player:
                with subprocess.Popen([str(self.helper), NODE, SINK, "MONO"], env=env,
                                      stdout=subprocess.PIPE, bufsize=0) as native:
                    capture = bench.GraphCapture(native.stdout.fileno())
                    try:
                        capture.read_frames(bench.RATE)
                        player.send_signal(signal.SIGUSR1)
                        # Build up several complete cycles in the native
                        # ring/pipe, with no change to their source positions.
                        sleep(.075)
                        start, samples, routes, present = capture.read_frames(2 * bench.RATE)
                        self.assertEqual(capture.clock_report()["observed_quantum_frames"], [512, 512])
                    finally:
                        capture.close()
                        end_process(native)
        row = bench.inspect_trial(start, samples, routes, [.00025, 0, 0], "diagnostic", 260, present)
        self.check_measured_row(row, 512)

    def test_preflight_pcm_when_running_sink_joins_a_different_driver(self):
        silence = np.zeros((3, 256), dtype="=f4")
        player = "kara-v51-silent-player"
        with self.graph(256) as (env, _):
            with self.player(env, silence, node=player), self.player(
                    env, silence, connect=False, fault="driver-group"):
                deadline = monotonic() + 3
                while True:
                    nodes = {obj["info"]["props"]["node.name"]: obj for obj in json.loads(
                        subprocess.check_output(["pw-dump"], env=env, timeout=2))
                        if obj["type"] == "PipeWire:Interface:Node"}
                    sink = nodes[SINK]
                    if sink["info"]["state"] == "running" and NODE in nodes:
                        break
                    self.assertLess(monotonic(), deadline, "fixture sink did not start")
                    sleep(.02)
                self.assertEqual(sink["info"]["props"].get("node.driver-id", sink["id"]), sink["id"])
                command = [str(self.helper), NODE, SINK, "MONO", "--preflight-player", player]
                with subprocess.Popen(command, env=env, stdout=subprocess.PIPE, bufsize=0) as native:
                    capture = bench.GraphCapture(native.stdout.fileno())
                    try:
                        _, _, routes, present = capture.read_frames(bench.RATE)
                        self.assertEqual(capture.driver, nodes["kara-test-driver"]["id"])
                        self.assertEqual((routes, present), ({bench.ROUTE_MIXED}, 7))
                    finally:
                        capture.close()
                        end_process(native)
                    self.assertEqual(native.returncode, 0)

    def test_other_quantum_and_missing_samples_invalidate_campaign(self):
        with self.graph(512) as (env, _):
            with self.player(env, waveform(control=True), fault="gap") as producer:
                code, report, log = self.campaign(env, producer, "control")
        self.assertEqual(code, 1, log)
        self.assertEqual(report["summary"]["status"], "inconclusive")
        self.assertIn("pcm_missing_or_invalid", log)
        self.assertIsNone(report["timing"])

    def test_async_link_has_no_absolute_result(self):
        with self.graph(128) as (env, _):
            with self.player(env, waveform(control=True), fault="async") as producer:
                code, report, log = self.campaign(env, producer, "control")
        self.assertEqual(code, 1, log)
        self.assertIn("tap_not_synchronous", log)
        self.assertIsNone(report["summary"]["graph_interval"])


if __name__ == "__main__":
    unittest.main()
