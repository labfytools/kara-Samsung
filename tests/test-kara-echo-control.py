#!/usr/bin/env python3
"""Deterministic feedback and vocal-envelope checks using only generated PCM."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_micro_test import monitor_peak, shape_monitor

RATE = 48000
ATTENUATION = 10 ** (-9 / 20)


def syllable(seconds, amplitude):
    t = np.arange(round(RATE * seconds)) / RATE
    envelope = np.minimum(np.minimum(t / .009, (seconds - t) / .018), 1)
    harmonics = (np.sin(2 * np.pi * 227 * t)
                 + .27 * np.sin(2 * np.pi * 454 * t + .13)
                 + .13 * np.sin(2 * np.pi * 1135 * t + .31))
    return (amplitude * envelope * harmonics).astype(np.float32)


def feedback(source, block_ms, guard, delay, gain):
    """Feed actual rendered PCM back after a causal, possibly varying delay."""
    frames = block_ms * RATE // 1000
    rendered = np.zeros_like(source)
    peak = monitor_peak(guard)
    for block, start in enumerate(range(0, source.size, frames)):
        end = min(start + frames, source.size)
        previous = np.arange(start, end) - delay(block)
        microphone = source[start:end].copy()
        present = previous >= 0
        microphone[present] += gain(block) * rendered[previous[present]]
        # The loop crosses int16 at both ends. No recorded fixture is used.
        pcm = (np.clip(microphone, -1, 32767 / 32768) * 32768).astype("<i2")
        output = shape_monitor(pcm.astype(np.float32) / 32768, peak)
        rendered[start:end] = (output * 32767).astype("<i2") / 32767
    return rendered


def rms(samples):
    return float(np.sqrt(np.mean(samples ** 2)))


class EchoTests(unittest.TestCase):
    def test_silence_and_direct_voice_shape(self):
        disabled, enabled = monitor_peak(False), monitor_peak(True)
        for voice, seconds, level in [("weak speech", .065, .002),
                                      ("loud voice", .065, .8),
                                      ("sustained singing", .8, .23)]:
            with self.subTest(voice=voice):
                dry = syllable(seconds, level)
                before = np.zeros(137, dtype=np.float32)
                samples = np.concatenate((before, dry, before))
                original = shape_monitor(samples, disabled)
                protected = shape_monitor(samples, enabled)
                np.testing.assert_array_equal(original, np.tanh(samples * 2.0) * .82)
                np.testing.assert_allclose(protected, original * ATTENUATION, atol=3e-8, rtol=1e-6)
                self.assertFalse(protected[:137].any() or protected[-137:].any())
                # Check both ends of the syllable survive transport quantization.
                pcm = (protected * 32767).astype("<i2")
                self.assertTrue(pcm[137:137+int(.004 * RATE)].any())
                self.assertTrue(pcm[-137-int(.004 * RATE):-137].any())
                for block_frames in (480, 960):
                    blocks = (shape_monitor(samples[i:i+block_frames], enabled)
                              for i in range(0, len(samples), block_frames))
                    np.testing.assert_array_equal(np.concatenate(list(blocks)), protected)

    def test_successive_reinjections_with_several_delays(self):
        source = np.zeros(RATE * 2, dtype=np.float32)
        dry = syllable(.065, .19)
        start = RATE // 20
        source[start:start+len(dry)] = dry
        for block_ms in (10, 20):
            for delay_ms in (95, 210, 375):
                with self.subTest(block_ms=block_ms, delay_ms=delay_ms):
                    period = delay_ms * RATE // 1000
                    off, on = [feedback(source, block_ms, enabled,
                                        lambda _: period, lambda _: .6) for enabled in (False, True)]
                    levels = []
                    for generation in range(4):
                        idx = start + generation * period
                        window = slice(idx, idx+len(dry))
                        levels.append(20 * np.log10(rms(on[window]) / rms(off[window])))
                    self.assertAlmostEqual(levels[0], -9, delta=.09)
                    for generation in range(1, 4):
                        self.assertLess(levels[generation], -9 - 8 * generation)

    def test_decreasing_gain_and_variable_delay(self):
        source = np.zeros(RATE * 2, dtype=np.float32)
        dry = syllable(.045, .24)
        onset = RATE // 20
        source[onset:onset+len(dry)] = dry
        dry_end = onset + len(dry)

        for name, delay, gain in [
            ("decreasing", lambda _: round(.18 * RATE), lambda i: .86 * .80 ** (i // 18)),
            ("variable", lambda i: round((.17 + .016 * np.sin(i * .12)) * RATE), lambda _: .90),
        ]:
            with self.subTest(name=name):
                off = feedback(source, 10, False, delay, gain)
                on = feedback(source, 10, True, delay, gain)
                # Normalize by first-pass voice so lowering the entire result
                # without reducing repeats would not satisfy the test.
                on_ratio = rms(on[dry_end:]) / rms(on[:dry_end])
                off_ratio = rms(off[dry_end:]) / rms(off[:dry_end])
                self.assertGreater(off_ratio, .1)
                self.assertLess(on_ratio, off_ratio * .41)
                self.assertLess(rms(on[RATE:]), rms(on[dry_end:RATE]) * .23)


if __name__ == "__main__":
    unittest.main()
