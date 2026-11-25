#!/usr/bin/python3 -B
"""Contrôles du propriétaire de processus sans matériel audio."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, time
import unittest
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _parole_index import DEFAULTS
from _parole_maintenance import Maintenance
from _parole_resources import GIB, Resources, contents


class ResourceTests(unittest.TestCase):
    def test_stem_shared_blocks_do_not_count_twice_against_the_cap(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            stem = root / "instrumental.flac"
            stem.write_bytes(b"x" * 8192)
            (root / "reused").mkdir()
            os.link(stem, root / "reused" / stem.name)
            allocated = stem.stat().st_blocks * 512
            self.assertGreater(allocated, 0)
            self.assertEqual(contents(root), (allocated, {stem.name: allocated}, allocated))

    def test_storage_checks_the_least_free_partition_and_working_margin(self):
        resources = Resources(Path("/fixture"))
        resources.storage = {"bytes": GIB, "free": {"library": 22 * GIB, "cache": 20 * GIB}}
        self.assertFalse(resources.allowed_storage(DEFAULTS, duration=200))
        resources.storage["free"]["cache"] = 22 * GIB
        self.assertTrue(resources.allowed_storage(DEFAULTS, duration=200))
        resources.storage["bytes"] = 50 * GIB - 10
        self.assertFalse(resources.allowed_storage(DEFAULTS, duration=200))

    def test_kara_selection_and_return_of_activity_release_supervised_work(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Maintenance(root, None, state=root / "state")
            self.addCleanup(runner.close)
            runner.current_settings = runner.index.settings()
            policy = Mock(return_value=True)
            runner.resources.check = policy
            for phase in ("Demucs", "Turbo"):
                with self.subTest(phase=phase):
                    subject = phase.lower()
                    runner.priority_until = 0.
                    runner.index.enqueue("prepare", subject, "fixture")
                    entry = runner.index.claim(("prepare",), time())
                    entered = Event()

                    def prepare(_task):
                        command = [sys.executable, "-c",
                                   "import json,time; print(json.dumps({'started': True}), flush=True); time.sleep(25)"]
                        runner.output(command, timeout=10,
                                      observer=lambda event: entered.set() if event.get("started") else None)
                        self.fail("Le traitement devait libérer ses ressources")

                    runner.prepare = prepare
                    worker = Thread(target=runner.execute, args=(entry,))
                    worker.start()
                    try:
                        self.assertTrue(entered.wait(2))
                        start = monotonic()
                        if phase == "Demucs":
                            # The selected song can retry once our group has
                            # released the slot. GTK never waits synchronously.
                            self.assertFalse(runner.yield_heavy())
                        else:
                            policy.return_value = False
                        worker.join(timeout=5)
                        self.assertFalse(worker.is_alive())
                        self.assertLess(monotonic() - start, 5)
                        with runner.index.db() as db:
                            job = db.execute("SELECT state,detail FROM tasks WHERE kind='prepare' AND subject=?",
                                             (subject,)).fetchone()
                            self.assertEqual(tuple(job), ("suspended", "ressources libérées"))
                        self.assertTrue(runner.yield_heavy())
                    finally:
                        runner.interrupt.set()
                        worker.join(timeout=5)
                        runner.interrupt.clear()
                        policy.return_value = True


if __name__ == "__main__":
    unittest.main()
