#!/usr/bin/python3 -B
"""Qualification de l’admission sans manipuler la session ou le capot réels."""
from dataclasses import replace
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from threading import Event, Thread
from time import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_prepare import PreparedCache, file_token
from _kara_refine import VERSION as REFINEMENT_VERSION
from _parole_index import document, sidecar
from _parole_maintenance import Maintenance
from _parole_night import Desktop, Login, Night, mains_online


class PowerTests(unittest.TestCase):
    def test_system_ac_is_independent_from_charge_and_usb_output(self):
        with TemporaryDirectory() as temp:
            root = Path(temp)

            def device(name, kind, **properties):
                folder = root / name
                folder.mkdir()
                for key, value in dict(type=kind, **properties).items():
                    (folder / key).write_text(str(value))
                return folder

            device("BAT1", "Battery", capacity=82, status="Not charging")
            device("USB0", "USB", scope="Device", online=1)
            self.assertIsNone(mains_online(root))
            ac = device("ACAD", "Mains", online=0)
            self.assertIs(mains_online(root), False)
            (ac / "online").write_text("1")
            self.assertIs(mains_online(root), True)
            (root / "removed-during-read").mkdir()
            self.assertIs(mains_online(root), True)
            (ac / "online").write_text("?")
            self.assertIsNone(mains_online(root))

    def test_tlp_version_with_retained_holds_is_not_asked_to_hold(self):
        login = Login.__new__(Login)
        login.get = Mock(return_value="tlp-pd 1.11.0")
        self.assertIsNone(login.hold_economy())


class NightTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.owner = Maintenance(root, None, state=root / "state")
        self.addCleanup(self.owner.close)
        self.stems = root / "stems"
        stem_directory = patch("_parole_maintenance.STEM_ROOT", self.stems)
        stem_directory.start()
        self.addCleanup(stem_directory.stop)
        self.night = self.owner.night
        self.addCleanup(self.night.close)
        self.state = Desktop(True, True, True, False, False)
        self.ac = True
        power = patch("_parole_night.mains_online", side_effect=lambda: self.ac)
        power.start()
        self.addCleanup(power.stop)
        self.login = self.night.login = SimpleNamespace(
            inhibit=Mock(side_effect=lambda *_: os.open("/dev/null", os.O_RDONLY)),
            observe=lambda: self.state, sleeping=lambda: self.state.sleeping,
            request_lock=Mock(return_value=Mock(poll=Mock(return_value=0))), display=Mock(),
            hold_economy=Mock(return_value=object()), release_economy=Mock(), get=Mock(return_value="power-saver"),
            glib=SimpleNamespace(Error=RuntimeError))
        self.owner.index.configure(night_enabled=True, startup=False)
        self.settings = self.owner.current_settings = self.owner.index.settings()
        self.owner.resources.session_idle = Mock(return_value=True)
        self.owner.resources.check = Mock(return_value=True)
        self.owner.resources.collect_storage = Mock()
        self.owner.resources.storage = {"bytes": 100}

    def open(self):
        self.assertTrue(self.owner.night_allowed(self.settings, running=False))
        self.assertTrue(self.night.open(self.settings))
        return self.night.fd, self.night.delay

    def observe(self, **changes):
        self.state = replace(self.state, **changes)
        self.night.next_desktop = 0
        return self.night.check(self.settings)

    def test_lid_arms_with_no_work_before_lock_idle_or_closure(self):
        self.owner.index.configure(night_lid=True, night_lid_qualified=False)
        self.owner.foreground.clear()
        self.state = replace(self.state, locked=False, screen_off=False)
        self.owner.resources.session_idle.return_value = False
        self.owner.tick()
        self.login.inhibit.assert_not_called()
        self.owner.index.configure(night_lid_qualified=True)
        self.owner.tick()
        self.settings = self.owner.current_settings
        self.login.inhibit.assert_called_once_with("handle-lid-switch", "block")
        fd = self.night.lid_fd
        self.assertFalse(self.observe(lid=True))
        self.login.request_lock.assert_called_once()
        self.login.display.assert_not_called()
        self.assertFalse(self.observe(locked=True))
        self.login.display.assert_called_once_with(False)
        self.assertTrue(self.observe(screen_off=True))
        self.assertTrue(self.owner.night_allowed(self.settings, running=False))
        self.assertEqual(self.night.lid_fd, fd)
        self.ac = False
        self.assertFalse(self.night.refresh(self.settings))
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertIsNone(self.night.fd)
        self.ac = True
        self.assertFalse(self.night.check(self.settings))
        self.assertIsNone(self.night.lid_fd)

    def test_ambiguous_opening_does_not_light_displays_or_reset_budget(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.open()
        self.assertTrue(self.observe(lid=True))
        budget, fd = self.night.budget, self.night.lid_fd
        self.state = replace(self.state, lid=False, sleeping=True)
        # Observe the reported edge separately from manual-sleep cleanup.
        self.night.observe(100.)
        self.assertTrue(self.night.desktop.lid)
        self.night.next_desktop = 0
        self.state = replace(self.state, lid=True, sleeping=False)
        self.assertTrue(self.night.check(self.settings))
        self.assertTrue(self.observe(lid=False))
        self.night.open_since -= 3
        with patch("_parole_night.acpi_lid_closed", return_value=True):
            self.assertTrue(self.observe(lid=False))
        self.login.display.assert_not_called()
        self.assertEqual(self.night.budget, budget)
        self.assertEqual(self.night.lid_fd, fd)
        self.assertNotIn(("sleep", "block"), [call.args for call in self.login.inhibit.call_args_list])
        self.night.next_display = 0
        with patch("_parole_night.acpi_lid_closed", return_value=False):
            self.assertFalse(self.observe(lid=False))
        self.login.display.assert_called_once_with(True)
        self.assertFalse(self.observe(screen_off=False))
        self.assertTrue(self.night.reopened)

    def test_expiry_releases_lid_even_when_no_work_is_due(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.assertTrue(self.night.refresh(self.settings))
        fd = self.night.lid_fd
        self.assertTrue(self.observe(lid=True))
        self.night.budget = 1.
        self.assertFalse(self.night.refresh(self.settings))
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertIsNone(self.night.fd)

    def test_budget_survives_restart_and_real_resume_resets_it(self):
        self.open()
        self.night.close()
        self.owner.index.night_budget(1.)
        self.night = self.owner.night = Night(self.owner.index, self.owner.journal)
        self.night.login = self.login
        self.addCleanup(self.night.close)
        self.assertFalse(self.night.check(self.settings))
        self.assertFalse(self.night.open(self.settings))
        self.owner.resources.resumed = True
        self.assertTrue(self.owner.night_allowed(self.settings, running=False))
        self.assertIsNone(self.owner.index.night_budget())
        self.assertTrue(self.night.open(self.settings))

    def test_serial_claim_passes_legacy_daily_limit_and_releases_at_end(self):
        self.owner.index.configure(heavy_daily=1)
        for subject in ("a", "b", "c", "d", "e"):
            self.owner.index.enqueue("prepare", subject, "fixture", now=1)
        self.owner.prepare = Mock(return_value=("done", "prêt", 0))
        for _ in range(5):
            self.assertTrue(self.owner.tick_night(self.settings, time()))
        self.assertEqual(self.owner.prepare.call_count, 5)
        self.assertIsNone(self.night.fd)
        self.assertEqual(len(self.login.inhibit.call_args_list), 2)
        with self.owner.index.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM daily_usage").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM tasks WHERE state='done'").fetchone()[0], 5)
        _, _, _, report = self.owner.index.night_progress()
        self.assertEqual(report["ready"], 5)
        self.assertEqual(report["cache_bytes"], 100)
        self.assertIsNone(report["pending"])

    def test_report_retains_validated_work_and_counts_a_crashed_claim_once(self):
        index = self.owner.index
        at = time()
        index.enqueue("prepare", "first", "fixture", now=at)
        task = index.claim(("prepare",), at, night=(10., 20))
        index.finish(task, "done", night=dict(seconds=30., outcome="ready", cache_bytes=42, corrected=1, generated=0))
        index.enqueue("prepare", "measured", "fixture", now=at)
        task = index.claim(("prepare",), at, night=(10., 42))
        measured = dict(seconds=15., outcome="ready", cache_bytes=62, corrected=0, generated=0,
                        measurements={"turbo": 6., "reprises": 2., "taille": 4096})
        index.finish(task, "done", night=measured)
        index.finish(task, "done", night=measured)
        index.enqueue("prepare", "second", "fixture", now=at)
        index.claim(("prepare",), at, night=(10., 62))
        self.assertTrue(index.recover())
        self.assertFalse(index.recover())
        _, _, _, report = index.night_progress()
        self.assertEqual((report["ready"], report["interrupted"], report["corrected"], report["seconds"]), (2, 1, 1, 45.))
        self.assertEqual((report["cache_baseline"], report["cache_bytes"]), (20, 62))
        self.assertEqual(report["measurements"], {"turbo": [6., 1], "reprises": [2., 1], "taille": [4096, 1]})
        self.assertIsNone(report["pending"])

    def test_cache_state_distinguishes_reference_without_stems_from_rejected_reference(self):
        root = self.owner.index.root
        path, model = root / "song.opus", root / "model"
        path.write_bytes(b"x" * 4096)
        model.touch()
        cache = self.owner.cache = PreparedCache(root / "prepared", model)
        folder = cache.folder(path)
        folder.mkdir(parents=True)
        (folder / "prepared.json").touch()
        item = {"refinement": {"version": REFINEMENT_VERSION},
                "report": {"word_coverage": .9, "line_coverage": .8}, "source": {}}
        with patch.object(cache, "cached", return_value=item):
            _, state, report = self.owner.inspect_cache(path, None, "", "")
            self.assertEqual(state, "partiel")
            self.assertTrue(report["reference_validated"])
            self.assertFalse(report["instrumental"])
        with patch.object(cache, "cached", side_effect=[None, item]):
            _, state, report = self.owner.inspect_cache(path, None, "", "")
            self.assertEqual(state, "partiel")
            self.assertFalse(report["reference_validated"])
            self.assertEqual(report["coverage"], .9)

    def test_recovery_removes_only_owned_drafts(self):
        stage = self.stems / "processing-interrupted"
        stage.mkdir(parents=True)
        (stage / ".parole-stage").touch()
        other = self.stems / "processing-other"
        other.mkdir()
        ready = self.stems / "valid"
        ready.mkdir()
        self.owner.recover_night_drafts()
        self.assertFalse(stage.exists())
        self.assertTrue(other.exists())
        self.assertTrue(ready.exists())

    def test_last_lrc_attempt_can_fall_back_to_local_text_and_is_reused(self):
        song = {"id": "fixture", "token": "audio", "side": "side", "title": "Chanson",
                "info": document({"plain": "Paroles locales vérifiées"})}
        self.owner.output = Mock(return_value=(0, b'{"state":"unavailable"}'))
        path = self.owner.index.root / "song.opus"
        result = self.owner.night_reference(song, path)
        self.assertEqual((result["mode"], result["lyrics"]), ("plain", "Paroles locales vérifiées"))
        self.assertEqual(self.owner.night_reference(song, path), result)
        self.owner.output.assert_called_once()

    def test_local_lrc_rejected_acoustically_gets_one_alternate_lookup(self):
        path = self.owner.index.root / "song.opus"
        path.write_bytes(b"fixture")
        path.with_suffix(".lrc").write_text("[00:04.000]Refrain")
        song = {"id": "fixture", "path": str(path), "token": document(file_token(path)), "title": "Chanson",
                "side": document([sidecar(path, suffix)[0] for suffix in (".lrc", ".txt")]), "lrc": "local",
                "duration": 25, "previous": None, "info": document({"plain": "Refrain"})}
        self.owner.index.song = Mock(return_value=song)
        self.owner.refresh_song = Mock()
        self.owner.resources.allowed_storage = Mock(return_value=True)
        negative = {}
        self.owner.cache.cached = Mock(side_effect=lambda *_, **kw: negative if kw.get("insufficient") and negative else None)

        def analyze(*_args, **_kwargs):
            negative["source"] = {"kind": "local"}
            return 1, b""

        self.owner.output = Mock(side_effect=analyze)
        self.owner.night_reference = Mock(return_value={"state": "not_found"})
        with patch("_parole_maintenance.cached_stems", return_value=(Path("a"), Path("b"))):
            self.assertEqual(self.owner.prepare({"subject": "fixture"}, nocturnal=True)[0], "temporary")
            self.assertEqual(self.owner.prepare({"subject": "fixture"}, nocturnal=True)[0], "review")
        self.owner.output.assert_called_once()
        self.owner.night_reference.assert_called_once_with(song, path)
        self.assertEqual(path.with_suffix(".lrc").read_text(), "[00:04.000]Refrain")

    def test_real_subprocess_stops_on_power_loss_sleep_heat_space_and_kara(self):
        cases = ("Demucs", "Turbo", "veille", "température", "stockage", "Kara", "activité", "désactivation")
        for phase in cases:
            with self.subTest(phase=phase):
                self.ac = True
                self.state = replace(self.state, sleeping=False)
                self.owner.priority_until = 0.
                self.owner.resources.session_idle.return_value = True
                self.owner.resources.check.return_value = True
                self.owner.index.configure(night_enabled=True)
                self.owner.resources.resumed = False
                self.night.next_desktop = self.night.next_sleep = 0.
                fds = self.open()
                self.owner.index.enqueue("prepare", phase, "fixture")
                task = self.owner.index.claim(("prepare",), time())
                entered = Event()

                def prepare(*_args, **_kwargs):
                    self.owner.output([sys.executable, "-c", "import time; print('{}',flush=True); time.sleep(30)"],
                                      timeout=10, observer=lambda _event: entered.set())
                    return "done", "inattendu", 0

                self.owner.prepare = prepare
                thread = Thread(target=self.owner.execute, args=(task,), kwargs={"nocturnal": True})
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    if phase in ("Demucs", "Turbo"):
                        self.ac = False
                    elif phase == "veille":
                        self.state = replace(self.state, sleeping=True)
                    elif phase in ("température", "stockage"):
                        self.owner.resources.reason = phase
                        self.owner.resources.check.return_value = False
                    elif phase == "Kara":
                        self.assertFalse(self.owner.yield_heavy())
                    elif phase == "activité":
                        self.owner.resources.resumed = True
                    else:
                        self.owner.index.configure(night_enabled=False)
                    thread.join(timeout=5)
                    self.assertFalse(thread.is_alive())
                    for fd in fds:
                        with self.assertRaises(OSError):
                            os.fstat(fd)
                    with self.owner.index.db() as db:
                        self.assertEqual(db.execute("SELECT state FROM tasks WHERE subject=?", (phase,)).fetchone()[0],
                                         "suspended")
                finally:
                    self.owner.interrupt.set()
                    thread.join(timeout=5)
                    self.owner.interrupt.clear()


if __name__ == "__main__":
    unittest.main()
