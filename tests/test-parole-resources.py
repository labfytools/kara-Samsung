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
from _parole_resources import GIB, Resources, contents, thermal_admission, memory_psi, memory_limits, memory_reserve


class ResourceTests(unittest.TestCase):
    def test_demucs_thread_policy(self):
        from _kara_stems import demucs_threads
        from _parole_night import Desktop
        self.assertEqual(demucs_threads({}), "6")
        for value in ("2", "6", "12"):
            self.assertEqual(demucs_threads({"KARA_DEMUCS_THREADS": value}), value)
        for value in ("0", "1000", "invalid"):
            self.assertEqual(demucs_threads({"KARA_DEMUCS_THREADS": value}), "6")
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            runner = Maintenance(root, None, state=root / "state")
            self.addCleanup(runner.close)
            runner.night_running = True
            for closed, expected in ((True, "12"), (False, "12")):
                runner.night.desktop = Desktop(True, True, closed, closed, False)
                with patch("_parole_maintenance.run") as invoke:
                    runner.output([sys.executable, "-V"])
                self.assertEqual(invoke.call_args.kwargs["env"]["KARA_DEMUCS_THREADS"], expected)
                self.assertEqual(demucs_threads(invoke.call_args.kwargs["env"]), expected)

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
        self.assertEqual(memory_limits(nocturnal=True, running=True), (None, None))
        self.assertEqual(memory_limits(nocturnal=True, running=False), (None, None))
        self.assertEqual(memory_limits(nocturnal=True, running=True, light=True), (None, None))
        self.assertEqual(memory_limits(nocturnal=False, running=True), (1., 1.))

    def test_closed_memory_reserves_twenty_percent(self):
        for running in (False, True):
            self.assertEqual(memory_reserve(DEFAULTS, total_kib=15596740, nocturnal=True, running=running), 3047)
            self.assertEqual(memory_reserve(DEFAULTS, total_kib=16777216, nocturnal=True, running=running), 3277)
        self.assertEqual(memory_reserve(DEFAULTS), DEFAULTS["memory_mib"])
        self.assertEqual(memory_reserve(DEFAULTS, running=True), DEFAULTS["running_memory_mib"])
        with self.assertRaises(ValueError):
            memory_reserve(DEFAULTS, nocturnal=True, total_kib=0)

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
                    "/proc/meminfo": "MemTotal: 16777216 kB\nMemAvailable: 6324224 kB\nSwapTotal: 6291456 kB\nSwapFree: 0 kB",
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
            readings["/proc/pressure/memory"] = "some avg10=40.00\nfull avg10=25.00"
            clock.return_value = 102.
            readings["/proc/stat"] = "cpu 2 0 2 150 0 0 0 0"
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertTrue(resources.memory_observation["psi_advisory"])
            self.assertEqual(resources.memory_observation["full_avg10"], 25.)
            # Full utilization does not prevent an unattended admission.
            readings[str(busy)] = "100"
            readings["/proc/stat"] = "cpu 1002 0 2 150 0 0 0 0"
            resources.disk_load.return_value = 100
            clock.return_value = 104.
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertEqual(resources.reason, "Mode nocturne · calcul prioritaire")
            readings[str(busy)] = "0"
            resources.disk_load.return_value = 0
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True, light=True))
            self.assertTrue(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            self.assertTrue(resources.check(DEFAULTS, running=True, nocturnal=True, lid=False))
            resources.session_idle.return_value = False
            clock.return_value = 105.
            resources.sample = 0.
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True, lid=False))
            self.assertEqual(resources.reason, "Session active")
            resources.session_idle.return_value = True
            # A full swap does not prevent starting or continuing with more
            # than 20% physical RAM available, even below the old 4-GiB floor.
            readings["/proc/meminfo"] = "MemTotal: 16777216 kB\nMemAvailable: 3460301 kB\nSwapTotal: 6291456 kB\nSwapFree: 0 kB"
            clock.return_value = 106.
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertTrue(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            readings["/proc/meminfo"] = "MemTotal: 16777216 kB\nMemAvailable: 3145728 kB\nSwapTotal: 6291456 kB\nSwapFree: 0 kB"
            clock.return_value = 108.
            self.assertFalse(resources.check(DEFAULTS, running=True, nocturnal=True, lid=True))
            self.assertIn("réserve", resources.memory_detail)
            self.assertIn("informatif", resources.memory_detail)
            self.assertEqual(resources.memory_retry_at, 138.)
            readings["/proc/meminfo"] = "MemTotal: 16777216 kB\nMemAvailable: 6324224 kB\nSwapTotal: 6291456 kB\nSwapFree: 0 kB"
            readings["/proc/pressure/memory"] = "some avg10=0.10\nfull avg10=0.05"
            clock.return_value = 110.
            self.assertFalse(resources.check(DEFAULTS, nocturnal=True, lid=True))
            self.assertIn("Pause mémoire", resources.reason)
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True, light=True))
            clock.return_value = 140.
            readings["/proc/stat"] = "cpu 1003 0 3 250 0 0 0 0"
            self.assertTrue(resources.check(DEFAULTS, nocturnal=True, lid=True))

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
              patch.object(Path, "glob", return_value=[]),
              patch.object(Path, "iterdir", return_value=[])):
            self.assertFalse(resources.check(dict(DEFAULTS, battery=True), running=True))
            self.assertIn("PSI · 5440 Mio", resources.memory_detail)
            self.assertIn("1.12 %", resources.memory_detail)
            resources.sample = 0
            readings.update({"/proc/meminfo": "MemAvailable: 1310720 kB",
                             "/proc/pressure/memory": "some avg10=0.29 avg60=0.10 avg300=0.10 total=4\nfull avg10=0.10 avg60=0.10 avg300=0.10 total=4"})
            self.assertFalse(resources.check(dict(DEFAULTS, battery=True), running=True))
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
