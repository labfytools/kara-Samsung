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
