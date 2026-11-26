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


if __name__ == "__main__":
    unittest.main()
