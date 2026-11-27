#!/usr/bin/python3 -B
"""Exercise the turn mixer, capture clock and guide gate without an audio device."""
from pathlib import Path
import socket
import struct
import subprocess
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import gi
import numpy as np

gi.require_version("Gtk", "4.0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_micro_test import monitor_peak, shape_monitor
from _kara_room_audio import RoomAudio
from _kara_stem_playback import GAIN_RAMP, Gst, StemPlayback, TURN_GUIDE_GAIN, TURN_MUSIC_GAIN
from _kara_stems import aligned_pair
from _kara_voice import transcribe_prepared_job

RATE = 48000


class MixTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        root = Path(cls.enterClassContext(TemporaryDirectory()))
        time = np.arange(2 * RATE) / RATE
        cls.signal = .40 * np.sin(2 * np.pi * 400 * time)
        cls.stems = (root / "instrumental.flac", root / "vocals.flac")
        for channel, path in enumerate(cls.stems):
            # Orthogonal stereo planes make side effects between the two
            # faders directly measurable in the real pipeline output.
            stereo = np.zeros((len(time), 2), dtype="<i2")
            stereo[:, channel] = np.rint(cls.signal * 32767).astype("<i2")
            with wave.open(str(path.with_suffix(".wav")), "wb") as file:
                file.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
                file.writeframes(stereo.tobytes())
            subprocess.run(("ffmpeg", "-v", "error", "-nostdin", "-i",
                            str(path.with_suffix(".wav")), str(path)), check=True, timeout=15)

    def test_two_decoders_share_time_and_have_independent_gains(self):
        self.assertTrue(aligned_pair(self.stems))
        measured = []
        for music, guide in ((100, 0), (0, 100), (100, 100)):
            effect = StemPlayback()
            observed = {}
            try:
                effect.set_turn_mix(music, guide, 100)
                effect.observe_output(lambda rate, channels, block, encoding, clock:
                                      observed.update({clock["pts_ns"]: (rate, channels, block, encoding)}))
                effect._set_pipeline(self.stems, None)
                self.assertNotEqual(effect.pipeline.set_state(Gst.State.PLAYING), Gst.StateChangeReturn.FAILURE)
                effect.playing = True
                sink = effect.pipeline.get_by_name("out")
                buffers = []
                for _ in range(12):
                    sample = sink.emit("try-pull-sample", Gst.SECOND)
                    self.assertIsNotNone(sample)
                    buffer = sample.get_buffer()
                    pcm = buffer.extract_dup(0, buffer.get_size())
                    self.assertEqual(observed[buffer.pts], (RATE, 2, pcm, "f32le"))
                    buffers.append(np.frombuffer(pcm, dtype="<f4").reshape(-1, 2))
                signal = np.concatenate(buffers)
                measured.append(np.sqrt(np.mean(signal ** 2, axis=0)))
                # Exercise live timestamped control-point installation on the
                # same running pipeline, whose position must stay monotonic.
                ok, before = effect.pipeline.query_position(Gst.Format.TIME)
                self.assertTrue(ok)
                effect.set_turn_mix(37, 63, 90)
                ok, after = effect.pipeline.query_position(Gst.Format.TIME)
                self.assertTrue(ok)
                self.assertGreaterEqual(after, before)
                start, initial, target = effect.ramps["music"]
                self.assertGreaterEqual(start, before)
                self.assertAlmostEqual(initial, min(music, 100) / 100 * TURN_MUSIC_GAIN)
                self.assertAlmostEqual(target, .37 * TURN_MUSIC_GAIN)
                for _ in range(55):
                    sample = sink.emit("try-pull-sample", Gst.SECOND)
                    self.assertIsNotNone(sample)
                    buffer = sample.get_buffer()
                    if buffer.pts > start + GAIN_RAMP + 3 * Gst.SECOND // 10:
                        break
                signal = np.frombuffer(buffer.extract_dup(0, buffer.get_size()), dtype="<f4").reshape(-1, 2)
                np.testing.assert_allclose(np.sqrt(np.mean(signal ** 2, axis=0)),
                                           .40 / np.sqrt(2) * np.array((.37 * TURN_MUSIC_GAIN, .63 * TURN_GUIDE_GAIN)),
                                           atol=.003)
            finally:
                effect.disable()
        expected = .40 / np.sqrt(2) * np.array((TURN_MUSIC_GAIN, TURN_GUIDE_GAIN))
        np.testing.assert_allclose(measured[0], (expected[0], 0), atol=.002)
        np.testing.assert_allclose(measured[1], (0, expected[1]), atol=.002)
        np.testing.assert_allclose(measured[2], expected, atol=.002)

    def test_artist_only_never_reaches_decoder_when_guide_is_audible(self):
        for ratio in (.1, .5, 1.):
            audio = self.signal[:RATE:3].astype(np.float32) * ratio
            with patch("_kara_voice.transcribe_player", side_effect=AssertionError("Guide credited to player")):
                self.assertEqual(transcribe_prepared_job(None, audio, {
                    "instrumental": True, "track_start": 0., "track_end": 1., "guide": str(self.stems[1]),
                    "language": "fr",
                }), ([], "guide"))


class ReturnTests(unittest.TestCase):
    def test_quiet_gain_increases_under_the_existing_peak_limit(self):
        quiet = np.array((-.012, .012), dtype=np.float32)
        limit = monitor_peak(True)
        old = shape_monitor(quiet, limit)
        boosted = shape_monitor(quiet, limit, 3.)
        np.testing.assert_array_less(2.9 * np.abs(old), np.abs(boosted))
        loud = shape_monitor(np.linspace(-1, 1, 1000), limit, 3.)
        self.assertLessEqual(float(np.max(np.abs(loud))), limit)
        self.assertLess(TURN_MUSIC_GAIN + TURN_GUIDE_GAIN + limit, 1.)

    def test_linear_room_fader_and_emergency_cut_have_independent_peak_budget(self):
        from _kara_monitor_profiles import ROOM_MUSIC_BUDGET

        room = RoomAudio(Mock())
        with patch.object(room, "_route_settings", return_value=("sink", "device", {
                "volume_percent": 73, "profile": "balanced"})):
            room.open_turn("owner", "Joueur", "tour", "Titre")
        self.addCleanup(room.close_turn)
        route = room.route
        samples = shape_monitor(np.full(480, .1, dtype=np.float32), monitor_peak(True))
        for _ in range(40):
            result = room.apply_return(route, samples)
        self.assertAlmostEqual(float(result[-1] / samples[-1]), .73, places=3)
        self.assertLess(ROOM_MUSIC_BUDGET + monitor_peak(True), 1.)
        room.mute_return()
        self.assertFalse(room.apply_return(route, samples).any())

if __name__ == "__main__":
    unittest.main()
