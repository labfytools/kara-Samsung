#!/usr/bin/python3 -B
"""Contrôles du propriétaire de processus sans matériel audio."""
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import monotonic, time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _parole_index import DEFAULTS
from _parole_maintenance import Journal, Maintenance
from _parole_resources import GIB, Resources, contents, thermal_admission, memory_psi, memory_limits


class ResourceTests(unittest.TestCase):
    def test_configurable_closed_lid_thermal_ceiling(self):
        self.assertFalse(thermal_admission(DEFAULTS, [70], [60], nocturnal=True, closed=True))
        self.assertTrue(thermal_admission(DEFAULTS, [69], [60], nocturnal=True, closed=True))
        settings = dict(DEFAULTS, night_cpu_temperature=95, night_gpu_temperature=95,
                        night_closed_temperature=95)
        self.assertTrue(thermal_admission(settings, [94.9], [94.9], nocturnal=True, closed=True))
        self.assertFalse(thermal_admission(settings, [95], [70], nocturnal=True, closed=True))
        self.assertFalse(thermal_admission(settings, [70], [95], nocturnal=True, closed=True))
        self.assertFalse(thermal_admission(settings, [], [60], nocturnal=True, closed=True))
        self.assertFalse(thermal_admission(settings, [60], [], nocturnal=True, closed=True))
        self.assertFalse(thermal_admission(settings, [0], [60], nocturnal=True, closed=True))
        settings.update(night_cpu_temperature=100, night_gpu_temperature=100, night_closed_temperature=100)
        self.assertFalse(thermal_admission(settings, [95], [60], nocturnal=True, closed=True))
        settings.update(night_gpu_temperature=75)
        self.assertFalse(thermal_admission(settings, [80], [75], nocturnal=True, closed=True))

    def test_psi_distinguishes_partial_stall_from_full_stall(self):
        self.assertEqual(memory_psi("some avg10=2.08 avg60=0.1\nfull avg10=1.50 avg60=0.1"), (2.08, 1.5))
        for text in ("some avg10=2.08", "some avg10=nan\nfull avg10=0", "some avg10=0\nfull avg10=101"):
            with self.assertRaises(ValueError):
                memory_psi(text)
        self.assertEqual(memory_limits(closed=True, running=True), (5., 3.))
        self.assertEqual(memory_limits(closed=True, running=False), (1., 1.))
        self.assertEqual(memory_limits(closed=True, running=True, light=True), (1., 1.))
        self.assertEqual(memory_limits(closed=False, running=True), (1., 1.))

    def test_closed_running_memory_policy_and_cooldown(self):
        resources = Resources(Path("/fixture"))
        resources.collect_storage = Mock()
        resources.disk_load = Mock(return_value=0)
        resources.allowed_storage = Mock(return_value=True)
        resources.session_idle = Mock(return_value=True)
        resources.storage = {"bytes": 0, "free": {"test": 100 * GIB}}
        cpu = Path("/sys/class/hwmon/hwmon-test-cpu")
        gpu = Path("/sys/class/hwmon/hwmon-test-gpu")
        busy = Path("/sys/class/drm/card0/device/gpu_busy_percent")
        readings = {"/proc/stat": "cpu 1 0 1 50 0 0 0 0",
                    "/proc/meminfo": "MemAvailable: 6324224 kB",
                    "/proc/pressure/memory": "some avg10=2.08\nfull avg10=2.00",
                    str(cpu / "name"): "k10temp", str(gpu / "name"): "amdgpu",
                    str(cpu / "temp1_input"): "60000", str(gpu / "temp1_input"): "55000",
                    str(busy): "0"}
        def glob(path, pattern):
            if str(path) == "/sys/class/hwmon": return [cpu, gpu]
            if str(path) == "/sys/class/drm": return [busy]
            if path in (cpu, gpu): return [path / "temp1_input"]
            return []
        with (patch.object(Path, "read_text", lambda path: readings[str(path)]),
              patch.object(Path, "glob", glob),
              patch("_parole_resources.multimedia_active", return_value=False),
              patch("_parole_resources.monotonic", return_value=100.) as clock):
            self.assertTrue(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            # A fresh admission must not reuse the more permissive running sample.
            self.assertFalse(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertTrue(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            readings["/proc/pressure/memory"] = "some avg10=3.10\nfull avg10=3.00"
            clock.return_value = 102.
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            self.assertIn("full avg10 3.00 %", resources.memory_detail)
            self.assertEqual(resources.memory_retry_at, 132.)
            readings["/proc/pressure/memory"] = "some avg10=0.10\nfull avg10=0.05"
            clock.return_value = 104.
            self.assertFalse(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertIn("Pause mémoire", resources.reason)
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True, light=True))
            clock.return_value = 134.
            readings["/proc/stat"] = "cpu 2 0 2 150 0 0 0 0"
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True))
            readings["/proc/meminfo"] = "MemAvailable: 1310720 kB"
            clock.return_value = 136.
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            self.assertIn("réserve", resources.memory_detail)

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

    def test_memory_observation_distinguishes_pressure_from_reserve(self):
        resources = Resources(Path("/fixture"))
        resources.collect_storage = Mock()
        resources.disk_load = Mock(return_value=0)
        resources.allowed_storage = Mock(return_value=True)
        resources.session_idle = Mock(return_value=True)
        readings = {"/proc/stat": "cpu 1 0 1 50 0 0 0 0",
                    "/proc/meminfo": "MemAvailable: 5570560 kB",
                    "/proc/pressure/memory": "some avg10=1.12 avg60=0.10 avg300=0.10 total=4\nfull avg10=0.10 avg60=0.10 avg300=0.10 total=4"}
        with (patch.object(Path, "read_text", lambda path: readings[str(path)]),
              patch.object(Path, "glob", return_value=[])):
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True))
            self.assertIn("PSI · 5440 Mio", resources.memory_detail)
            self.assertIn("1.12 %", resources.memory_detail)
            resources.sample = 0
            readings.update({"/proc/meminfo": "MemAvailable: 1310720 kB",
                             "/proc/pressure/memory": "some avg10=0.29 avg60=0.10 avg300=0.10 total=4\nfull avg10=0.10 avg60=0.10 avg300=0.10 total=4"})
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True))
            self.assertIn("réserve · 1280 Mio", resources.memory_detail)
            resources.check(DEFAULTS, busy=True, nocturnal=True)
            self.assertIsNone(resources.memory_detail)

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

    def test_memory_diagnostic_remains_in_console_after_restart(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Maintenance(root, None, state=root / "state")
            self.addCleanup(runner.close)
            runner.resources.memory_detail = "PSI · 5440 Mio disponibles · 1.12 %"
            runner.progress = ("DEMUCS", "Titre privé", .5)
            runner.log_memory_stop()
            restarted = Journal(runner.index.folder)
            self.addCleanup(restarted.close)
            _, entries = restarted.snapshot()
            self.assertEqual(entries[-1][1:], ("RESSOURCES", "WARNING",
                                               "DEMUCS suspendu · PSI · 5440 Mio disponibles · 1.12 %"))


if __name__ == "__main__":
    unittest.main()
