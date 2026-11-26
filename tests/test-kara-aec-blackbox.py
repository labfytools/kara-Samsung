#!/usr/bin/python3 -B
"""The offline replay accepts shared frames and refuses a different graph."""
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox
from _kara_blackbox_aec import compare, input_pair
from _kara_blackbox_lab import analyze

REPLAY = runpy.run_path(str(Path(__file__).with_name("qualify-kara-aec-blackbox.py")))


def capture(root, witness_driver):
    manager = BlackBox(root)
    manager.start(consent=True, voice=True, tracks=["monitor", "witness"])
    session = manager.voice_session()
    duration, delay, rate = 5, 1920, 48000
    source = np.random.default_rng(429).normal(0, .07, rate * duration).astype("<f4")
    monitor = np.column_stack((source, source))
    witness = np.roll(source * .6, delay)
    witness[:delay] = 0
    try:
        for first in range(0, source.size, 4096):
            end = min(first + 4096, source.size)
            clock = dict(frame=first, monotonic_ns=1_000_000_000 + first * 1_000_000_000 // rate,
                         xrun=0, quantum=end - first, route=1, epoch=1)
            common = dict(turn="0123456789abcdef", player="Fixture", song="Synthétique", recording_session=session)
            manager.pcm("monitor", rate, 2, monitor[first:end].tobytes(), **common,
                        clock=dict(clock, driver=4, present=6, spa_headers=6))
            manager.pcm("witness", rate, 1, witness[first:end].tobytes(), **common,
                        clock=dict(clock, driver=witness_driver, present=1, spa_headers=1))
    finally:
        manager.stop(wait=True)
    return root / session


class ReplayTests(unittest.TestCase):
    def test_shared_clock_and_disjoint_drivers(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            shared = capture(root / "shared", 4)
            metrics = REPLAY["run"](shared)
            self.assertEqual(metrics["seconds"], 5)
            self.assertEqual(metrics["delay_candidate_ms"], 40)
            self.assertGreater(metrics["input_correlation"], .95)
            self.assertLess(metrics["webrtc"]["rms"], metrics["witness_rms"])
            self.assertEqual(REPLAY["matched_cycles"](capture(root / "disjoint", 5)), [])

    def test_v3_pairs_phone_and_output_with_distinct_clock_domains(self):
        with TemporaryDirectory() as directory:
            manager = BlackBox(Path(directory) / "blackbox", queue_size=2048)
            manager.start(consent=True, voice=True, tracks=["wifi", "monitor"])
            session = manager.voice_session()
            turn = dict(turn="a" * 16, player="Fixture", song="Synthétique")
            connection = "c" * 32
            rate = 48000
            mono = np.random.default_rng(49).normal(0, .1, 6 * rate).astype("<f4")
            echo = np.zeros_like(mono)
            echo[1920:] = mono[:-1920] * .6
            base = 1_000_000_000
            manager.emit("party", "turn-start", **turn)
            for first in range(0, len(mono), 960):
                stop = first + 960
                stamp = base + first * 1_000_000_000 // rate
                monitor = np.column_stack((mono[first:stop], mono[first:stop]))
                clock = dict(driver=3, epoch=1, frame=first, monotonic_ns=stamp,
                             xrun=0, quantum=960, route=1, present=6, spa_headers=6)
                manager.pcm("monitor", rate, 2, monitor.tobytes(), **turn, clock=clock,
                            recording_session=session)
                manager.pcm("wifi", rate, 1, (echo[first:stop] * 32768).astype("<i2").tobytes(),
                            **turn, connection=connection, packet_timing={"frame_end": stop})
                if first in (rate // 2, (rate * 11) // 2):
                    manager.emit("clock", "mobile-anchor", **turn, connection=connection, rate=rate,
                                 context_frame=first, renderer_frame_min_ns=stamp,
                                 renderer_frame_max_ns=stamp)
            manager.emit("party", "turn-end", **turn)
            manager.stop(wait=True)
            folder = manager.root / session
            report = analyze(folder)
            render, mic = input_pair(folder, report, f"{session}/{turn['turn']}", "monitor")
            self.assertEqual(len(mic), 5 * rate)
            metrics = compare(render, mic)
            self.assertEqual(metrics["delay_candidate_ms"], 40)
            self.assertIsNone(metrics["physical_alignment_bound_ns"])
            self.assertFalse(metrics["ground_truth_available"])
            self.assertGreater(metrics["variants"]["unchanged"]["reference_correlation"], .99)


if __name__ == "__main__":
    unittest.main()
