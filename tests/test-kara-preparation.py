"""Silent regressions for the production preparation and score paths."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
import wave

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

import _kara_prepare as prepare
from _kara_reference import alignment, align_words, lrc_text
from _kara_refine import Gap, consolidate, refine, two_observations
from _kara_scoring import prepared_window, score_breakdown, score_reference_line
from _kara_turbo import ObservedWord, decode_segment, transcribe_file
from _kara_words import word_boundaries
from _paroles_lrc import parse_lrc, source_orders

ROWS = [(10., "Voici l’été"), (14., "Les oiseaux chantent"), (19., "Un soleil rouge"),
        (24., "Les oiseaux chantent"), (30., "Dormez paisiblement")]


def observations(rows, shifts=None):
    if shifts is None:
        shifts = [0.] * len(rows)
    return [ObservedWord(word, stamp + shift + index * .42, stamp + shift + index * .42 + .38,
                         .94, "next_token")
            for (stamp, phrase), shift in zip(rows, shifts)
            for index, (word, *_chars) in enumerate(word_boundaries(phrase))]


class AlignmentTests(unittest.TestCase):
    def test_correct_lrc_and_individual_offsets(self):
        for shifts in [[0.] * 5, [.5] * 5, [5.] * 5, [0., .2, .7, 1.4, 2.],
                       [1.7, -.65, .4, -.3, 0.]]:
            with self.subTest(shifts=shifts):
                lines, report, ready = alignment(ROWS, observations(ROWS, shifts), 45.)
                self.assertTrue(ready)
                self.assertEqual(report["changed"], sum(abs(shift) >= .075 for shift in shifts))
                self.assertEqual(parse_lrc(lrc_text(lines)),
                                 [(round(t + shift, 3), text) for (t, text), shift in zip(ROWS, shifts)])
        self.assertEqual(lines[0]["words"][1]["text"], "l")
        self.assertEqual(lines[0]["words"][2]["text"], "été")
        self.assertEqual(lines[0]["words"][2]["normalized"], "ete")

    def test_refrains_and_instrumental_intro(self):
        heard = observations(ROWS, [3.] * 5)
        heard.insert(0, ObservedWord("instrumental", .5, 1., .6))
        lines, report, ready = alignment(ROWS, heard, 45.)
        self.assertTrue(ready)
        self.assertEqual([lines[i]["start"] for i in (1, 3)], [17., 27.])
        self.assertEqual(report["word_coverage"], 1.)

    def test_wrong_version_missing_line_and_recognition_error(self):
        _, _, ready = alignment(ROWS, observations([(1., "A completely different title")]), 45.)
        self.assertFalse(ready)
        heard = observations(ROWS[:2] + ROWS[3:])
        lines, report, ready = alignment(ROWS, heard, 45.)
        self.assertTrue(ready)
        self.assertEqual(report["uncertain_lines"], 1)
        self.assertIsNone(lines[2]["start"])
        self.assertTrue(all(word["start"] is None for word in lines[2]["words"]))
        heard = observations(ROWS)
        heard[4] = ObservedWord("moineaux", heard[4].start, None, .9)
        lines, report, ready = alignment(ROWS, heard, 45.)
        self.assertTrue(ready)
        self.assertEqual(report["uncertain_words"], 1)
        self.assertEqual(lines[1]["text"], ROWS[1][1])

    def test_ambiguous_single_observation_for_repeated_refrain(self):
        refs = [(3., "Silence musique"), (8., "Silence musique")]
        _, report, ready = alignment(refs, observations(refs[:1]), 30.)
        self.assertFalse(ready)
        self.assertEqual(report["matched_words"], 0)
        self.assertEqual(report["ambiguous_ranks"], 2)
        self.assertEqual(align_words(["a", "b", "a"], ["a", "a"]), ({0: 0, 2: 1}, 2, 0))

    def test_inverted_lrc_times_can_recover_source_line_order(self):
        bad_times = [35., 19., 5., 3., 1.]
        source = lrc_text([{"display_start": time, "text": row[1]} for time, row in zip(bad_times, ROWS)])
        order, lines, _, ready = prepare.choose_alignment(
            source_orders(source), observations(ROWS), 45., lambda *_: None)
        self.assertTrue(ready)
        self.assertEqual(order, "file")
        self.assertEqual([line["display_start"] for line in lines], [t for t, _ in ROWS])
        self.assertEqual(list(source_orders("[00:01][00:07]Chante\n[00:04]Danse")), ["timestamps"])

    def test_word_end_can_be_a_held_note_or_missing(self):
        segment = decode_segment(0., 5., .1, [(0.4, .9, b" bon"), (1.0, .9, b"jour"),
                                                 (2., .8, b" lune")], 12., 6.)
        a, b = segment.words
        self.assertEqual((a.text, a.start, a.end, a.end_origin), ("bonjour", 12.4, 14., "next_token"))
        self.assertIsNone(b.end)
        self.assertEqual(word_boundaries("chuis là"), [("je", 0, 5), ("suis", 0, 5), ("la", 6, 8)])
        self.assertEqual(word_boundaries("ﬆyle ℅"), [("style", 0, 4), ("c", 5, 6), ("o", 5, 6)])

    def test_full_silent_ffmpeg_windows_cover_track(self):
        with TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic.wav"
            with wave.open(str(path), "wb") as stream:
                stream.setparams((1, 2, 16_000, 0, "NONE", ""))
                stream.writeframes(bytes(53 * 16_000 * 2))
            decoder = Mock()
            decoder.decode.return_value = "fr", []
            result = transcribe_file(path, decoder, "fr", lambda _: None)
            self.assertEqual(result[:2], (53., "fr"))
            self.assertEqual([(len(args[0]) // 32000, args[2]) for args, _ in decoder.decode.call_args_list],
                             [(26, 0.), (28, 22.), (7, 46.)])


class RefineTests(unittest.TestCase):
    def test_audits_bad_prefix_and_keeps_other_baseline_anchors(self):
        rows = [(4., "soleil de minuit"), (8., "une forêt antique"), (11., "le matin arrive"),
                (17.5, "juste ici les trois oiseaux chantent"), (23., "puis les chemins tournent"),
                (26., "enfin le silence revient")]
        heard = observations(rows, [1.] * len(rows))
        index = sum(len(word_boundaries(text)) for _, text in rows[:3])
        heard[index] = ObservedWord("juste", 13.3, None, .97)
        lines, basis, ready = alignment(rows, heard, 35.)
        self.assertTrue(ready)
        old = lines[3]["words"][1]["start"]
        pcm = contextlib.nullcontext((35 * 16_000, None))
        first = observations(rows[3:4], [1.])
        second = observations(rows[3:4], [1.04])
        with (patch("_kara_refine.pcm_file", return_value=pcm),
              patch("_kara_refine.two_observations", return_value=(first, second, 2))):
            new, _, ready, stats = refine(Path("offline"), lines, basis, 35., "fr", lambda: None)
        self.assertTrue(ready)
        self.assertEqual(stats["quarantined"], 1)
        self.assertEqual(stats["recovered_mix"], 1)
        self.assertEqual(new[3]["start"], 18.5)
        self.assertEqual(new[3]["words"][0]["origin"], "whisper_local_mix")
        self.assertEqual(new[3]["words"][0]["rejected"]["start"], 13.3)
        self.assertEqual(new[3]["words"][1]["start"], old)
        self.assertEqual(lines[3]["start"], 13.3)

    def test_single_decode_is_only_a_candidate_and_crossing_is_refused(self):
        line = {"start": None}
        entries = [(0, {"start": 1.0}), (0, line), (0, {"start": 2.0})]
        gap = Gap(1, 2, 1., 2., False)
        read = ObservedWord("voix", 1.5, None, .90)
        known = [0, 2]
        self.assertEqual(consolidate(entries, gap, {1: read}, {}, "whisper_local_mix", known), 0)
        self.assertEqual(line["candidate"]["start"], 1.5)
        self.assertIsNone(line["start"])
        crossing = ObservedWord("voix", 2.1, None, .94)
        self.assertEqual(consolidate(entries, gap, {1: crossing}, {1: crossing}, "whisper_local_mix", known), 0)
        self.assertEqual(consolidate(entries, gap, {1: read}, {1: read}, "whisper_local_mix", known), 1)
        self.assertEqual(line["start"], 1.5)
        self.assertNotIn("candidate", line)

    def test_overlapping_windows_collapse_to_one_observation_on_short_file(self):
        engine = Mock()
        engine.decode.return_value = ("fr", [])
        pcm = 16000, Mock(return_value=b"pcm")
        _, second, count = two_observations(engine, pcm, "fr", .1, .95, [Gap(0, 1, .1, .95, False)])
        self.assertEqual((second, count), ((), 1))
        engine.decode.assert_called_once_with(b"pcm", "fr", 0., beam=True)



class PlainReferenceTests(unittest.TestCase):
    def test_missing_line_has_no_timestamp(self):
        rows = [(None, "Cette phrase existe"), (None, "Jamais observée")]
        heard = [ObservedWord(word, i + 1., i + 1.1, .95) for i, word in enumerate(("cette", "phrase", "existe"))]
        lines, report, ready = alignment(rows, heard, 9)
        self.assertFalse(ready)
        self.assertEqual(report["line_coverage"], .5)
        self.assertIsNone(lines[1]["display_start"])

    def test_refrain_publication_and_move_reuse_the_observed_reference(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            audio, model = root / "Titre.opus", root / "model"
            audio.write_bytes(b"fixture audio")
            model.write_bytes(b"fixture poids")
            cache = prepare.PreparedCache(root / "prepared", model)
            worker = prepare.Preparer(cache)
            words = ["refrain", "appris", "refrain", "appris"]
            heard = [ObservedWord(w, 1. + i*.5, 1.2 + i*.5, .99, "dtw") for i, w in enumerate(words)]
            # The two occurrences must use their own observations even if the
            # wording is identical. The bracketed section is a text heading.
            with patch.object(worker, "settings", return_value={}), patch.object(
                    worker, "transcribe", return_value=(5., "fr", heard, [], 0., .1)) as decode, patch.object(
                    prepare, "music_library", return_value=root):
                self.assertTrue(worker.prepare(audio, plain=b"[Chorus]\nRefrain appris\nRefrain appris\n")[0])
                self.assertIn("[00:02.000]Refrain appris", audio.with_suffix(".lrc").read_text())
                self.assertIsNotNone(cache.cached(audio, verify=False))
                self.assertTrue(worker.prepare(audio)[0])
                moved = audio.with_name("Autre chemin.opus")
                audio.rename(moved)
                audio.with_suffix(".lrc").rename(moved.with_suffix(".lrc"))
                self.assertTrue(worker.prepare(moved, previous=audio)[0])
                decode.assert_called_once()

class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        directory = Path(self.tmp.name)
        self.song = directory / "song.opus"
        self.song.write_bytes(b"original audio")
        self.lrc = self.song.with_suffix(".lrc")
        self.original = lrc_text([{"display_start": t, "text": text} for t, text in ROWS]).encode()
        self.lrc.write_bytes(self.original)
        model = directory / "model.bin"
        model.write_bytes(b"test weights")
        self.cache = prepare.PreparedCache(directory / "prepared", model)
        self.worker = prepare.Preparer(self.cache)
        self.worker.binary = directory / "fixture"
        self.worker.transcribe = Mock(return_value=(45., "fr", observations(ROWS, [.5] * 5), [], .5, 1.))

    def test_cache_identity_source_integrity_and_force(self):
        self.assertTrue(self.worker.prepare(self.song)[0])
        self.worker.prepare(self.song)
        self.worker.transcribe.assert_called_once()
        item = self.cache.cached(self.song)
        expected = lrc_text(item["lines"]).encode()
        self.assertEqual((self.cache.folder(self.song) / (item["lrc_sha256"] + ".lrc")).read_bytes(), expected)
        self.assertEqual(self.lrc.read_bytes(), self.original)
        self.song.write_bytes(b"changed audio!")
        self.assertIsNone(self.cache.cached(self.song))
        self.worker.prepare(self.song)
        self.lrc.write_bytes(self.original + b"\n")
        self.assertIsNone(self.cache.cached(self.song))
        self.worker.prepare(self.song)
        self.worker.prepare(self.song, force=True)
        # Forcing an already verified generation retries only local work.
        self.assertEqual(self.worker.transcribe.call_count, 3)
        self.cache.model.write_bytes(b"replacement weights")
        self.assertIsNone(self.cache.cached(self.song))

    def test_failure_corruption_and_interruption_cannot_publish(self):
        with patch.object(self.worker, "transcribe", side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.prepare(self.song)
        self.assertIsNone(self.cache.cached(self.song))
        self.worker.prepare(self.song)
        item = self.cache.cached(self.song)
        corrected = self.cache.folder(self.song) / (item["lrc_sha256"] + ".lrc")
        corrected.write_text("partial")
        self.assertIsNone(self.cache.cached(self.song))
        self.worker.transcribe.return_value = (45., "fr", [], [], .5, 1.)
        self.assertFalse(self.worker.prepare(self.song)[0])
        self.assertIsNone(self.cache.cached(self.song))
        self.assertEqual(self.cache.cached(self.song, insufficient=True)["state"], "insufficient")
        calls = self.worker.transcribe.call_count
        self.assertFalse(self.worker.prepare(self.song)[0])
        self.assertEqual(self.worker.transcribe.call_count, calls)

    def test_interrupt_between_lrc_and_manifest(self):
        atomic = prepare.atomic

        def interrupted(path, data):
            if path.name == "prepared.json":
                raise KeyboardInterrupt
            atomic(path, data)

        with patch.object(prepare, "atomic", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                self.worker.prepare(self.song)
        self.assertIsNone(self.cache.cached(self.song))
        self.assertTrue(self.worker.prepare(self.song)[0])

    def test_remote_source_reused_without_library_write(self):
        self.lrc.unlink()
        self.worker.reader = Mock(return_value=(self.original, "fixture"))
        self.worker.prepare(self.song)
        self.worker.prepare(self.song)
        self.worker.reader.assert_called_once_with(self.song)
        self.assertFalse(self.lrc.exists())
        self.worker.prepare(self.song, force=True)
        self.worker.reader.assert_called_once_with(self.song)

    def test_legacy_ready_migrates_without_rescanning_good_anchors(self):
        self.worker.prepare(self.song)
        generation = self.cache.cached(self.song)
        before = generation["lines"]
        del generation["refinement"]
        del generation["settings"]["local_decoding"]
        generation["settings"]["build"] = "historical-decoder"
        self.cache.publish(self.cache.folder(self.song), generation)
        ready, report, _, _ = self.worker.prepare(self.song)
        self.assertTrue(ready)
        self.assertTrue(report["refinement"]["reused_reference"])
        self.worker.transcribe.assert_called_once()
        upgraded = self.cache.cached(self.song)
        self.assertEqual(upgraded["lines"], before)
        self.assertEqual(upgraded["baseline_settings"]["build"], "historical-decoder")

    def test_batch_deduplicates_and_reports_all_tracks(self):
        second = self.song.with_name("second.opus")
        second.write_bytes(b"another audio file")
        second.with_suffix(".lrc").write_bytes(self.original)
        output = io.StringIO()
        args = ["prepare", str(self.song), str(self.song), str(second)]
        with (patch.object(prepare, "Preparer", return_value=self.worker), patch.object(sys, "argv", args),
              patch.object(prepare.signal, "signal"), contextlib.redirect_stdout(output)):
            self.assertEqual(prepare.main(), 0)
        results = [event for event in map(json.loads, output.getvalue().splitlines()) if event["event"] == "result"]
        self.assertEqual([event["path"] for event in results], [str(self.song), str(second)])
        self.assertEqual(self.worker.transcribe.call_count, 2)


class ScoreTests(unittest.TestCase):
    def setUp(self):
        lines, _, _ = alignment(ROWS, observations(ROWS), 45.)
        self.ref = {"lines": lines, "duration": 45., "language": "fr"}

    @staticmethod
    def heard(line, delay=0):
        return [{"word": word["normalized"], "start": word["start"] + delay,
                 "end": word["end"] + delay if word["end"] else None, "confidence": .9}
                for word in line["words"]]

    def test_perfect_score_and_boundary_timing(self):
        lines = self.ref["lines"]
        for delay in (0., .25, .5, 1., -1.):
            result = {i: score_reference_line(line, self.heard(line, delay)) for i, line in enumerate(lines)}
            detail = score_breakdown(self.ref, result)
            self.assertEqual((detail["lyrics"], detail["timing"], detail["coverage"], detail["score"]),
                             (100, 100, 100, 100))
        for delay, status in ((1.25, "late"), (-1.25, "early")):
            result = score_reference_line(lines[0], self.heard(lines[0], delay))
            self.assertEqual([r["status"] for r in result], [status] * 3)

    def test_fixed_offsets_and_progressive_drift_against_the_same_reference(self):
        line = self.ref["lines"][1]
        for delay in (.25, .5, .75, 1., 1.00025, 1.5, 2.):
            for direction in (-1, 1):
                late = "early" if direction < 0 else "late"
                observed = self.heard(line, direction * delay)
                self.assertEqual([w["status"] for w in score_reference_line(line, observed)],
                                 ["correct" if delay <= 1 else late] * 3)
        observed = self.heard(line)
        for word, delay in zip(observed, (0., .75, 1.5)):
            word["start"] += delay
        self.assertEqual([w["status"] for w in score_reference_line(line, observed)],
                         ["correct", "correct", "late"])

    def test_substitution_insertion_deletion_and_low_confidence(self):
        line = self.ref["lines"][1]
        heard = self.heard(line)
        heard[1]["word"] = "autre"
        self.assertEqual([r["status"] for r in score_reference_line(line, heard)], ["correct", "incorrect", "correct"])
        heard = self.heard(line)
        del heard[1]
        heard.append({"word": "ajout", "start": 15., "end": 15.2, "confidence": .9})
        self.assertEqual(sorted(r["status"] for r in score_reference_line(line, heard)),
                         ["correct", "correct", "extra", "missing"])
        heard[0]["confidence"] = .1
        self.assertEqual(score_reference_line(line, heard)[0]["status"], "uncertain")

    def test_silence_and_backing_cannot_credit_hallucinations(self):
        line = self.ref["lines"][0]
        for source in ("silence", "backing", "unverified"):
            line_result = score_reference_line(line, self.heard(line), source=source)
            result = score_breakdown(self.ref, {0: line_result})
            self.assertIsNone(result["score"])
            self.assertEqual(result["correct"], 0)
        all_silent = {index: score_reference_line(item, [], source="silence")
                      for index, item in enumerate(self.ref["lines"])}
        self.assertEqual(score_breakdown(self.ref, all_silent)["score"], 0)

    def test_empty_reference_word_is_excluded(self):
        line = self.ref["lines"][0]
        heard = self.heard(line)
        line["words"][1]["start"] = None
        result = score_reference_line(line, heard)
        self.assertEqual(result[1]["status"], "uncertain")
        summary = score_breakdown(self.ref, {0: result})
        self.assertIsNone(summary["score"])
        self.assertEqual(summary["correct"], 2)
        self.assertLess(summary["coverage"], 70)
        self.assertLess(summary["reference_coverage"], 100)

    def test_denominator_counts_only_evaluated_targets(self):
        lines = self.ref["lines"]
        heard = self.heard(lines[1])
        heard.append({"word": "addition", "start": 15.1, "confidence": .9})
        lines[0]["words"][0]["start"] = None
        results = {0: score_reference_line(lines[0], [], source="backing"),
                   1: score_reference_line(lines[1], heard)}
        for index in (2, 3, 4):
            results[index] = score_reference_line(lines[index], self.heard(lines[index]))
        summary = score_breakdown(self.ref, results)
        self.assertEqual(summary["total"], summary["verified"] + summary["unevaluable"])
        self.assertEqual(summary["eligible"], summary["verified"] + summary["uncertain"] + summary["not_assessed"])
        self.assertEqual(summary["verified"], summary["correct"] + summary["incorrect"])
        self.assertEqual((summary["extras"], summary["unreliable_reference"], summary["score"]), (1, 1, 100))
        self.assertLess(summary["coverage"], 100)
        del results[4]
        summary = score_breakdown(self.ref, results)
        self.assertEqual(summary["not_assessed"], len(lines[4]["words"]))
        self.assertIsNone(summary["score"])

    def test_pause_and_seek_do_not_reassign_completed_evidence(self):
        try:
            from _kara_game import GameScore
        except ImportError:
            self.skipTest("PyGObject absent")
        game = GameScore(lambda: None)
        game.set_round([line["text"] for line in self.ref["lines"]], reference=self.ref)
        game.ready = True
        game.send_job = Mock(return_value=True)
        times = [line["display_start"] for line in self.ref["lines"]]
        onsets = [tuple(word["start"] for word in line["words"]) for line in self.ref["lines"]]
        ends = [line["end"] for line in self.ref["lines"]]
        src = ("instrumental", "original")
        game.tick(times, 45., 0., 1., 100., onsets, ends, source=src)
        game.tick(times, 45., 12.9, 1., 112.9, onsets, ends, source=src)
        self.assertEqual(game.send_job.call_args.args[0].line, 0)
        window = game.send_job.call_args.args[0]
        heard = [{**word, "start": word["start"] - window.start, "end": word["end"] - window.start}
                 for word in self.heard(self.ref["lines"][0])]
        game.match_reference(window, {"source": "voice", "heard": heard})
        game.tick(times, 45., 13., 0., 113., onsets, ends, source=src)
        game.tick(times, 45., 13., 1., 117., onsets, ends, source=src)
        game.tick(times, 45., 17., 1., 121., onsets, ends, source=src)
        self.assertEqual(game.line_results[0][0]["status"], "correct")
        self.assertEqual(game.send_job.call_args.args[0].line, 1)

    def test_player_post_roll_only_extends_capture_and_respects_bounds(self):
        onsets = (10., 11.)
        old = prepared_window(0, 10., 12., 50., 1., onsets, 1.)
        new = prepared_window(0, 10., 12., 50., 1., onsets, 1., post_roll=.75)
        self.assertEqual((old.start, old.word_times), (new.start, new.word_times))
        self.assertEqual(new.stop, old.stop + .75)
        self.assertEqual(prepared_window(0, 10., 12., 14., 1., onsets, 1., post_roll=.75).stop, 14.)
        self.assertEqual(prepared_window(0, 10., 36.8, 50., 1., onsets, 1., post_roll=.75).stop, new.start + 30.)

    def test_prepared_player_job_has_no_lyric_prompt(self):
        try:
            from _kara_game import GameScore
            from _kara_scoring import AudioWindow
        except ImportError:
            self.skipTest("PyGObject absent")
        game = GameScore(lambda: None)
        game.set_round([line["text"] for line in self.ref["lines"]], reference=self.ref)
        game.process = Mock()
        self.assertTrue(game.send_job(AudioWindow(0, 1., 4., 1.), 20., 23., ("stem", "mix")))
        sent = game.process.get_stdin_pipe().write_all.call_args.args[0]
        job = json.loads(sent)
        self.assertTrue(job["prepared"])
        self.assertEqual(job["language"], "fr")
        self.assertTrue({"prompt", "expected", "word_times"}.isdisjoint(job))

    def test_voice_silence_and_accompaniment_waveform_gates(self):
        try:
            import numpy as np
            from _kara_voice import backing_similarity, transcribe_player, transcribe_prepared_job
        except ImportError:
            self.skipTest("Dépendances du moteur vocal absentes")
        decoder = Mock()
        samples = np.zeros(16_000, dtype=np.float32)
        self.assertEqual(transcribe_player(decoder, samples, "fr"), ([], "silence"))
        self.assertEqual(transcribe_prepared_job(decoder, samples, {"instrumental": False}), ([], "unverified"))
        decoder.decode.assert_not_called()

        rng = np.random.default_rng(17)
        backing = rng.normal(0, .08, 32_000).astype("<f4")
        replay = backing[2000:18000] * .3
        self.assertGreater(backing_similarity(replay, backing), .99)
        self.assertLess(backing_similarity(rng.normal(0, .03, 16_000), backing), .2)
        job = {"instrumental": True, "track_start": 0., "track_end": 1., "language": "fr",
               "backing": "fixture", "original": "fixture"}
        with patch.object(subprocess, "check_output", return_value=backing.tobytes()):
            self.assertEqual(transcribe_prepared_job(decoder, replay, job), ([], "backing"))
        decoder.decode.assert_not_called()
        # A low-pass replay loses broadband coherence, while the shared
        # low-band component still identifies the reference at its PCM lag.
        narrow = np.hanning(23)
        filtered = np.convolve(replay, narrow / narrow.sum(), mode="same").astype("<f4")
        self.assertLess(backing_similarity(filtered, backing), .72)
        with patch.object(subprocess, "check_output", return_value=backing.tobytes()):
            self.assertEqual(transcribe_prepared_job(decoder, filtered, job), ([], "backing"))
        decoder.decode.assert_not_called()

    def test_cross_window_word_has_one_owner(self):
        try:
            import numpy as np
            from _kara_voice import transcribe_player
            from _kara_turbo import Segment
        except ImportError:
            self.skipTest("Dépendances du moteur vocal absentes")
        # Both windows recognize the held word in their shared region.
        # Its actual onset is on the first side of the cut.
        word = ObservedWord("bonjour", 15.8, 16.4, .9, "next_token")
        decoder = Mock()
        decoder.decode.return_value = "fr", [Segment("bonjour", 15.8, 16.4, .1, (word,))]
        samples = (.02 * np.sin(2 * np.pi * 220 * np.arange(20 * 16_000) / 16_000)).astype(np.float32)
        received, source = transcribe_player(decoder, samples, "fr")
        self.assertEqual(source, "voice")
        self.assertEqual(decoder.decode.call_count, 2)
        self.assertEqual([(w["word"], w["start"], w["end"]) for w in received],
                         [("bonjour", 15.8, 16.4)])



if __name__ == "__main__":
    unittest.main()
