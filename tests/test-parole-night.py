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
from _kara_turbo import ObservedWord
from _parole_index import document, inventory, sidecar
from _parole_maintenance import Maintenance
from _parole_night import Desktop, Login, Night, mains_online, POWER_LOSS_GRACE, POWER_RECOVERY_STABLE
from _parole_transcribe import proposals, review_ranges


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
        self.assertEqual(self.night.lid_fd, fd)
        self.night.power_loss_since -= POWER_LOSS_GRACE + 1
        self.assertFalse(self.night.refresh(self.settings))
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertIsNone(self.night.fd)
        self.ac = True
        self.assertFalse(self.night.check(self.settings))
        self.assertIsNone(self.night.lid_fd)
        self.night.power_online_since -= POWER_RECOVERY_STABLE + 1
        self.assertTrue(self.night.check(self.settings))
        self.assertIsNotNone(self.night.lid_fd)

    def test_closed_ac_flicker_stops_work_and_retains_inhibitor(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.open()
        self.assertTrue(self.observe(lid=True))
        fd, budget = self.night.lid_fd, self.night.budget
        self.ac = False
        self.assertFalse(self.night.refresh(self.settings, running=True))
        self.assertFalse(self.night.release_after_work)
        self.night.stop_work()
        self.assertEqual(self.night.lid_fd, fd)
        self.assertIsNone(self.night.delay)
        self.assertIsNone(self.owner.index.closed_progress()["ended"])
        self.ac = True
        self.assertFalse(self.night.check(self.settings))
        self.night.power_recovery_since -= POWER_RECOVERY_STABLE + 1
        self.assertTrue(self.night.check(self.settings))
        self.assertEqual(self.night.lid_fd, fd)
        self.assertEqual(self.night.budget, budget)
        self.assertIsNone(self.night.power_loss_since)

    def test_repeated_unstable_power_does_not_extend_grace(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.open()
        self.assertTrue(self.observe(lid=True))
        self.ac = None
        self.assertFalse(self.night.check(self.settings))
        started = self.night.power_loss_since
        self.ac = True
        self.assertFalse(self.night.check(self.settings))
        self.ac = False
        self.assertFalse(self.night.check(self.settings))
        self.assertEqual(self.night.power_loss_since, started)
        self.night.power_loss_since -= POWER_LOSS_GRACE + 1
        self.assertFalse(self.night.check(self.settings))
        self.assertIsNone(self.night.lid_fd)

    def test_manual_sleep_overrides_power_grace(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.open()
        self.assertTrue(self.observe(lid=True))
        self.ac = False
        self.assertFalse(self.night.check(self.settings))
        self.assertIsNotNone(self.night.lid_fd)
        self.assertFalse(self.observe(sleeping=True))
        self.assertIsNone(self.night.lid_fd)

    def test_open_lid_battery_releases_immediately(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.assertTrue(self.night.refresh(self.settings))
        self.ac = False
        self.assertFalse(self.night.refresh(self.settings))
        self.assertIsNone(self.night.lid_fd)

    def test_restart_closed_on_ac_waits_for_stability_and_lock(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.state = Desktop(True, False, False, True, False)
        self.assertFalse(self.night.refresh(self.settings))
        self.assertIsNone(self.night.lid_fd)
        self.night.power_online_since -= POWER_RECOVERY_STABLE + 1
        self.assertFalse(self.night.refresh(self.settings))
        self.assertIsNotNone(self.night.lid_fd)
        self.login.request_lock.assert_called_once()
        self.assertFalse(self.observe(locked=True))
        self.login.display.assert_called_once_with(False)
        self.assertTrue(self.observe(screen_off=True))

    def test_closed_rearm_never_bypasses_manual_sleep_or_expired_budget(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.state = replace(self.state, lid=True, sleeping=True)
        self.assertFalse(self.night.refresh(self.settings))
        self.assertIsNone(self.night.lid_fd)
        self.assertFalse(self.observe(sleeping=False))
        self.night.power_online_since -= POWER_RECOVERY_STABLE + 1
        self.night.budget = 1.
        self.settings["night_minutes"] = 1
        with patch("_parole_night.clock_gettime", return_value=10000.):
            self.assertFalse(self.night.refresh(self.settings))
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

    def test_protected_closure_restores_display_after_battery_sleep(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.state = replace(self.state, screen_off=False)
        self.open()
        self.assertFalse(self.observe(lid=True))
        self.login.display.assert_called_once_with(False)
        self.assertTrue(self.observe(screen_off=True))
        self.ac = False
        self.assertFalse(self.observe(sleeping=True))
        self.assertIsNone(self.night.lid_fd)
        self.assertFalse(self.observe(lid=False))
        self.assertEqual(self.login.display.call_count, 1)
        self.assertFalse(self.observe(sleeping=False))
        self.night.open_since -= 3
        self.night.next_display = 0
        with patch("_parole_night.acpi_lid_closed", return_value=False):
            self.assertFalse(self.observe(lid=False))
        self.login.display.assert_called_with(True)
        self.assertIsNone(self.night.lid_fd)
        self.assertFalse(self.observe(screen_off=False))
        self.assertFalse(self.night.restore_display)

    def test_expiry_releases_lid_even_when_no_work_is_due(self):
        self.settings.update(night_lid=True, night_lid_qualified=True)
        self.assertTrue(self.night.refresh(self.settings))
        fd = self.night.lid_fd
        self.assertTrue(self.observe(lid=True))
        self.night.budget = 1.
        self.settings["night_minutes"] = 1
        with patch("_parole_night.clock_gettime", return_value=10000.):
            self.assertFalse(self.night.refresh(self.settings))
        with self.assertRaises(OSError):
            os.fstat(fd)
        self.assertIsNone(self.night.fd)

    def test_unlimited_night_survives_48_hours_without_bypassing_power(self):
        self.settings.update(night_lid=True, night_lid_qualified=True, night_minutes=0)
        self.open()
        self.assertTrue(self.observe(lid=True))
        fd = self.night.lid_fd
        # Simulate more than two days of elapsed boot time, without sleeping.
        self.night.budget = 1.
        with patch("_parole_night.clock_gettime", return_value=49 * 3600.):
            self.assertTrue(self.night.check(self.settings))
            self.assertEqual(self.night.lid_fd, fd)
            self.ac = False
            self.assertFalse(self.night.check(self.settings))
            self.assertEqual(self.night.lid_fd, fd)
            self.night.power_loss_since -= POWER_LOSS_GRACE + 1
            self.assertFalse(self.night.check(self.settings))
            self.assertIsNone(self.night.lid_fd)

    def test_budget_survives_restart_and_real_resume_resets_it(self):
        self.open()
        self.night.close()
        self.owner.index.night_budget(1.)
        self.night = self.owner.night = Night(self.owner.index, self.owner.journal)
        self.night.login = self.login
        self.addCleanup(self.night.close)
        self.settings["night_minutes"] = 1
        with patch("_parole_night.clock_gettime", return_value=10000.):
            self.assertFalse(self.night.check(self.settings))
            self.assertFalse(self.night.open(self.settings))
        self.owner.resources.resumed = True
        self.assertTrue(self.owner.night_allowed(self.settings, running=False))
        self.assertIsNone(self.owner.index.night_budget())
        self.assertTrue(self.night.open(self.settings))

    def test_night_never_claims_legacy_alignment_or_automatic_drafts(self):
        index = self.owner.index
        for kind in ("prepare", "transcription"):
            index.enqueue(kind, kind, "fixture", now=1, payload={"manual": True})
        self.owner.prepare = Mock()
        self.owner.transcribe_task = Mock()
        self.assertFalse(self.owner.tick_night(self.settings, time()))
        self.owner.prepare.assert_not_called()
        self.owner.transcribe_task.assert_not_called()
        with index.db() as db:
            self.assertEqual([tuple(row) for row in db.execute(
                "SELECT kind,state,attempts FROM tasks ORDER BY kind")],
                [("prepare", "queued", 0), ("transcription", "queued", 0)])

    def missing_tracks(self, *names):
        index = self.owner.index
        paths = [index.root / (name + ".opus") for name in names]
        for path in paths:
            path.write_bytes(b"fixture")
        inventory(index, None, lambda *_: ("", "absent", {}), lambda: False, lambda *_: None,
                  paths=paths, reader=lambda path: ({"title": path.stem, "artist": "Artiste",
                                                       "duration_seconds": 5}, {}))
        with index.db() as db:
            return dict(db.execute("SELECT title,id FROM songs"))

    def test_closed_background_search_persists_completed_tasks(self):
        self.owner.foreground.clear()
        index = self.owner.index
        index.configure(night_lid=True, night_lid_qualified=True, network_workers=1)
        self.settings = index.settings()
        self.night.refresh(self.settings)
        self.assertTrue(self.observe(lid=True))
        tracks = self.missing_tracks("slow", "first", "second")
        self.owner.output = Mock(side_effect=(TimeoutError(), *((
            0, b'{"state":"found","source":"LRCLIB"}'),) * 2))
        for i, subject in enumerate(("slow", "first", "second"), 1):
            index.enqueue("lrc", tracks[subject], "fixture", now=i)
        for kind in ("lyrics", "cover", "prepare", "transcription", "codex"):
            index.enqueue(kind, tracks["slow"], "fixture", now=1)
        for _ in range(4):
            self.owner.tick()
        report = index.closed_progress()
        self.assertEqual(report["completed"], {"lrc": 2})
        self.assertEqual(index.night_lrc_status()["retrieved"], 2)
        self.assertTrue(all("--original-lrc" in call.args[0] for call in self.owner.output.call_args_list))
        with index.db() as db:
            pending = {row[0] for row in db.execute("SELECT kind FROM tasks WHERE state='queued'")}
        self.assertTrue({"lyrics", "cover", "prepare", "transcription", "codex"} <= pending)
        self.assertIsNone(report["ended"])
        self.ac = False
        self.owner.tick()
        self.assertIsNone(index.closed_progress()["ended"])
        self.night.power_loss_since -= POWER_LOSS_GRACE + 1
        self.owner.tick()
        self.assertEqual(index.closed_progress()["reason"], "secteur requis")
        self.assertIsNone(self.night.lid_fd)

    def test_closed_night_waits_for_provider_then_caches_negative_result(self):
        index = self.owner.index
        identity = self.missing_tracks("target")["target"]
        now = time()
        index.enqueue("lrc", identity, "fixture", now=now)
        self.owner.output = Mock(return_value=(0, b'{"state":"not_found"}'))
        with patch("_parole_maintenance.night_lrc_providers", return_value=[]):
            self.assertFalse(self.owner.tick_night(self.settings, now))
        self.owner.output.assert_not_called()
        with patch("_parole_maintenance.night_lrc_providers", return_value=[{"availability": "ready"}]):
            self.assertTrue(self.owner.tick_night(self.settings, now))
            self.assertFalse(self.owner.tick_night(self.settings, time()))
        self.owner.output.assert_called_once()
        with index.db() as db:
            row = db.execute("SELECT state,due FROM tasks WHERE subject=?", (identity,)).fetchone()
        self.assertEqual(row["state"], "temporary")
        self.assertGreater(row["due"], now + 60)

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

    def test_draft_filters_segments_without_corresponding_observed_words(self):
        heard = [ObservedWord("vrai", 1., 1.1, .9), ObservedWord("chant", 1.2, 1.4, .8)]
        parts = [dict(start=.9, end=2., text="Vrai chant", no_speech=.1),
                 dict(start=2.1, end=3.1, text="Paroles suggérées", no_speech=.1)]
        self.assertEqual(proposals(4., heard, parts), ("Vrai chant", "[00:01.000]Vrai chant", 1))
        draft = {"duration": 4., "language": "fr", "audio": {"sha256": "fixture"},
                 "model": {}, "settings": {}, "observation": {"input": "original", "acceleration": "vulkan"},
                 "first_pass": {"language_request": "", "words": [[w.text, w.start, w.end, w.confidence, None]
                                                                  for w in heard],
                                "segments": [parts[0], *([parts[1]] * 25)]}}
        self.assertEqual(review_ranges(draft), ((2.1, 3.1, "Paroles suggérées"),) * 20)
        draft["first_pass"]["segments"] = [{"start": "invalid"}]
        self.assertEqual(review_ranges(draft), ())

    def test_distinct_second_reading_and_refrains_stay_experimental(self):
        from _parole_codex import validate_draft
        from _parole_transcribe import compare_windows, provenance, repeated_lines

        spans = [SimpleNamespace(no_speech=.1, words=[ObservedWord("autre", 2.2, 2.3, .9)])]
        decoder = SimpleNamespace(decode=Mock(return_value=("fr", spans)))
        parts = [dict(start=2., end=3., text="Vrai chant", no_speech=.1)]
        findings = compare_windows(decoder, lambda *_: b"pcm", (5., "fr", [], parts), lambda *_: None)
        self.assertEqual(findings, [dict(start=2., end=3., first="Vrai chant", alternate="autre",
                                         decision="divergent")])
        self.assertTrue(decoder.decode.call_args.kwargs["beam"])
        self.assertEqual(repeated_lines("Premier vers\nMême refrain\nPremier vers\nMême refrain"),
                         {"repeated_lines": [[1, 3], [2, 4]], "possible_choruses": [[1, 3]]})
        report = provenance({"plain": "Brouillon", "timed": "[00:01]Brouillon", "omitted": 1,
                             "comparisons": findings})
        self.assertEqual((report["status"], report["reliability"]), ("experimental", "partial"))
        query = {"research_id": "fixture", "ranges": [{"id": 0}]}
        suggestion = {"id": 0, "suggestion": "Un mot", "reason": "Proposition à écouter"}
        with self.assertRaises(ValueError):
            validate_draft({"research_id": "fixture", "suggestions": [suggestion, suggestion]}, query)
        self.assertEqual(validate_draft({"research_id": "fixture", "suggestions": [suggestion]}, query)
                         ["suggestions"], [suggestion])

    def test_real_subprocess_stops_on_power_loss_sleep_heat_space_and_kara(self):
        cases = ("secteur", "veille", "Kara", "activité", "désactivation")
        for phase in cases:
            with self.subTest(phase=phase):
                self.ac = True
                self.state = replace(self.state, sleeping=False)
                self.owner.set_priority(False, kara=False)
                self.owner.priority_until = 0.
                self.owner.resources.session_idle.return_value = True
                self.owner.resources.check.return_value = True
                self.owner.index.configure(night_enabled=True)
                self.owner.resources.resumed = False
                self.night.next_desktop = self.night.next_sleep = 0.
                fds = self.open()
                self.owner.index.enqueue("lrc", phase, "fixture")
                task = self.owner.index.claim(("lrc",), time())
                entered = Event()

                def lookup(*_args, **_kwargs):
                    self.owner.output([sys.executable, "-c", "import time; print('{}',flush=True); time.sleep(30)"],
                                      timeout=10, observer=lambda _event: entered.set())
                    return "done", "inattendu", 0

                self.owner.network_output = lookup
                thread = Thread(target=self.owner.execute, args=(task,), kwargs={"nocturnal": True})
                thread.start()
                try:
                    self.assertTrue(entered.wait(2))
                    if phase == "secteur":
                        self.ac = False
                    elif phase == "veille":
                        self.state = replace(self.state, sleeping=True)
                    elif phase in ("température", "stockage"):
                        self.owner.resources.reason = phase
                        self.owner.resources.check.return_value = False
                    elif phase == "Kara":
                        self.owner.set_priority(True, kara=True)
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
