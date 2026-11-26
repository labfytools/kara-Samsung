#!/usr/bin/env python3
"""Exercise the production recorder with isolated, synthetic PCM only."""
import json
from hashlib import sha256
import os
from pathlib import Path
import socket
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox, atomic_json, cycle_id, digest, events, validate_recording
from _kara_blackbox_clock import browser_anchor
from _kara_blackbox_turbo import receive, submit
from _kara_blackbox_trials import annotate, packet_simulation, simulate_pcm, simulation_options


class RecordingTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.box = BlackBox(Path(temporary.name) / "blackbox")
        self.turn = {"turn": "a" * 16, "player": "Pseudo privé fixture", "song": "Synthétique"}

    def test_cycle_and_track_identity_with_shared_session_and_late_packet(self):
        self.box.start(consent=True, voice=True)
        session = self.box.voice_session()
        self.assertIsNone(self.box.voice_session("witness"))
        self.box.emit("party", "turn-start", **self.turn)
        self.box.pcm("wifi", 48000, 1, bytes(1920), **self.turn)
        self.box.pcm("wifi", 48000, 1, bytes(1920), **self.turn, recording_session="autre-session")
        self.box.emit("party", "turn-end", **self.turn)
        self.box.pcm("wifi", 48000, 1, bytes(1920), **self.turn)
        self.box.stop(wait=True)
        folder = self.box.root / session
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertTrue(validate_recording(folder, manifest)["valid"])
        cycle = cycle_id(session, self.turn["turn"])
        self.assertEqual(manifest["capture"]["state"], "incomplete")
        self.assertEqual(manifest["cycles"][cycle]["streams"]["MIC_RECEIVED"]["id"], cycle + "/wifi")
        self.assertEqual(manifest["cycles"][cycle]["streams"]["OUTPUT_MONITOR"]["state"], "absent")
        self.assertEqual(manifest["cycles"][cycle]["streams"]["ACOUSTIC_WITNESS"]["state"], "disabled")
        log = folder / "events.jsonl"
        rows = [row for row in events(log) if row["cycle"] == cycle]
        self.assertEqual([row["event"] for row in rows], ["turn-start", "pcm", "turn-end"])
        self.assertEqual(len({row["player_id"] for row in rows}), 1)
        self.assertNotIn(self.turn["player"], log.read_text())
        rows[1]["data"]["stream"] = "autre-flux"
        log.write_text("".join(json.dumps(row) + "\n" for row in rows))
        manifest["files"]["events.jsonl"] = {"bytes": log.stat().st_size, "sha256": digest(log)}
        with self.assertRaisesRegex(ValueError, "Identité du flux"):
            validate_recording(folder, manifest)

    def test_marker_is_human_and_scoped_to_turn(self):
        self.box.start(consent=True)
        self.box.mark("off-key", "annotation fixture", **self.turn, position=1.5)
        with self.assertRaises(ValueError):
            self.box.mark("typed-script")
        self.box.stop(wait=True)
        folder = self.box.root / self.box.status()["latest"]["session"]
        row = next(row for row in events(folder / "events.jsonl") if row["event"] == "marker")
        self.assertEqual(row["cycle"], cycle_id(folder.name, self.turn["turn"]))
        self.assertEqual(row["position"], 1.5)
        self.assertEqual(row["data"], {"kind": "off-key", "note": "annotation fixture", "provenance": "human"})

    def test_interrupted_draft_keeps_valid_pcm_and_later_marker(self):
        root = self.box.root
        root.mkdir(mode=0o700)
        session = "session-20260101T000000-0123456789ab"
        folder = root / session
        audio = folder / "audio"
        audio.mkdir(parents=True, mode=0o700)
        cycle = cycle_id(session, self.turn["turn"])
        atomic_json(folder / "manifest.json", {"format": 3, "status": "recording", "session": session,
                                               "capture": {"voice": True, "tracks": ["wifi"]}})
        track = f"{self.turn['turn']}-wifi-0000.wav"
        # The provisional mono PCM header still advertises no samples.
        header = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36, b"WAVE", b"fmt ", 16,
                             1, 1, 48000, 96000, 2, 16, b"data", 0)
        (audio / track).with_suffix(".part").write_bytes(header + bytes(1920))
        self.assertFalse((audio / track).exists())
        with (folder / "events.jsonl").open("w") as log:
            for ident, event in [(1, "pcm"), (2, "pcm"), (3, "marker")]:
                data = {"track": "wifi", "file": f"audio/{track}", "stream": f"{cycle}/wifi",
                        "rate": 48000, "channels": 1, "sample": (ident - 1) * 960, "frames": 960}
                log.write(json.dumps({"id": ident, "session": session, "cycle": cycle, "turn": self.turn["turn"],
                                      "component": "audio", "event": event, "monotonic_ns": ident,
                                      "data": data if event == "pcm" else {}}) + "\n")
            log.write('{"id":')
        self.box.recover()
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertEqual(validate_recording(folder, manifest)["status"], "incomplete")
        self.assertEqual([row["id"] for row in events(folder / "events.jsonl")], [1, 3])
        self.assertEqual(self.box.status()["latest"]["unrecoverable_pcm_events"], 1)

    def test_asymmetric_clock_exchange_preserves_interval(self):
        # Server clock = browser clock + 1 s. Network delays are 2 and
        # 12 ms, so the midpoint is biased by 5 ms even with perfect clocks.
        payload = dict(received_ms=202., context_begin_ms=202.1, context_end_ms=202.3,
                       sent_ms=203., context_frame=9600)
        stamp = browser_anchor(1_200_000_000, 1_215_000_000, payload)
        self.assertEqual((stamp["offset_min_ns"], stamp["offset_max_ns"]), (998_000_000, 1_012_000_000))
        self.assertEqual(stamp["offset_uncertainty_ns"], 7_000_000)
        self.assertIsNone(stamp["physical_bound_ns"])
        self.assertIsNone(browser_anchor(1_200_000_000, 1_200_000_001, payload))

    def test_multitrack_report_requires_contiguous_coincident_evidence(self):
        self.box.queue_size = 2048
        self.box.start(consent=True, voice=True, tracks=["wifi", "monitor"])
        session = self.box.voice_session()
        connection = "c" * 32
        base = 10_000_000_000
        for label, shifted, missing_track in [("a", 0, False), ("b", 8_000_000_000, False), ("d", 0, True)]:
            turn = {**self.turn, "turn": label * 16}
            self.box.emit("party", "turn-start", **turn)
            for packet in range(300):
                first = packet * 960
                stamp = base + packet * 20_000_000
                self.box.pcm("wifi", 48000, 1, bytes(1920), **turn, connection=connection, sequence=packet,
                             packet_timing={"frame_end": first + 960})
                if not missing_track:
                    clock = dict(frame=first, monotonic_ns=stamp + shifted, xrun=0, quantum=960, route=1,
                                 epoch=1, driver=7, present=6, spa_headers=6)
                    self.box.pcm("monitor", 48000, 2, bytes(7680), **turn, recording_session=session, clock=clock)
                if packet in (30, 270):
                    self.box.emit("clock", "mobile-anchor", **turn, rate=48000, connection=connection,
                                  context_frame=first, renderer_frame_min_ns=stamp - 1000,
                                  renderer_frame_max_ns=stamp + 1000)
            self.box.emit("party", "turn-end", **turn)
        self.box.stop(wait=True)
        folder = self.box.root / session
        manifest = json.loads((folder / "manifest.json").read_text())
        self.assertTrue(validate_recording(folder, manifest)["valid"])
        report = json.loads((folder / "analysis.json").read_text())
        cycles = report["cycles"]
        shared = cycles[cycle_id(session, "a" * 16)]
        self.assertEqual(shared["aec_state"], "software_overlap")
        self.assertIsNone(shared["physical_alignment_bound_ns"])
        self.assertEqual(shared["overlap"]["monitor"]["duration_ns"], 4_799_998_000)
        for label in ("b", "d"):
            other = cycles[cycle_id(session, label * 16)]
            self.assertEqual(other["overlap"], {})
            self.assertEqual(other["aec_state"], "missing_timing_or_tracks")
        cli = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "bin/kara-blackbox"),
               "--root", str(self.box.root)]
        exported = subprocess.check_output([*cli, "export", session], text=True)
        self.assertNotIn(session, exported)
        self.assertNotIn(self.turn["song"], exported)
        self.assertEqual(json.loads(exported)["aec_states"], {"software_overlap": 1, "missing_timing_or_tracks": 2})
        first = json.loads(subprocess.check_output(
            [*cli, "replay-timeline", session, "--cycle", cycle_id(session, "b" * 16), "--limit", "1"], text=True))
        self.assertEqual(first["event"], "turn-start")

    def test_exact_turbo_input_is_bound_to_decode_call(self):
        self.box.start(consent=True, voice=True, tracks=["turbo"])
        session = self.box.voice_session()
        block = struct.pack("<h", 756) * 19200
        self.box.emit("party", "turn-start", **self.turn)
        self.box.emit("turbo", "window", **self.turn, window=7)
        rx, tx = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        with rx, tx:
            rx.setblocking(False)
            self.assertTrue(submit(tx, 7, 1, 5, block))

            def process(header, fd, size):
                window, call, offset = header
                self.box.pcm("turbo", 16000, 1, os.pread(fd, size, 0), **self.turn, recording_session=session,
                             decode={"window": window, "call": call, "window_offset": offset,
                                     "first_frame": 0, "total_frames": size // 2})

            receive(rx, process)
        self.box.emit("scoring", "line-decision", **self.turn, window=7,
                      decoder={"decodes": [{"call": 1, "input_frames": len(block) // 2,
                                            "input_sha256": sha256(block).hexdigest()}]})
        self.box.emit("party", "turn-end", **self.turn)
        self.box.stop(wait=True)
        folder = self.box.root / session
        window_id = next(row["id"] for row in events(folder / "events.jsonl") if row["event"] == "window")
        result = self.box.root.parent / "bout.wav"
        cli = [sys.executable, "-B", str(Path(__file__).resolve().parents[1] / "bin/kara-blackbox"),
               "--root", str(self.box.root), "extract-turbo", session,
               "--id", str(window_id), "--call", "1", "--output", str(result)]
        report = json.loads(subprocess.check_output(cli, text=True))
        self.assertEqual(report["sha256"], sha256(block).hexdigest())
        with wave.open(str(result)) as wav:
            self.assertEqual(wav.readframes(wav.getnframes()), block)
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)

    def test_simulation_and_human_revisions_do_not_modify_capture(self):
        import numpy as np

        with self.assertRaises(ValueError):
            self.box.start(consent=True, mode="benchmark")
        self.box.start(consent=True, voice=True, mode="calibration")
        self.assertIsNone(self.box.voice_session("turbo"))
        session = self.box.voice_session()
        self.box.emit("party", "turn-start", **self.turn)
        for sequence in range(6):
            self.box.pcm("wifi", 48000, 1, bytes(1920), sequence=sequence, **self.turn)
        self.box.emit("party", "turn-end", **self.turn)
        self.box.stop(wait=True)
        folder = self.box.root / session
        checksum = digest(folder / "events.jsonl")
        cycle = cycle_id(session, self.turn["turn"])
        options = simulation_options({"drop_every": 2, "jitter_ms": 1, "delay_ms": 2, "reconnect": 0})
        rows = list(packet_simulation(folder, cycle, options, 29, None, None, 6))
        self.assertEqual(rows, list(packet_simulation(folder, cycle, options, 29, None, None, 6)))
        self.assertEqual(rows[-1], {"packets": 6, "delivered": 3, "lost": 3})
        self.assertIsNone(rows[1]["receive_ns"])
        out = self.box.root.parent / "annotations.jsonl"
        first = annotate(self.box.root, out, session, cycle, 1, 2, "sung", "", None)
        revised = annotate(self.box.root, out, session, cycle, 1, 3, "off-key", "", first["id"])
        self.assertEqual((revised["revision"], revised["source"]), (2, "human"))
        self.assertEqual(digest(folder / "events.jsonl"), checksum)
        original = np.ones(16, dtype=np.float32) * .8
        rendered = np.ones((16, 2), dtype=np.float32) * .7
        overlay = simulation_options({"gain": 0, "echo_gain": 1, "clip_peak": .5})
        np.testing.assert_allclose(simulate_pcm(original, rendered, overlay, 48000), .5)
        np.testing.assert_allclose(original, .8)


if __name__ == "__main__":
    unittest.main()
