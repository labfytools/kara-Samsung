"""Synthetic fixtures for provenance, abstention and immutable BlackBox replay."""
import json
import os
import runpy
from types import SimpleNamespace
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox, digest
from _kara_blackbox_chronos import differences, replay
from _kara_blackbox_lyrics import record, snapshot
from _kara_blackbox_lyric_clock import SECOND, anchors, chapter_markers
from _kara_chronos import cached, calculate, display_index, implementation, prepare, reference_lines
from _kara_chronos_policy import support
from _kara_chronos_worker import rows_for, window_words


def alignment():
    admitted = {"start": 1.2, "end": 1.4, "lexical": True, "acoustic": False,
                "phones": [["h", 1.2]]}
    refused = dict(admitted, start=1.5, end=1.9, lexical=False, phones=[["n", 1.5]])
    return {"engine": "fixture", "lines": rows_for(["Hello night", "Again"],
                                                   [admitted, refused, None], 5)}


class ChronosTests(unittest.TestCase):
    def test_abstention_keeps_text_offsets_and_proposal(self):
        lines = alignment()["lines"]
        self.assertEqual([line["text"] for line in lines], ["Hello night", "Again"])
        self.assertEqual(display_index(lines), ([1.2], [0]))
        self.assertIsNone(lines[0]["words"][1]["start"])
        self.assertEqual(lines[0]["words"][1]["proposal"]["start"], 1.5)
        self.assertEqual(lines[1]["status"], "absent")
        self.assertIsNone(lines[1]["start"])

    def test_lexical_support_requires_time_confidence_and_word(self):
        marks = [{"start": 2}, {"start": 3}, {"start": 4}]
        heard = [("hello", 2.2, .8), ("night", 3, .1), ("again", 5, .9)]
        self.assertEqual(support(["Hello", "night", "again"], marks, heard), [True, False, False])

    def test_source_order_and_untimed_input(self):
        text = "\ufeff[00:12]One\n[00:08]Two"
        self.assertEqual(reference_lines({"original": text, "timed": True, "order": "file"}), ["One", "Two"])
        self.assertEqual(reference_lines({"original": text, "timed": True}), ["Two", "One"])
        self.assertEqual(reference_lines({"original": "\ufeffOne\n\nTwo", "timed": False}), ["One", "Two"])

    def test_partial_chronos_uses_lrc_without_altering_the_acoustic_evidence(self):
        from _kara_chronos import display_receipt, playback_lines

        # The late proposal for line three belongs outside that line's
        # source interval. The earlier line still keeps its observed onset.
        raw = [
            {"start": 1.2, "text": "One", "status": "experimental"},
            {"start": None, "text": "Two", "status": "absent"},
            {"start": 6.9, "text": "Three", "status": "experimental"},
            {"start": None, "text": "Four", "status": "absent"},
        ]
        source = {"timed": True, "original": "[00:01]One\n[00:03]Two\n[00:05]Three\n[00:07]Four"}
        receipt = {"engine": "huang", "status": "experimental", "source": source, "duration": 8., "lines": raw}
        view = display_receipt(receipt)
        self.assertIs(view["lines"], raw)
        self.assertEqual(display_index(playback_lines(view)), ([1.2, 3., 5., 7.], [0, 1, 2, 3]))
        self.assertEqual([line["status"] for line in playback_lines(view)],
                         ["experimental", "source-timing", "source-timing", "source-timing"])
        changed = dict(receipt, source={**source, "original": "[00:01]Other edition"})
        self.assertIs(display_receipt(changed), changed)

    def test_project_uses_measured_clock_and_seek_breaks_continuity(self):
        turn = "4" * 16
        def clock(t, pos):
            return {"event": "music-clock", "component": "audio", "turn": turn, "position": pos,
                    "monotonic_ns": t * SECOND, "data": {"playback_rate": 1, "strawberry_monotonic_ns": t * SECOND}}
        rows = [clock(10, 1), clock(11, 2), {"event": "seek", "turn": turn, "monotonic_ns": 12 * SECOND},
                clock(12, 1), clock(13, 2)]
        intervals = list(anchors(rows, turn, 13 * SECOND))
        self.assertEqual(intervals, [(10 * SECOND, 11 * SECOND, 1, 2), (12 * SECOND, 13 * SECOND, 1, 2)])
        markers = list(chapter_markers(alignment()["lines"], intervals, 10 * SECOND, 11 * SECOND))
        self.assertEqual([row["event"] for row in markers], ["line", "word-start", "word-end", "phone", "abstention", "phone"])
        self.assertEqual(markers[1]["monotonic_ns"], 10_200_000_000)

    def test_differences_require_identical_text_and_never_label_refusals_as_delays(self):
        current = alignment()
        alternate = alignment()
        alternate["lines"][0]["words"][0]["start"] = 1.28
        change = differences(current, alternate)
        self.assertEqual(change["lines"][0]["words"][0]["delta_ms"], 80)
        alternate["lines"][0]["text"] = "Different words"
        self.assertEqual(differences(current, alternate)["state"], "lyrics-differ")

    def test_final_lexical_window_uses_real_audio_extent(self):
        wire = [b"segment 0 2000 0.1", b"token 80 0.9 48656c6c6f"]
        result = list(window_words(wire, 25., 2.))
        self.assertEqual(result[0].text, "hello")
        self.assertAlmostEqual(result[0].start, 25.8)
        self.assertLessEqual(result[0].end, 27.)

    def test_generation_reuses_exact_input_and_never_creates_lrc(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            song = root / "sound.mp3"
            song.write_bytes(b"fixture")
            original = song.with_suffix(".txt")
            original.write_text("Hello night")
            cache = SimpleNamespace(cached=lambda *args, **kwargs: None)
            result = {**alignment(), "engine": "huang", "version": 1,
                      "status": "experimental", "implementation": implementation()}
            with patch("_kara_chronos.ROOT", root / "cache"):
                with patch("_kara_chronos.calculate", return_value=result) as calculate:
                    self.assertEqual(prepare(song, cache, "huang"), "experimental")
                    self.assertEqual(prepare(song, cache, "huang"), "experimental")
                    self.assertEqual(calculate.call_count, 1)
                    self.assertIsNotNone(cached(song, "huang"))
                    original.write_text("Editor changed the words")
                    self.assertIsNone(cached(song, "huang"))
            self.assertFalse(song.with_suffix(".lrc").exists())

    def test_night_request_caches_private_text_and_cannot_enable_demucs_engine(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            song = root / "sound.flac"
            song.write_bytes(b"fixture")
            source = {"kind": "night-plain", "provider": "fixture", "original": "Hello night", "timed": False}
            result = {**alignment(), "engine": "huang", "version": 1,
                      "status": "experimental", "implementation": implementation()}
            with patch("_kara_chronos.ROOT", root / "cache"):
                with patch("_kara_chronos.calculate", return_value=result) as execution:
                    prepare(song, None, "huang", supplied=source, allow_demucs=False)
                    execution.assert_called_once_with(song, source, "huang", allow_demucs=False)
                    self.assertFalse(cached(song, "huang")["separation_allowed"])
                    prepare(song, None, "huang", supplied=source)
                    self.assertEqual(execution.call_count, 2)
            self.assertFalse(song.with_suffix(".lrc").exists())
            with self.assertRaises(ValueError):
                calculate(song, source, "wav2vec2", allow_demucs=False)

    def test_party_relaxes_only_the_playback_gate(self):
        from _parole_process import Interrupted

        def guarded_run(_args, *, stop, **_kwargs):
            if stop():
                raise Interrupted
            return 17, b"failure"

        with TemporaryDirectory() as directory, patch("_kara_chronos.ROOT", Path(directory)), patch.dict(
                os.environ, {"KARA_CHRONOS_PYTHON": sys.executable}), patch(
                "_kara_chronos.run", side_effect=guarded_run), patch("_kara_chronos.guard") as guard:
            guard.side_effect = lambda _path, require_quiet: "Lecture en cours" if require_quiet else "Réserve mémoire"
            with self.assertRaisesRegex(RuntimeError, "Lecture en cours"):
                calculate("audio", {}, "huang")
            with self.assertRaisesRegex(RuntimeError, "Réserve mémoire"):
                calculate("audio", {}, "huang", for_party=True)
            self.assertEqual([call.kwargs for call in guard.call_args_list],
                             [{"require_quiet": True}, {"require_quiet": False}])

    def test_engine_diagnostics_preserve_the_inner_failure_and_exit_code(self):
        from _kara_preparation_error import error_details

        def failed_run(_args, *, event, **kwargs):
            self.assertTrue(kwargs["merge_stderr"])
            event({"event": "chronos-phase", "phase": "separation"})
            event({"event": "chronos-error", "phase": "separate", "message": "Erreur exacte\nDétail",
                   "exit_code": 17, "protection": None})
            event({"event": "chronos-error", "phase": "separation", "message": "Erreur enveloppante",
                   "exit_code": 1, "protection": None})
            return 1, b"systemd log"

        with TemporaryDirectory() as directory, patch("_kara_chronos.ROOT", Path(directory)), patch.dict(
                os.environ, {"KARA_CHRONOS_PYTHON": sys.executable}), patch(
                "_kara_chronos.run", side_effect=failed_run):
            with self.assertRaises(RuntimeError) as failed:
                calculate("audio", {}, "huang")
        self.assertEqual(error_details(failed.exception, "chronos"), {
            "phase": "separate", "message": "Erreur exacte\nDétail", "exit_code": 17, "protection": None})

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "requires GTK")
    def test_async_preparation_writes_exact_blackbox_diagnostics(self):
        from gi.repository import Gio, GLib
        from _kara_blackbox import events
        from types import MethodType

        executable = Path(__file__).resolve().parents[1] / "bin/paroles-strawberry"
        window = runpy.run_path(str(executable), run_name="kara_preparation_diagnostics")["LyricsWindow"]
        with TemporaryDirectory() as directory:
            song = Path(directory) / "fixture.opus"
            box = BlackBox(Path(directory) / "blackbox")
            box.start(consent=True)
            session = box.status()["session"]
            records = [
                {"event": "chronos", "path": str(song), "engine": "huang", "status": "unavailable",
                 "phase": "admission", "message": "Réserve mémoire", "exit_code": None,
                 "protection": "Réserve mémoire"},
                {"event": "diagnostic", "path": str(song), "engine": "turbo", "phase": "vocal_refinement",
                 "message": "Le détail exact\nde l’erreur.", "exit_code": 7, "protection": None},
            ]
            process = Gio.Subprocess.new(
                [sys.executable, "-c", "import sys; print(sys.argv[1]); print(sys.argv[2], file=sys.stderr); sys.exit(9)",
                 "\n".join(map(json.dumps, records)), "Diagnostic du processus"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE)
            loop = GLib.MainLoop()
            subject = SimpleNamespace(blackbox=box, closed=False, align_job=process, track=None,
                                      active_plan={song: "Fixture"}, align_finished=set(),
                                      align_diagnostics="", chronos_reports={}, batch_failed={},
                                      batch_protected={}, stem_queue={}, stem_failed=set(), preload_cancelled=False,
                                      batch_experimental=set(), training_songs=set(), start_stem_generation=loop.quit)
            for name in ("read_alignment", "alignment_exit", "log_preparation_error"):
                setattr(subject, name, MethodType(getattr(window, name), subject))
            subject.preparation_status_code = window.preparation_status_code
            stream = Gio.DataInputStream.new(process.get_stdout_pipe())
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, subject.read_alignment, process)
            deadline = GLib.timeout_add_seconds(5, lambda: (loop.quit(), GLib.SOURCE_CONTINUE)[1])
            loop.run()
            GLib.source_remove(deadline)
            box.stop(wait=True)
            rows = list(events(box.root / session / "events.jsonl"))
            errors = {(row["component"], row["data"]["phase"]): row["data"] for row in rows
                      if row["event"] == "error"}
            self.assertEqual(errors["chronos", "admission"]["protection"], "Réserve mémoire")
            self.assertIsNone(errors["chronos", "admission"]["exit_code"])
            self.assertEqual(errors["turbo", "vocal_refinement"]["message"], "Le détail exact\nde l’erreur.")
            self.assertEqual(errors["turbo", "vocal_refinement"]["exit_code"], 7)
            self.assertEqual(errors["turbo", "worker"]["message"], "Diagnostic du processus\n")
            self.assertEqual(errors["turbo", "worker"]["exit_code"], 9)
            self.assertIsNone(subject.align_job)

    def test_timed_source_survives_engine_and_score_failures_without_duplicate_work(self):
        from _kara_chronos import playback_experiment, playback_lines
        from _kara_prepare import PreparedCache, Preparer, prepare_chronos_track

        with TemporaryDirectory() as directory:
            root = Path(directory)
            song = root / "audio.flac"
            song.write_bytes(b"audio fixture")
            preparer = Preparer(PreparedCache(root / "prepared"))
            events = []
            def send(event, _song, **data):
                events.append((event, data))

            # The Enhanced LRC is retained even if the guarded model never starts.
            lrc = b"[00:01.00]<00:01.00>Hello <00:01.40>night\n[00:03.00]Again"
            options = dict(for_party=True, progress=lambda *_: None, send=send)
            with patch("_kara_chronos.ROOT", root / "chronos"), patch(
                    "_kara_stems.duration", return_value=5.) as probe, patch(
                    "_musique_oz.probe", return_value=({}, [])), patch(
                    "_parole_lyrics_lookup.playback_candidates", return_value=[]), patch.object(
                    preparer, "reader", return_value=(lrc, "LRCLIB")) as reader, patch(
                    "_kara_chronos.calculate", side_effect=RuntimeError("Protection mémoire")) as engine, patch.object(
                    preparer, "prepare", side_effect=RuntimeError("Erreur exacte de référence")) as score:
                self.assertEqual(prepare_chronos_track(song, preparer, "huang", force=False, **options), (False, True))
                score.assert_not_called()
                self.assertEqual(prepare_chronos_track(song, preparer, "huang", force=True, **options), (False, True))
                score.assert_called_once()
                reader.assert_called_once_with(song)
                probe.assert_called_once_with(song)
                self.assertEqual(engine.call_count, 2)
                self.assertEqual(events[-1][1]["report"]["message"], "Erreur exacte de référence")
                playback = playback_experiment(song, preparer.cache, "huang")
                self.assertEqual(playback["engine"], "LRC")
                self.assertEqual(playback["source"]["provider"], "LRCLIB")
                self.assertEqual(display_index(playback_lines(playback)), ([1., 3.], [0, 1]))
                self.assertEqual([line["text"] for line in playback_lines(playback)], ["Hello night", "Again"])
                song.with_suffix(".lrc").write_text("[00:02]An edition from a human")
                self.assertIsNone(playback_experiment(song, preparer.cache, "huang"))
            self.assertFalse(preparer.cache.folder(song).exists())

    def test_generated_draft_uses_private_reference_and_stays_outside_score(self):
        from _kara_chronos import cache_path, playback_experiment, source_for
        from _kara_prepare import PreparedCache, file_token
        from _parole_transcribe import NAME

        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            song = root / "sound.flac"
            song.write_bytes(b"fixture")
            cache = PreparedCache(root / "prepared")
            manifest = cache.folder(song) / NAME
            manifest.parent.mkdir(parents=True)
            draft = {"version": 2, "audio": {"file": file_token(song), "sha256": digest(song)}, "duration": 5.,
                     "plain": "Hello night", "timed": "[00:01.00]Hello night", "omitted": 1}
            manifest.write_text(json.dumps(draft))
            with patch("_kara_chronos.ROOT", root / "chronos"), patch.object(cache, "cached", return_value=None), patch(
                    "_musique_oz.probe", return_value=({}, [])):
                self.assertEqual(source_for(song, cache)["kind"], "unavailable")
                fallback = playback_experiment(song, cache, "huang")
                self.assertEqual(fallback["source"]["kind"], "generated-transcription")
                self.assertEqual(display_index(fallback["lines"]), ([1.0], [0]))
                self.assertEqual(fallback["lines"][0]["words"], [])
                self.assertIsNone(playback_experiment(song, cache, ""))
                source = source_for(song, cache, include_draft=True)
                result = {**alignment(), "source": source, "engine": "huang", "version": 1,
                          "audio_sha256": digest(song), "status": "experimental", "implementation": implementation(),
                          "duration": 5.}
                with patch("_kara_chronos.calculate", return_value=result) as calculate:
                    prepare(song, cache, "huang", supplied=source, allow_demucs=False)
                    self.assertFalse(calculate.call_args.kwargs["allow_demucs"])
                    self.assertEqual(playback_experiment(song, cache, "wav2vec2")["engine"], "huang")
                failed = {**result, "status": "abstained",
                          "lines": rows_for(["Hello night"], [None, None], 5.)}
                path = cache_path(song, "huang")
                saved = json.loads(path.read_text())
                path.write_text(json.dumps(saved | failed))
                self.assertEqual(playback_experiment(song, cache, "huang")["engine"], "Turbo")
                draft["timed"] = "[00:02.00]Hello night"
                manifest.write_text(json.dumps(draft))
                self.assertIsNone(cached(song, "huang", cache=cache))
                self.assertEqual(playback_experiment(song, cache, "huang")["engine"], "Turbo")
                self.assertFalse(song.with_suffix(".lrc").exists())
                song.with_suffix(".txt").write_text("A corrected reference")
                self.assertIsNone(playback_experiment(song, cache, "huang"))

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "requires GTK")
    def test_actual_kara_display_preserves_score_clock_and_uses_only_observed_onsets(self):
        executable = Path(__file__).resolve().parents[1] / "bin/paroles-strawberry"
        window = runpy.run_path(str(executable), run_name="kara_chronos_fixture")["LyricsWindow"]
        document = alignment()
        subject = SimpleNamespace(times=[.5, 4.], view_lyrics=["Hello night", "Again"],
                                  chronos_reference=document, chronos_times=[1.2], chronos_indices=[0])
        self.assertEqual(window.view_anchor(subject, .8), -1)
        self.assertEqual(window.view_anchor(subject, 4.), 0)
        self.assertEqual(window.display_progress(subject, 2., 0), 5 / 11)
        self.assertEqual(subject.times, [.5, 4.])

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "requires GTK")
    def test_experimental_plot_and_capture_preserve_original_mix_and_no_score(self):
        from _kara_blackbox_lyrics import load

        executable = Path(__file__).resolve().parents[1] / "bin/paroles-strawberry"
        window = runpy.run_path(str(executable), run_name="kara_generated_fixture")["LyricsWindow"]
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            song = root / "audio"
            song.write_bytes(b"fixture")
            box = BlackBox(root / "blackbox")
            box.start(consent=True)
            session = box.status()["session"]
            context = {"turn": "b" * 16, "player": "Fixture", "song": "Fixture"}
            experiment = {"engine": "Turbo", "audio_sha256": digest(song),
                          "lines": [{"start": 1., "text": "Hello night", "words": []}],
                          "source": {"kind": "generated-transcription", "original": "Hello night",
                                     "transcription": {"status": "experimental", "reliability": "partial"}}}
            player = SimpleNamespace(song=SimpleNamespace(path=song))
            queue = SimpleNamespace(submit=lambda work: work())
            calls = []
            subject = SimpleNamespace(blackbox=box, game_score=SimpleNamespace(diagnostic_turn=context),
                                      scoring_source=lambda: None, active_reference=None, tag_text="",
                                      chronos_reference=experiment, tag_reader=queue,
                                      music_volume=.5, turn_original_mix=True,
                                      room_audio=SimpleNamespace(enable_return=lambda: None),
                                      guide_intervals=[], apply_player_mix=lambda player: None,
                                      vocal_effect=SimpleNamespace(disable=lambda: None),
                                      player_command=lambda name, *args: calls.append(name),
                                      write_music_volume=lambda *_a, **_kw: self.fail("Strawberry must stay audible"),
                                      refresh_game=lambda: None)
            window.begin_playback(subject, player)
            self.assertEqual(calls, ["OpenUri"])
            with patch.dict(window.log_playback_reference.__globals__, {"LIBRARY": root}):
                window.log_playback_reference(subject, song)
            box.stop(wait=True)
            item = load(box.root / session, context["turn"])["reference"]
            self.assertIsNone(item["historical"])
            self.assertEqual(item["source"], "generated-transcription")
            self.assertEqual(item["transcription"]["reliability"], "partial")
            self.assertEqual(item["displayed"], [[1., "Hello night"]])

    def test_existing_capture_replay_and_historical_score_invariance(self):
        with TemporaryDirectory() as directory:
            base = Path(directory)
            original = base / "audio"
            original.write_bytes(b"exact fixture")
            box = BlackBox(base / "blackbox")
            box.start(consent=True)
            context = {"turn": "b" * 16, "player": "Fixture", "song": "Fixture"}
            session = box.status()["session"]
            folder = box.root / session
            box.emit("party", "turn-start", **context)
            document = snapshot({"original": {"sha256": digest(original)}}, "sidecar-text", "Hello night\nAgain", [])
            document["chronos"] = {**alignment(), "source": {"kind": "sidecar-text", "timed": False,
                                                               "original": "Hello night\nAgain"}}
            record(box, session, context, document)
            box.emit("score", "line-decision", **context, line=0, tolerance=.3, source="voice",
                     reference={"words": [{"id": 0, "normalized": "hello", "text": "Hello", "start": 1.2},
                                          {"id": 1, "normalized": "night", "text": "night", "start": 1.5}]},
                     observed=[{"word": "hello", "confidence": .9, "start": 1.25}])
            box.emit("party", "turn-end", **context, summary={"score": 75})
            box.stop(wait=True)
            logged = digest(folder / "events.jsonl")
            output = base / "new.json"
            with patch("_kara_blackbox_chronos.calculate", return_value=alignment()) as calculate:
                report = replay(folder, context["turn"], "huang", explicit=original, output=output)
                self.assertEqual(calculate.call_args.kwargs["expected_audio"], digest(original))
                with self.assertRaisesRegex(ValueError, "nouvelle sortie"):
                    replay(folder, context["turn"], "huang", explicit=original, output=output)
            self.assertEqual(digest(folder / "events.jsonl"), logged)
            self.assertEqual(json.loads(output.read_text())["events_sha256"], logged)
            self.assertEqual(report["score_replay"]["historical"], {"score": 75})
            self.assertEqual(report["score_replay"]["comparison"]["eligible"], 1)
            self.assertEqual(report["score_replay"]["comparison"]["correct"], 1)


if __name__ == "__main__":
    unittest.main()
