#!/usr/bin/env python3
"""Exercise the production recorder with isolated, synthetic PCM only."""
import json
from pathlib import Path
import struct
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox, atomic_json, cycle_id, digest, events, validate_recording
from _kara_blackbox_clock import browser_anchor


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


if __name__ == "__main__":
    unittest.main()
