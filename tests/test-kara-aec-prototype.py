#!/usr/bin/env python3
"""Exercise both actual AEC libraries using generated PCM only."""
import ctypes as ct
from ctypes.util import find_library
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _kara_aec_prototype as aec


class AnalyticControls(unittest.TestCase):
    def test_lowering_everything_does_not_improve_voice_to_residual(self):
        generator = np.random.default_rng(33)
        clean = generator.standard_normal(aec.FRAME)
        echo = generator.standard_normal(aec.FRAME)
        first = aec.voice_metrics(clean, clean + echo)
        reduced = aec.voice_metrics(clean, (clean + echo) * (10 ** (-9 / 20)))
        self.assertAlmostEqual(first["voice_si_sdr_db"], reduced["voice_si_sdr_db"])
        self.assertAlmostEqual(first["voice_gain_db"] - reduced["voice_gain_db"], 9)

    def test_wrong_reference_does_not_remove_accompaniment_from_capture(self):
        full = aec.open_loop(aec.CASES[0])
        partial = aec.open_loop(aec.VOICE_ONLY_REFERENCE)
        np.testing.assert_array_equal(full[0], partial[0])
        np.testing.assert_array_equal(full[1], partial[1])
        self.assertGreater(aec.power(full[2] - partial[2]), 1e-4)

    def test_acceptance_requires_voice_echo_and_frame_budget(self):
        def candidate(voice_gain, voice_sdr, early_echo, late_echo, over_budget=0):
            good_voice = {"voice_gain_db": voice_gain, "voice_si_sdr_db": voice_sdr}
            return {"cases": {"trial": {
                "voice": {"weak": good_voice}, "echo_reduction_db": early_echo,
                "late_echo_reduction_db": late_echo, "wall_over_10ms": over_budget,
            }}, "self_return": {"post_phrase_return_reduction_db": late_echo, "singing": good_voice}}

        self.assertTrue(all(aec.qualification(candidate(0, 18, 18, 18)).values()))
        self.assertFalse(any(aec.qualification(candidate(-9, 18, 18, 18)).values()))
        self.assertFalse(any(aec.qualification(candidate(0, 4, 18, 18)).values()))
        self.assertFalse(any(aec.qualification(candidate(0, 18, 18, 2)).values()))
        self.assertFalse(aec.qualification(candidate(0, 18, 18, 18, 1))["trial"])


class SpeexTests(unittest.TestCase):
    @unittest.skipUnless(find_library("speexdsp"), "SpeexDSP not installed")
    def test_has_actual_cancellation_on_a_fixed_echo(self):
        delay = aec.find_delay(aec.Speex)
        result = aec.inspect(aec.CASES[0], aec.Speex, delay)
        self.assertEqual(result["frames_processed"], 1200)
        self.assertGreater(result["echo_reduction_db"], 10)
        self.assertGreater(result["voice"]["speech"]["voice_gain_db"], -3)
        with self.assertRaises(ValueError):
            aec.Speex(3)


class WebRtcTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not shutil.which("pkg-config") or not shutil.which("c++"):
            raise unittest.SkipTest("WebRTC prototype toolchain unavailable")
        pkg = subprocess.run(["pkg-config", "--exists", "webrtc-audio-processing-1"],
                             check=False, timeout=5)
        if pkg.returncode:
            raise unittest.SkipTest("webrtc-audio-processing-1 development files unavailable")
        directory = cls.enterClassContext(tempfile.TemporaryDirectory(prefix="kara-aec-test-"))
        cls.artifact = Path(directory) / "aec.so"
        aec.build_webrtc(cls.artifact)

    def factory(self, channels):
        return aec.WebRtc(self.artifact, channels)

    def test_real_fixed_echo_reduction_and_no_missing_frames(self):
        delay = aec.find_delay(self.factory)
        self.assertLess(delay, aec.FRAME * 2)
        result = aec.inspect(aec.CASES[0], self.factory, delay)
        self.assertGreater(result["echo_reduction_db"], 10)
        self.assertEqual(result["frames_processed"], 1200)
        self.assertTrue(np.isfinite(result["voice"]["singing"]["voice_si_sdr_db"]))

    def test_c_abi_rejects_invalid_sizes_and_accepts_stereo(self):
        engine = self.factory(2)
        self.addCleanup(engine.close)
        data = np.zeros(aec.FRAME, dtype=np.int16)
        far = np.zeros(aec.FRAME * 2, dtype=np.int16)
        out = data.copy()
        self.assertFalse(engine.lib.kara_aec_create(3))
        self.assertNotEqual(engine.lib.kara_aec_step(
            engine.state, far, far.size, data, data.size - 1, out, out.size, 25), 0)
        self.assertNotEqual(engine.lib.kara_aec_step(
            ct.c_void_p(), far, far.size, data, data.size, out, out.size, 25), 0)
        with self.assertRaises(RuntimeError):
            engine.step(far, data, out, -1)
        engine.step(far, data, out, 25)


if __name__ == "__main__":
    unittest.main()
