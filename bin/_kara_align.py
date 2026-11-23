"""Recalage local d'un LRC sur l'enregistrement du morceau."""
from collections import defaultdict
from difflib import SequenceMatcher
import json
from pathlib import Path
from subprocess import check_output, SubprocessError
import sys

import numpy as np

from _kara_timing import MAX_TIMING_STEP, TIMING_VERSION, audio_signature, line_signature
from _kara_voice import NativeDecoder, RATE, TURBO_MODEL, load_model, emit
from _kara_dtw_words import native_timing, word_checkpoints
from _kara_words import IGNORED_HINTS, LyricVocabulary, words
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc


def references(segments, low, high):
    for start, stop, text in segments:
        if text.startswith(("*", "[", "(")) and text.endswith(("*", "]", ")")):
            continue
        tokens = words(text)
        for index, token in enumerate(tokens):
            time = start + (stop - start) * (index + .5) / len(tokens)
            if low <= time < high:
                yield time, time, token


def estimate_timing(rows, observed, word_based):
    expected = [(i, j, w) for i, (_, text) in enumerate(rows) for j, w in enumerate(words(text))]
    stamps = [start for start, _ in rows]
    heard = [word for _, _, word in observed]
    matcher = SequenceMatcher(None, [word for _, _, word in expected], heard, autojunk=False)
    matches = defaultdict(list)
    for first, second, size in matcher.get_matching_blocks():
        for k in range(size):
            line, index, word = expected[first + k]
            moment, end, _ = observed[second + k]
            if abs(stamps[line] - moment) <= 16:
                matches[line].append((index, moment, end, word))
    coverage = sum(map(len, matches.values())) / len(expected) if expected else 0
    anchors = {}
    for i, hits in matches.items():
        count = len(words(rows[i][1]))
        first, heard_at, _, _ = hits[0]
        distinct = sum(word not in IGNORED_HINTS and len(word) >= 3 for _, _, _, word in hits)
        if len(hits) >= 3 and len(hits) * 2 >= count and first <= 1 and distinct >= 1:
            start = max(0, heard_at - (0 if word_based else .18) - first * .30)
            anchors[i] = round(start, 2)
    if coverage < .55 or len(anchors) < min(4, max(2, sum(bool(words(text)) for _, text in rows) // 2)):
        return None, None, coverage, 0
    applied = 0
    original = stamps.copy()
    for i, candidate in anchors.items():
        if abs(candidate - original[i]) < (.075 if word_based else .65):
            continue
        lo = stamps[i - 1] if i else -.3
        hi = anchors.get(i + 1, stamps[i + 1]) if i + 1 < len(stamps) else float("inf")
        if candidate >= lo + .25 and candidate <= hi - .25:
            stamps[i] = candidate
            applied += 1
    if not all(a < b for a, b in zip(stamps, stamps[1:])):
        return None, None, coverage, 0
    marks = None
    if word_based:
        marks = [
            word_checkpoints(text, matches[i], stamps[i],
                             stamps[i + 1] if i + 1 < len(stamps) else stamps[i] + 8)
            if i in anchors else None
            for i, (_, text) in enumerate(rows)
        ]
    return stamps, marks, coverage, applied


def refine_lines(audio, duration, rows, vocabulary, stamps, marks, progress):
    if not marks:
        return 0
    try:
        decoder = NativeDecoder(vocabulary.language)
    except (OSError, RuntimeError):
        return 0
    refined = 0
    try:
        for index, (_, text) in enumerate(rows):
            progress(60 + 39 * index // len(rows))
            expected = vocabulary.tokens[index]
            if not expected:
                continue
            start = stamps[index]
            previous = stamps[index - 1] if index else 0
            following = stamps[index + 1] if index + 1 < len(stamps) else duration
            if marks[index]:
                end = marks[index][-1][0]
            else:
                end = min(following, start + 8)
            head = max(0, start - MAX_TIMING_STEP)
            tail = min(duration, following + MAX_TIMING_STEP, end + MAX_TIMING_STEP)
            if tail - head < .2:
                continue
            try:
                observed = decoder.decode(audio[round(head * RATE):round(tail * RATE)],
                                          vocabulary.prompt(index, index))
            except RuntimeError:
                continue
            hits = []
            matching = SequenceMatcher(None, expected, [word for _, _, word in observed], autojunk=False)
            for first, second, count in matching.get_matching_blocks():
                for step in range(count):
                    low, high, word = observed[second + step]
                    if low + head < following:
                        hits.append((first + step, low + head, high + head, word))
            if (len(hits) < max(2, .70 * len(expected))
                    or hits[0][0] > 1
                    or not any(word not in IGNORED_HINTS for _, _, _, word in hits)):
                continue
            first, begin, _, _ = hits[0]
            candidate = round(begin - first * .3, 2)
            if abs(candidate - start) > MAX_TIMING_STEP or not previous < candidate < following:
                candidate = start
            checkpoints = word_checkpoints(text, hits, candidate, following)
            if checkpoints is None:
                continue
            stamps[index], marks[index] = candidate, checkpoints
            refined += 1
    except (OSError, EOFError, ValueError):
        pass
    finally:
        decoder.close()
    return refined


def align_song(path):
    lrc = path.with_suffix(".lrc")
    with lrc.open("rb") as source:
        raw = source.read(MAX_LRC_BYTES + 1)
    if len(raw) > MAX_LRC_BYTES:
        raise ValueError("Fichier LRC trop grand")
    rows = parse_lrc(raw.decode("utf-8-sig"))
    vocabulary = LyricVocabulary(text for _, text in rows)
    if not any(vocabulary.tokens):
        raise ValueError("Paroles minutées introuvables")
    audio_id = audio_signature(path)
    data = check_output(("ffmpeg", "-v", "error", "-i", str(path), "-f", "s16le",
                         "-ac", "1", "-ar", "16000", "-"), timeout=90)
    audio = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
    duration = len(audio) / 16000
    if duration < 1:
        raise ValueError("Le morceau est trop court pour le calage")
    executable = Path(__file__).with_name("_kara_dtw")
    highest = 0

    def report(value, phase="song"):
        nonlocal highest
        highest = max(highest, value)
        emit("progress", percent=highest, phase=phase)

    try:
        observed = native_timing(executable, TURBO_MODEL, vocabulary.language, data,
                                 lambda value: report(int(.6 * value)))
    except (OSError, ValueError):
        observed = None
    word_based = bool(observed)
    label = "Turbo"
    if not word_based:
        model, label = load_model("turbo")
        observed = []
        fallback_start = highest
        for low in range(0, int(duration) + 1, 12):
            high = min(low + 12, duration)
            a, b = max(0, low - 2), min(duration, high + 2)
            clip = audio[int(a * 16000):int(b * 16000)]
            if len(clip) < 16000:
                continue
            segments = model.transcribe(clip, language=vocabulary.language, initial_prompt="", no_context=True,
                                        no_speech_thold=.65, audio_ctx=900 if label == "Turbo" else 0,
                                        token_timestamps=True, split_on_word=True, max_len=24)
            spans = [(a+s.t0/100, a+s.t1/100, s.text) for s in segments if 0 <= s.t0/100 < b-a
                     and s.t1 > s.t0 and s.t1/100 <= b-a+.55]
            observed.extend(references(spans, low, high))
            report(fallback_start + int((100 - fallback_start) * high / duration))
    stamps, marks, coverage, count = estimate_timing(rows, sorted(observed), word_based)
    if word_based and stamps is None:
        # Le LRC donne une fenêtre de recherche quand Turbo ne reconnaît pas
        # assez de mots sur la piste entière (rap, effets, couplets répétés).
        stamps, marks = [start for start, _ in rows], [None] * len(rows)
    refined = refine_lines(audio, duration, rows, vocabulary, stamps, marks,
                           lambda value: report(value, "lines"))
    if marks and not any(marks):
        stamps = marks = None
    report(100, "lines" if marks else "song")
    return {"signature": line_signature(rows), "audio": audio_id, "version": TIMING_VERSION,
            "times": stamps, "word_marks": marks, "coverage": round(coverage, 3),
            "changed": count, "refined": refined, "model": label}


if __name__ == "__main__":
    try:
        emit("alignment", **align_song(Path(sys.argv[1])))
    except (OSError, ValueError, RuntimeError, SubprocessError) as error:
        emit("error", message=str(error))
        raise SystemExit(1)
