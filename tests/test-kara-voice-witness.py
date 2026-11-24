#!/usr/bin/env python3
"""Synthetic voice and CLI campaigns through the acoustic witness."""

from contextlib import redirect_stderr, redirect_stdout
from importlib.util import module_from_spec, spec_from_file_location
from io import StringIO
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bin"))
import _kara_voice_latency_witness as witness

fixture_spec = spec_from_file_location("witness_fixtures", HERE / "test-kara-voice-latency.py")
fixtures = module_from_spec(fixture_spec)
fixture_spec.loader.exec_module(fixtures)


def syllable(duration_ms, seed=9):
    rng = np.random.default_rng(seed)
    t = np.arange(round(duration_ms * witness.RATE / 1000)) / witness.RATE
    u = t / (duration_ms / 1000)
    pitch = 150 + 36 * np.sin(u * 3.8) - 20 * u + 25 * (seed % 3)
    phase = np.cumsum(pitch) * (2 * np.pi / witness.RATE)
    vowel = np.zeros(len(t))
    # Moving formants, voiced nucleus, and distinct bursts on either side.
    for harmonic in range(1, 33):
        hz = harmonic * pitch
        weight = (.85 * np.exp(-((hz - (710 + 240 * u)) / 230) ** 2)
                  + .45 * np.exp(-((hz - (1700 - 430 * u)) / 310) ** 2)
                  + .24 * np.exp(-((hz - 2750) / 540) ** 2))
        vowel += weight * np.sin(harmonic * phase + rng.uniform(0, 2 * np.pi))
    shape = np.maximum(0, np.sin(np.pi * np.clip((u - .10) / .81, 0, 1))) ** .85
    fricative = np.diff(rng.normal(0, 1, len(t) + 1))
    puffs = (.23 * np.exp(-((u - .052) / .037) ** 2)
             + .25 * np.exp(-((u - .943) / .035) ** 2))
    sound = shape * vowel + puffs * fricative
    return .47 * sound / np.max(np.abs(sound))


def trial(duration=208, onset=550, returns=(), *, seed=9, noise=18, extra=()):
    rng = np.random.default_rng(seed + 80)
    recording = rng.normal(0, noise / 32768, witness.TRIAL_FRAMES)
    direct = syllable(duration, seed)

    def add(offset, wave):
        index = round(offset * witness.RATE / 1000)
        recording[index:index + len(wave)] += wave

    add(onset, direct)
    for delay, gain, processed in returns:
        returned = direct
        if processed:
            # Fixed coloration, a small nonlinear compressor and a
            # level-dependent envelope differ from a gain-only PCM copy.
            taps = np.array([.72, .2, -.06, .03, .1])
            returned = np.convolve(direct, taps, mode="full")[:len(direct)]
            returned = .26 * np.tanh(6 * returned)
            returned *= np.linspace(.78, 1.16, len(returned))
        add(onset + delay, gain * returned)
    for delay, wave in extra:
        add(onset + delay, wave)
    return np.rint(recording * 32768).astype(np.int16)


class VoiceAnalysisTests(unittest.TestCase):
    def inspect(self, samples, stage="diagnostic", control=(), preparation=None):
        if preparation is None:
            preparation = np.zeros(witness.PREPARATION_FRAMES, dtype=np.int16)
        ambient = witness.calibrate(fixtures.ambient_noise(18))
        return witness.identify_voice(samples, preparation, ambient, stage, control)

    def assert_delay(self, row, delay, overlap):
        self.assertEqual(row["status"], "supported", row)
        self.assertAlmostEqual(row["witness_interval_ms"], delay, delta=2.5)
        self.assertLessEqual(row["lag_range_ms"][0], delay)
        self.assertGreaterEqual(row["lag_range_ms"][1], delay)
        selected = next(item for item in row["candidates"] if item["delay_ms"] == row["witness_interval_ms"])
        self.assertIs(selected["overlap"], overlap)
        self.assertEqual(selected["confidence"], "high")

    def test_long_voice_quiet_copy_and_known_delays(self):
        for duration, delay, gain in ((100, 1100, .075), (158, 231.37, .043), (300, 452.7, .11)):
            with self.subTest(ms=duration):
                self.assert_delay(self.inspect(trial(duration, returns=[(delay, gain, False)])), delay, False)

    def test_colored_compressed_return_separate_and_overlapping(self):
        for duration, delay, gain in ((155, 35, .17), (185, 348.6, .13),
                                      (215, 127.25, .19), (300, 158.3, .22)):
            with self.subTest(delay=delay):
                row = self.inspect(trial(duration, returns=[(delay, gain, True)]))
                self.assert_delay(row, delay, delay < duration)

    def test_room_reflection_observed_then_vetoed(self):
        capture = trial(245, returns=[(143.5, .20, False)])
        control = self.inspect(capture, "control")
        self.assertEqual(control["status"], "control_examined", control)
        ranges = [item["lag_range_ms"] for item in control["candidates"] if item["confidence"] != "low"]
        self.assertTrue(any(low <= 143.5 <= high for low, high in ranges), control)
        diagnostic = self.inspect(capture, control=ranges)
        self.assertEqual(diagnostic["status"], "inconclusive", diagnostic)
        self.assertEqual(diagnostic["reason"], "candidate_matches_control")
        self.assertTrue(any(item.get("matches_control") for item in diagnostic["candidates"]))

        mixed = trial(245, returns=[(143.5, .2, False), (284.1, .26, True)])
        # The room's tail still overlaps a return that begins after the dry syllable.
        self.assert_delay(self.inspect(mixed, control=ranges), 284.1, True)

    def test_no_return_or_unrelated_secondary_event(self):
        for samples in (
            trial(300),
            trial(208, extra=[(354, .38 * syllable(210, seed=71))]),
            trial(208, extra=[(229, .4 * syllable(208, seed=31))]),
            trial(260, returns=[(220, .22, False), (408, .17, False)]),
            trial(230, returns=[(21, .3, False)]),
            trial(250, returns=[(63, .006, False)]),
        ):
            with self.subTest(peak=np.max(np.abs(samples))):
                row = self.inspect(samples)
                self.assertEqual(row["status"], "inconclusive", row)
                self.assertNotIn("witness_interval_ms", row)

    def test_close_arrivals_and_single_periodic_sound_are_ambiguous(self):
        samples = trial(206, returns=[(220, .15, False), (224, .15, False)])
        pair = self.inspect(samples)
        self.assertEqual(pair["status"], "inconclusive", pair)
        self.assertTrue(any(item["nearby_competitor_ms"] is not None for item in pair["candidates"]))

        # The periodic waveform produces strong correlations at many lags,
        # but contains only one sound with a smooth, nonconstant envelope.
        phase = 2 * np.pi * 500 * np.arange(10000) / witness.RATE
        shape = np.sin(np.pi * np.arange(10000) / 10000) ** .75
        tone = 14000 * (np.sin(phase) + .3 * np.sin(phase * 2)) * shape
        samples = np.zeros(witness.TRIAL_FRAMES, dtype=np.int16)
        samples[26000:36000] = tone
        self.assertEqual(self.inspect(samples)["status"], "inconclusive")

    def test_envelope_and_spectrum_without_waveform_are_diagnostic_only(self):
        source = syllable(208)
        phase_flips = 1 - 2 * (np.arange(source.size) // 480 % 2)
        samples = trial(208, extra=[(350, .32 * source * phase_flips)])
        row = self.inspect(samples)
        self.assertEqual(row["status"], "inconclusive", row)
        self.assertEqual(row["reason"], "insufficient_candidate_confidence")
        moderate = [item for item in row["candidates"] if item["confidence"] == "moderate"]
        self.assertTrue(any(item["anchor_count"] == 0 and item["lag_range_ms"][0] <= 350
                            <= item["lag_range_ms"][1] for item in moderate), row)

    def test_valid_pair_with_unrelated_late_sound_is_rejected(self):
        samples = trial(200, returns=[(220, .2, False)], extra=[(2100, syllable(160))])
        self.assertEqual(self.inspect(samples)["reason"], "unexpected_late_sound")

    def test_preparation_and_clipping_reject_measurement(self):
        samples = trial(170, returns=[(220, .3, False)])
        prep = np.zeros(witness.PREPARATION_FRAMES, dtype=np.int16)
        prep[-1000:] = 5000
        self.assertEqual(self.inspect(samples, preparation=prep)["reason"], "early_event")
        samples[891] = -32768
        self.assertEqual(self.inspect(samples)["reason"], "clipping")


class VoiceCampaignTests(unittest.TestCase):
    def run_cli(self, args, windows):
        stream = fixtures.PacedPipe(fixtures.campaign_signal(windows, noise_counts=18))
        stdout, stderr = StringIO(), StringIO()
        with (patch.object(sys, "argv", ["witness", "--mode", "voice", "--source", "synthetic", *args]),
              patch.object(witness, "open_capture", side_effect=stream.open) as capture,
              redirect_stdout(stdout), redirect_stderr(stderr)):
            code = witness.main()
        capture.assert_called_once_with("synthetic")
        return code, json.loads(stdout.getvalue()), stderr.getvalue()

    def test_three_control_three_diagnostic_then_publish_ten(self):
        with tempfile.TemporaryDirectory() as directory:
            control_file = Path(directory) / "control.json"
            diagnostic_file = Path(directory) / "diagnostic.json"
            control_args = ["--voice-reference", str(control_file)]
            control = [trial(duration, onset=600 + duration) for duration in (115, 200, 296)]
            code, result, messages = self.run_cli(["--voice-control"], control)
            self.assertEqual(code, 0, result)
            self.assertEqual(messages.count("DITES « TAC ! » MAINTENANT"), 3)
            self.assertEqual(result["summary"]["status"], "voice_control_ready")
            self.assertIsNone(result["summary"]["median_witness_ms"])
            control_file.write_text(json.dumps(result))
            pairs = [trial(207, onset=450 + 38 * i, returns=[(delay, .20, True)])
                     for i, delay in enumerate((207.1, 203.7, 210.2))]
            code, result, messages = self.run_cli(control_args, pairs)
            self.assertEqual(code, 0, result["summary"])
            self.assertEqual(messages.count("DITES « TAC ! » MAINTENANT"), 3)
            self.assertEqual(messages.count("confiance élevée"), 3)
            self.assertIsNone(result["summary"]["median_witness_ms"])
            diagnostic_file.write_text(json.dumps(result))

            args = [*control_args, "--trials", "10", "--voice-diagnostic", str(diagnostic_file)]
            pairs = [trial(240, returns=[(delay, .20, True)])
                     for delay in (201, 205, 207, 209, 205, 211, 207, 208)]
            code, result, messages = self.run_cli(args, pairs + [trial(), trial()])
            self.assertEqual(code, 0, result["summary"])
            self.assertEqual(messages.count("DITES « TAC ! » MAINTENANT"), 10)
            self.assertEqual(result["summary"]["status"], "voice_witness_intervals")
            self.assertEqual(result["summary"]["supported"], 8)
            self.assertAlmostEqual(result["summary"]["median_witness_ms"], 207, delta=1)
            self.assertAlmostEqual(result["summary"]["mad_ms"], 2, delta=1)
            starts = [row["start_frame"] for row in result["trials"]]
            np.testing.assert_array_equal(np.diff(starts), witness.PREPARATION_FRAMES + witness.TRIAL_FRAMES)

            # A valid report is associated with a specific off-pass artifact.
            control_file.write_text(control_file.read_text() + "\n")
            with (patch.object(sys, "argv", ["witness", "--mode", "voice", "--source", "synthetic", *args]),
                  patch.object(witness, "open_capture") as capture, redirect_stderr(StringIO())):
                with self.assertRaises(SystemExit) as error:
                    witness.main()
            self.assertEqual(error.exception.code, 2)
            capture.assert_not_called()

    def test_summary_rejects_short_or_inconsistent_campaign(self):
        def row(delay):
            return {"status": "supported", "witness_interval_ms": delay, "lag_range_ms": [delay - 4, delay + 4]}

        for rows in ([row(220)] * 9, [row(220)] * 7 + [{"status": "inconclusive"}] * 3,
                     [row(220)] * 8 + [row(250)] * 2,
                     [row(220)] * 5 + [row(239)] * 5):
            summary = witness.voice.summarize(rows, "measurement", None)
            self.assertEqual(summary["status"], "inconclusive")
            self.assertIsNone(summary["median_witness_ms"])

    def test_truncated_voice_capture_cannot_validate_control(self):
        code, result, messages = self.run_cli(
            ["--voice-control"], [trial(160), trial(180), trial(200)[:witness.RATE]])
        self.assertEqual(code, 1)
        self.assertEqual(messages.count("DITES « TAC ! » MAINTENANT"), 3)
        self.assertEqual(result["summary"]["status"], "inconclusive")
        self.assertEqual(result["summary"]["reason"], "incomplete_capture")
        self.assertEqual(result["error"]["reason"], "pcm_eof")
        self.assertIsNone(result["summary"]["median_witness_ms"])

    def test_finished_but_ambiguous_control_allows_diagnostic_candidates_only(self):
        noisy = trial(180, extra=[(2200, syllable(140))])
        code, control, _ = self.run_cli(["--voice-control"], [trial(150), noisy, trial(170)])
        self.assertEqual(code, 1)
        self.assertEqual(control["summary"]["status"], "inconclusive")
        with tempfile.TemporaryDirectory() as directory:
            reference = Path(directory) / "control.json"
            reference.write_text(json.dumps(control))
            pairs = [trial(180, returns=[(240, .2, False)])] * 3
            code, diagnostic, messages = self.run_cli(["--voice-reference", str(reference)], pairs)
        self.assertEqual(code, 1)
        self.assertEqual(diagnostic["summary"]["reason"], "control_not_validated")
        self.assertTrue(all(row["status"] == "inconclusive" for row in diagnostic["trials"]))
        self.assertEqual(messages.count("confiance élevée"), 3)
        self.assertIsNone(diagnostic["summary"]["median_witness_ms"])


if __name__ == "__main__":
    unittest.main()
