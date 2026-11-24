#!/usr/bin/env python3
"""Timing telemetry and the one-ADC witness method, with synthetic signals."""
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
import selectors
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import numpy as np

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))
from _kara_voice_latency import RelativeTransit, SubmissionCadence, TimingWindow, TransportFreshness, player_backlog
import _kara_voice_latency_witness as witness


class TimingTests(unittest.TestCase):
    def test_freshness_guard_ignores_idle_sender_and_bounds_recovery(self):
        guard = TransportFreshness(40)
        self.assertIsNone(guard.observe(2000, 10))
        self.assertIsNone(guard.observe(4000, 2011))
        self.assertEqual(guard.observe(4010, 11000), "stale")
        self.assertEqual(guard.observe(4020, 12999), "stale")
        self.assertEqual(guard.observe(4030, 13000), "restart")
        self.assertIsNone(guard.observe(99, 13005))
        self.assertIsNone(guard.observe(109, 13015))

    def test_small_samples_are_not_high_percentiles(self):
        window = TimingWindow()
        self.assertIsNone(window.summary()["max"])
        for i in range(19):
            window.append(20 * i, i)
        self.assertEqual(window.summary()["p50"], 9)
        self.assertIsNone(window.summary()["p95"])
        for i in range(19, 100):
            window.append(20 * i, i)
        result = window.summary()
        self.assertEqual((result["p50"], result["p95"], result["p99"], result["max"]),
                         (49, 94, 98, 99))
        self.assertIsNone(result["trend_ms_per_s"])

    def test_clock_origins_do_not_change_relative_network_variation(self):
        def replay(android_origin, linux_origin, drift_ppm):
            transit = RelativeTransit()
            for i in range(400):
                nominal = i * 20
                sent = nominal + android_origin
                received = linux_origin + nominal * (1 + drift_ppm / 1e6) + 30 + i % 5
                transit.observe(sent, received)
            return transit

        first = replay(0, 1e8, 0).summary()
        second = replay(1e9, 25, 0).summary()
        self.assertEqual(first, second)
        self.assertEqual((first["p50"], first["p95"], first["p99"], first["max"]), (2, 4, 4, 4))
        skewed = replay(1e9, 25, 300)
        self.assertAlmostEqual(skewed.summary()["trend_ms_per_s"], .3, places=2)
        skewed.observe(0, 9000)
        carry = skewed.summary()
        self.assertEqual(carry["clock_resets"], 1)
        self.assertEqual(carry["n"], 1)
        self.assertIsNone(carry["p95"])

    def test_progressive_wait_with_gigue_and_bounded_recent_window(self):
        window = TimingWindow()
        for i in range(2100):
            when = i * 10
            window.append(when, 1.7 * when / 1000 + i % 6)
        result = window.summary()
        self.assertEqual(result["n"], 1200)
        self.assertAlmostEqual(result["trend_ms_per_s"], 1.7, places=2)
        self.assertLess(result["p95"], result["max"])

    def test_submission_cadence_reports_pcm_not_device_clock(self):
        normal, drops = SubmissionCadence(), SubmissionCadence()
        for i in range(501):
            normal.observe(i * 10, 480)
            if i % 5 != 4:
                drops.observe(i * 10, 480)
            if i == 499:
                self.assertIsNone(normal.summary())
        self.assertEqual(normal.summary(), 48000)
        self.assertEqual(drops.summary(), 38400)

    def test_local_kernel_and_asyncio_buffers_are_distinct(self):
        class Transport:
            def get_write_buffer_size(self):
                return 384

        read, write = os.pipe()
        try:
            os.write(write, bytes(1920))
            result = player_backlog(Transport(), write)
            self.assertEqual(result["kernel_pipe_pcm_ms"], 10)
            self.assertEqual(result["asyncio_pcm_ms"], 2)
            os.read(read, 384)
            self.assertEqual(player_backlog(Transport(), write)["kernel_pipe_bytes"], 1536)
        finally:
            os.close(write)
            os.close(read)
        self.assertIsNone(player_backlog(Transport(), write)["kernel_pipe_bytes"])


def transients(times, amplitudes, frames=witness.TRIAL_FRAMES):
    generator = np.random.default_rng(92)
    signal = generator.integers(-30, 31, size=frames, dtype=np.int16)
    t = np.arange(9 * witness.RATE // 1000) / witness.RATE
    for time, amplitude in zip(times, amplitudes, strict=True):
        index = round(time * witness.RATE / 1000)
        wave = np.sin(t * 2 * np.pi * 1473) * np.exp(-t / .005)
        signal[index:index + len(t)] += np.rint(wave * amplitude).astype(np.int16)
    return signal


class WitnessTests(unittest.TestCase):
    def test_same_adc_fixed_offset_and_jitter(self):
        offsets = [221.3, 217.2, 224.6, 221.1, 222.3] * 2
        rows = []
        for i, offset in enumerate(offsets):
            start = 380.2 + i * 17.6
            report = witness.identify(transients([start, start + offset], [14000, 5600]), -40, "return")
            self.assertEqual(report["status"], "accepted")
            self.assertAlmostEqual(report["witness_interval_ms"], offset, delta=2)
            self.assertLessEqual(report["threshold_and_bin_range_ms"][0], offset)
            self.assertGreaterEqual(report["threshold_and_bin_range_ms"][1], offset)
            rows.append(report)
        result = witness.summarize(rows, "return")
        self.assertEqual(result["status"], "witness_intervals")
        self.assertAlmostEqual(result["median_witness_ms"], 221.3, delta=2)
        self.assertGreater(result["mad_ms"], 0)

    def test_control_reflections_missing_echo_and_clipping(self):
        single = transients([370], [12000])
        self.assertEqual(witness.identify(single, -40, "control")["status"], "accepted")
        self.assertEqual(witness.identify(single, -40, "return")["status"], "missing_event")
        reflection = transients([370, 430], [12000, 4000])
        self.assertEqual(witness.identify(reflection, -40, "control")["status"], "possible_reflections")
        repeats = transients([370, 526, 682], [12000, 7000, 3000])
        self.assertEqual(witness.identify(repeats, -40, "return")["status"], "ambiguous_events")
        single[200] = -32768
        self.assertEqual(witness.identify(single, -40, "return")["status"], "clipping")

    def test_threshold_dependence_and_insufficient_trials(self):
        marginal = transients([370, 630], [13000, 650])
        self.assertEqual(witness.identify(marginal, -40, "return")["status"], "threshold_sensitive")
        good = witness.identify(transients([370, 630], [13000, 6500]), -40, "return")
        self.assertIsNone(witness.summarize([good] * 7, "return")["median_witness_ms"])
        self.assertEqual(witness.summarize([good] * 9 + [{"status": "incomplete"}],
                                          "return")["status"], "incomplete_capture")
        self.assertEqual(witness.identify(bytes(100), -40, "return"), {"status": "incomplete"})

    def test_weak_missing_early_late_and_multiple_claps(self):
        for times, amplitudes, expected in (
                ([], [], "missing_event"),
                ([450], [330], "weak_event"),
                ([120], [14000], "early_event"),
                ([2500], [14000], "late_event"),
                ([500, 820], [14000, 14000], "ambiguous_events")):
            with self.subTest(expected=expected):
                row = witness.identify(transients(times, amplitudes), -40, "control")
                self.assertEqual(row["status"], expected)
                self.assertIn("peak_dbfs", row)
                self.assertFalse(row["clipped"])
                self.assertNotIn("direct_ms", row)

    def test_present_return_cannot_validate_pre_prompt_clap(self):
        preparation = transients([1280], [14000], witness.PREPARATION_FRAMES)
        fake_pair = transients([400, 620], [7000, 6000])
        row = witness.identify(fake_pair, -40, "return", preparation=preparation)
        self.assertEqual((row["status"], row["phase"]), ("early_event", "preparation"))
        self.assertNotIn("witness_interval_ms", row)

    def test_saturation_before_invitation_is_identified_separately(self):
        preparation = np.zeros(witness.PREPARATION_FRAMES, dtype=np.int16)
        preparation[-10] = -32768
        row = witness.identify(transients([700], [14000]), -40, "control", preparation=preparation)
        self.assertEqual(row["status"], "early_event")
        self.assertTrue(row["preparation_clipped"])
        self.assertFalse(row["clipped"])

    def test_ambient_normal_loud_and_unstable(self):
        quiet = ambient_noise(465)
        normal = witness.calibrate(quiet)
        self.assertEqual(normal["status"], "ok")
        self.assertAlmostEqual(normal["rms_dbfs"], -37, delta=.4)
        self.assertGreater(normal["p95_dbfs"], normal["rms_dbfs"])
        self.assertGreaterEqual(normal["threshold_dbfs"] - normal["p95_dbfs"], 10)
        self.assertEqual(witness.calibrate(ambient_noise(2000))["status"], "ambient_too_loud")
        self.assertEqual(witness.calibrate(quiet, -40)["status"], "ambient_too_loud")
        quiet[:witness.RATE // 4] //= 4
        self.assertEqual(witness.calibrate(quiet)["status"], "ambient_unstable")


def ambient_noise(rms_counts):
    return np.random.default_rng(45).normal(0, rms_counts, witness.AMBIENT_FRAMES).astype("=i2")


class PeakInspectionTests(unittest.TestCase):
    def setUp(self):
        self.ambient = witness.calibrate(ambient_noise(18))
        self.assertEqual(self.ambient["threshold_dbfs"], -44)

    def inspect(self, samples, preparation=None):
        decision = witness.identify(samples, self.ambient["threshold_dbfs"], "return",
                                    self.ambient["p95_dbfs"], preparation)
        return decision, witness.inspect_trial(samples, decision, self.ambient)

    def test_weak_return_is_visible_at_noise_threshold_only(self):
        decision, report = self.inspect(transients([400, 620], [14000, 65]))
        self.assertEqual((decision["status"], decision["phase"]), ("missing_event", "return"))
        floor = self.ambient["p95_dbfs"]
        self.assertEqual(report["diagnostic_thresholds_dbfs"], [floor + 10, floor + 5])
        self.assertEqual(report["direct"]["start_ms"], 400)
        peak, = report["secondary_peaks"]
        self.assertEqual((peak["start_ms"], peak["delay_ms"]), (620, 220))
        self.assertEqual(peak["detection_threshold_dbfs"], floor + 5)
        self.assertAlmostEqual(peak["level_dbfs"], -58, delta=1)
        self.assertAlmostEqual(peak["relative_db"], peak["level_dbfs"] - report["direct"]["level_dbfs"])
        self.assertNotIn("witness_interval_ms", decision)

    def test_return_near_nominal_on_either_side_remains_threshold_sensitive(self):
        for amplitude, counts, crossed_db in ((350, [2, 1, 1], -47), (400, [2, 2, 1], -44)):
            with self.subTest(amplitude=amplitude):
                decision, report = self.inspect(transients([400, 620], [14000, amplitude]))
                self.assertEqual(decision["status"], "threshold_sensitive")
                self.assertEqual([item["event_count"] for item in report["v2_detection"]], counts)
                peak, = report["secondary_peaks"]
                self.assertAlmostEqual(peak["level_dbfs"], -44, delta=1.3)
                self.assertEqual(peak["detection_threshold_dbfs"], crossed_db)

    def test_multiple_reflections_and_weak_candidate_are_distinct(self):
        decision, report = self.inspect(transients([400, 454, 486, 620], [14000, 4000, 1000, 65]))
        self.assertEqual(decision["status"], "possible_reflections")
        peaks = report["secondary_peaks"]
        self.assertEqual([peak["delay_ms"] for peak in peaks], [54, 86, 220])
        self.assertEqual([peak["possible_reflection"] for peak in peaks], [True, True, False])
        self.assertIn("d’origine indéterminée", witness.describe_inspection(report))

    def test_delay_window_uses_group_onsets_and_includes_upper_boundary(self):
        for delay in (34, 36, 146, 700, 1100, 1102):
            with self.subTest(delay=delay):
                decision, report = self.inspect(transients([400, 400 + delay], [14000, 110]))
                self.assertEqual(decision["status"], "missing_event")
                expected = [delay] if 35 <= delay <= 1100 else []
                self.assertEqual([peak["delay_ms"] for peak in report["secondary_peaks"]], expected)

    def test_lower_threshold_does_not_merge_already_separated_events(self):
        samples = transients([400, 620, 760], [14000, 2000, 95])
        start, end = 400 * witness.RATE // 1000, 625 * witness.RATE // 1000
        # The quiet bridge merges the direct and the strong peak only at the
        # diagnostic levels. Its group must neither replace nor duplicate them.
        time = np.arange(end - start) / witness.RATE
        samples[start:end] += np.rint(120 * np.sin(2 * np.pi * 1000 * time)).astype(np.int16)
        decision, report = self.inspect(samples)
        self.assertEqual(decision["witness_interval_ms"], 220)
        self.assertEqual([peak["delay_ms"] for peak in report["secondary_peaks"]], [220, 360])

    def test_first_event_below_nominal_can_still_anchor_the_inspection(self):
        decision, report = self.inspect(transients([400, 620], [260, 3000]))
        self.assertEqual(decision["status"], "threshold_sensitive")
        self.assertEqual(report["direct"]["start_ms"], 400)
        self.assertEqual(report["direct"]["detection_threshold_dbfs"], -47)
        self.assertEqual(report["secondary_peaks"][0]["delay_ms"], 220)

    def test_no_distinct_event_or_no_reliable_direct_is_explicit(self):
        # A weak arrival inside the direct group is inseparable at this resolution.
        decision, report = self.inspect(transients([400, 411], [14000, 110]))
        self.assertEqual(decision["status"], "missing_event")
        self.assertEqual(report["secondary_peaks"], [])
        self.assertIn("Aucun second événement identifiable entre 35 et 1100 ms",
                      witness.describe_inspection(report))

        preparation = transients([1280], [14000], witness.PREPARATION_FRAMES)
        decision, report = self.inspect(transients([400, 620], [14000, 4000]), preparation)
        self.assertEqual((decision["status"], decision["phase"]), ("early_event", "preparation"))
        self.assertIsNone(report["direct"])
        self.assertEqual(report["secondary_peaks"], [])
        self.assertIn("Aucun second événement identifiable sans repère direct", witness.describe_inspection(report))


def pcm(*windows):
    return b"".join(window.astype(witness.PCM_DTYPE).tobytes() for window in windows)


def campaign_signal(trials, preparations=None, *, noise_counts=465):
    # The first second exceeds the noise and clipping limits. Only the
    # separate ambient window that follows stabilization may set the threshold.
    startup = np.zeros(witness.STABILIZATION_FRAMES, dtype=np.int16)
    startup[:witness.RATE:3] = -32768
    windows = [startup, ambient_noise(noise_counts)]
    if preparations is None:
        preparations = [np.zeros(witness.PREPARATION_FRAMES, dtype=np.int16)] * len(trials)
    for preparation, trial in zip(preparations, trials, strict=True):
        windows.extend((preparation, trial))
    return pcm(*windows)


class PacedPipe:
    """Drive actual nonblocking pipe reads with a deterministic producer clock."""

    def __init__(self, data, chunk=960, on_delivery=None):
        self.data = data
        self.chunk = chunk
        self.on_delivery = on_delivery
        self.now = 10.0
        self.next_delivery = self.now
        self.position = 0
        self.read_fd, self.write_fd = os.pipe()

    def clock(self):
        return self.now

    def register(self, fd):
        assert fd == self.read_fd

    def poll(self, timeout_ms):
        deadline = self.now + timeout_ms / 1000
        if self.next_delivery > deadline:
            self.now = deadline
            return []
        self.now = max(self.now, self.next_delivery)
        if self.position == len(self.data):
            os.close(self.write_fd)
            self.write_fd = None
            return [(self.read_fd, 1)]
        block = self.data[self.position:self.position + self.chunk]
        os.write(self.write_fd, block)
        self.position += len(block)
        delay = len(block) / witness.BYTES_PER_SECOND
        if self.on_delivery is not None:
            delay = self.on_delivery(self.position, delay)
        self.next_delivery += delay
        return [(self.read_fd, 1)]

    @contextmanager
    def open(self, source):
        try:
            with patch.object(witness, "monotonic", self.clock), patch.object(witness, "poll", return_value=self):
                yield witness.PcmCapture(self.read_fd)
        finally:
            os.close(self.read_fd)
            if self.write_fd is not None:
                os.close(self.write_fd)


class CampaignTests(unittest.TestCase):
    def run_pcm(self, data, mode="control", count=5, retries=0, on_delivery=None, on_prompt=None):
        pipe = PacedPipe(data, on_delivery=on_delivery)
        prompts = []
        messages = []

        def emit(message):
            messages.append(message)
            if message.startswith("CLAQUE"):
                prompts.append(pipe.now)
                if on_prompt is not None:
                    on_prompt(pipe)

        with patch.object(witness, "open_capture", pipe.open):
            result = witness.run_campaign("synthetic", mode, count, retries, None, emit)
        return result, prompts, messages

    def test_five_invites_are_paced_and_startup_is_excluded(self):
        claps = [transients([time], [14000]) for time in (420, 530, 650, 1700, 2040)]
        result, prompts, messages = self.run_pcm(campaign_signal(claps))
        self.assertEqual(result["summary"], {"attempts": 5, "accepted": 5, "status": "single_events"})
        self.assertEqual(result["ambient"]["status"], "ok")
        self.assertAlmostEqual(result["ambient"]["rms_dbfs"], -37, delta=.4)
        positions = [row["start_frame"] for row in result["trials"]]
        separation = (witness.PREPARATION_FRAMES + witness.TRIAL_FRAMES) / witness.RATE
        self.assertEqual(len(prompts), 5)
        np.testing.assert_allclose(np.diff(prompts), separation, atol=.02)
        np.testing.assert_array_equal(np.diff(positions), witness.PREPARATION_FRAMES + witness.TRIAL_FRAMES)
        self.assertEqual(sum(message.startswith("Essai accepté") for message in messages), 5)
        self.assertTrue(all("peak_inspection" not in row for row in result["trials"]))

    def test_cli_inspects_three_trials_with_v2_decisions_and_no_latency_summary(self):
        data = campaign_signal([transients([400, 620], [14000, 65]),
                                transients([600, 820], [14000, 400]),
                                transients([800], [14000])], noise_counts=18)
        baseline, _, _ = self.run_pcm(data, mode="return", count=3)
        args = ["witness", "--mode", "return", "--inspect-peaks", "--trials", "3", "--retries", "0",
                "--source", "synthetic"]
        stdout, stderr = StringIO(), StringIO()
        with (patch.object(sys, "argv", args), patch.object(witness, "open_capture", PacedPipe(data).open),
              redirect_stdout(stdout), redirect_stderr(stderr)):
            self.assertEqual(witness.main(), 1)

        result = json.loads(stdout.getvalue())
        self.assertEqual(result["summary"], baseline["summary"])
        self.assertEqual(result["summary"]["status"], "insufficient_pairs")
        self.assertIsNone(result["summary"]["median_witness_ms"])
        for row, expected in zip(result["trials"], baseline["trials"], strict=True):
            self.assertEqual(row.pop("peak_inspection")["threshold_dbfs"], -44)
            self.assertEqual(row, expected)

        output = stderr.getvalue()
        self.assertEqual(output.count("CLAQUE MAINTENANT"), 3)
        self.assertEqual(output.count("seuil nominal -44.0 dBFS"), 3)
        self.assertIn("Retour distinct introuvable", output)
        self.assertIn("Détection instable selon le seuil", output)
        self.assertIn("+220 ms · t=620 ms ·", output)
        self.assertIn("dB vs direct", output)
        self.assertIn("Aucun second événement identifiable", output)

    def test_explicit_retry_then_known_delay_in_return_mode(self):
        rejected = transients([450], [14000])
        pair = transients([600, 820], [14000, 6000])
        result, prompts, messages = self.run_pcm(campaign_signal([rejected] + [pair] * 10),
                                                 mode="return", count=10, retries=2)
        self.assertEqual(result["summary"]["status"], "witness_intervals")
        self.assertEqual(result["summary"]["median_witness_ms"], 220)
        self.assertEqual(result["summary"]["mad_ms"], 0)
        self.assertEqual(result["summary"]["accepted"], 10)
        self.assertEqual(len(prompts), 11)
        self.assertEqual(result["trials"][0]["status"], "missing_event")
        self.assertTrue(any("Retour distinct introuvable" in message for message in messages))

    def test_retry_budget_silence_never_yields_a_measurement(self):
        data = campaign_signal([transients([], [])] * 7)
        result, prompts, _ = self.run_pcm(data, retries=2)
        self.assertEqual(len(prompts), 7)
        self.assertEqual(result["summary"], {"attempts": 7, "accepted": 0, "status": "inconclusive"})
        self.assertTrue(all(row["status"] == "missing_event" for row in result["trials"]))

    def test_loud_ambient_stops_before_prompt(self):
        signal = pcm(np.zeros(witness.STABILIZATION_FRAMES), ambient_noise(2000))
        result, prompts, _ = self.run_pcm(signal)
        self.assertEqual(prompts, [])
        self.assertEqual(result["summary"]["status"], "ambient_too_loud")

    def test_delayed_pcm_and_delayed_terminal_invalidate_capture(self):
        data = campaign_signal([transients([600], [13000])])
        first_prompt = (witness.STABILIZATION_FRAMES + witness.AMBIENT_FRAMES
                        + witness.PREPARATION_FRAMES) * witness.FRAME_BYTES

        def stall(position, delay):
            return .35 if position == first_prompt else delay

        result, prompts, _ = self.run_pcm(data, count=1, on_delivery=stall)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["trials"][0]["status"], "incomplete")
        self.assertIn(result["error"]["reason"], ("pcm_delayed", "pcm_timeout"))

        def slow_terminal(pipe):
            pipe.now += .10

        result, prompts, _ = self.run_pcm(data, count=1, on_prompt=slow_terminal)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["error"]["reason"], "prompt_delayed")

    def test_fast_producer_backlog_cannot_create_fresh_trials(self):
        pair = transients([500, 720], [14000, 6500])
        data = campaign_signal([pair] * 10)
        end_of_first = (witness.STABILIZATION_FRAMES + witness.AMBIENT_FRAMES
                        + witness.PREPARATION_FRAMES + witness.TRIAL_FRAMES) * witness.FRAME_BYTES

        def burst(position, delay):
            return 0 if position >= end_of_first else delay

        result, prompts, _ = self.run_pcm(data, mode="return", count=10, on_delivery=burst)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["error"]["reason"], "pcm_backlog")
        self.assertEqual(result["summary"]["status"], "incomplete_capture")
        self.assertIsNone(result["summary"]["median_witness_ms"])

    def test_terminal_consumer_delay_with_pending_data(self):
        pair = transients([500, 720], [14000, 6500])
        pipe = PacedPipe(campaign_signal([pair] * 10))
        prompts = []

        def emit(message):
            if message.startswith("CLAQUE"):
                prompts.append(pipe.now)
            if message.startswith("Essai accepté"):
                # The kernel actually holds audio when the consumer resumes.
                os.write(pipe.write_fd, bytes(4096))
                pipe.now += .25

        with patch.object(witness, "open_capture", pipe.open):
            result = witness.run_campaign("synthetic", "return", 10, 2, None, emit)
        self.assertEqual(len(prompts), 1)
        self.assertEqual(result["error"]["reason"], "pcm_delayed")
        self.assertIsNone(result["summary"]["median_witness_ms"])

    def test_eof_after_valid_pairs_suppresses_latency(self):
        pair = transients([600, 820], [14000, 6500])
        short = pair[:witness.RATE]
        result, prompts, _ = self.run_pcm(campaign_signal([pair] * 9 + [short]), mode="return", count=10)
        self.assertEqual(len(prompts), 10)
        self.assertEqual(result["error"]["reason"], "pcm_eof")
        self.assertEqual(result["summary"]["accepted"], 9)
        self.assertIsNone(result["summary"]["median_witness_ms"])

    def test_pcm_frames_survive_split_sample_and_drain_preserves_alignment(self):
        samples = np.arange(3000, dtype=np.int16)
        pipe = PacedPipe(pcm(samples), chunk=901)
        with pipe.open("synthetic") as capture:
            first = capture.read_frames(177)
            queued = capture.drain_available()
            tail = capture.read_frames(3000 - capture.frame)
            self.assertEqual(len(queued), 273)
            np.testing.assert_array_equal(np.concatenate((first, queued, tail)), samples)


class ProcessTests(unittest.TestCase):
    def test_capture_pins_the_selected_source_and_pcm_format(self):
        command = witness.capture_command("synthetic-mic")
        self.assertEqual(command[0], "parec")
        for option in ("--device=synthetic-mic", "--format=s16le", "--channels=1", "--rate=48000",
                       "--property=node.dont-fallback=true", "--property=node.dont-reconnect=true"):
            self.assertIn(option, command)

    def test_ordered_shutdown_keeps_pipe_open_for_child(self):
        # The fake producer fills stdout, then writes once more on SIGTERM.
        # Closing the read end before reaping would fail the child.
        program = """import os, signal
def stop(*_):
    os.write(1, b'end')
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    os.write(1, bytes(4096))
"""
        with patch.object(witness, "capture_command", return_value=[sys.executable, "-c", program]):
            with witness.open_capture("synthetic") as capture:
                self.assertEqual(len(capture.read_frames(100)), 100)

    def test_cli_reports_real_capture_error(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "parec"
            fake.write_text("#!/bin/sh\necho 'capture failure from parec' >&2\nexit 7\n")
            fake.chmod(0o700)
            completed = subprocess.run(
                [sys.executable, str(BIN / "_kara_voice_latency_witness.py"), "--mode", "control"],
                env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]},
                capture_output=True, text=True, timeout=5)
        self.assertEqual(completed.returncode, 1)
        self.assertIn("capture failure from parec", completed.stderr)
        self.assertIn("code 7", completed.stderr)
        self.assertNotIn("CLAQUE MAINTENANT", completed.stderr)
        self.assertEqual(json.loads(completed.stdout)["summary"]["status"], "incomplete_capture")

    def test_cli_ctrl_c_stops_child_and_prints_no_latency(self):
        with tempfile.TemporaryDirectory() as directory:
            fake = Path(directory) / "parec"
            fake.write_text(f"""#!{sys.executable}
import os, signal, sys
def stop(*_):
    print("stopped", file=sys.stderr, flush=True)
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
signal.alarm(6)
print(f"ready {{os.getpid()}}", file=sys.stderr, flush=True)
signal.pause()
""")
            fake.chmod(0o700)
            with subprocess.Popen(
                    [sys.executable, str(BIN / "_kara_voice_latency_witness.py"), "--mode", "return"],
                    env={**os.environ, "PATH": directory + os.pathsep + os.environ["PATH"]},
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0) as process:
                try:
                    with selectors.DefaultSelector() as selector:
                        selector.register(process.stderr, selectors.EVENT_READ)
                        self.assertTrue(selector.select(5), "synthetic child did not start")
                        line = process.stderr.readline()
                        while not line.startswith(b"ready "):
                            self.assertTrue(line, "synthetic child exited")
                            self.assertTrue(selector.select(5), "synthetic child did not become ready")
                            line = process.stderr.readline()
                    child_pid = int(line.split()[1])
                    process.send_signal(signal.SIGINT)
                    output, diagnostic = process.communicate(timeout=5)
                    self.assertEqual(process.returncode, 130)
                    self.assertEqual(output, b"")
                    self.assertIn(b"stopped", diagnostic)
                    self.assertNotIn(b"BrokenPipeError", diagnostic)
                    with self.assertRaises(ProcessLookupError):
                        os.kill(child_pid, 0)
                finally:
                    if process.poll() is None:
                        process.send_signal(signal.SIGINT)
                        try:
                            process.wait(timeout=2)
                        except subprocess.TimeoutExpired:
                            process.kill()


if __name__ == "__main__":
    unittest.main()
