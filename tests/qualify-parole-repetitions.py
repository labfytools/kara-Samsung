#!/usr/bin/env python3
"""Mesure si les répétitions trouvées sur le mix ou la voix partagent des mots."""
from argparse import ArgumentParser
from collections import Counter
from csv import DictReader
import json
import os
from pathlib import Path
import runpy
import sys

import numpy as np

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import RATE, digest, pcm_file
from _kara_words import words

HOP = 320
STRIDE = 6
LENGTH = 150
MAX_PAIRS = 20
VOICE_FLOOR = .0126  # -38 dBFS on the separated voice.
MARGINS = .2


def read(path):
    return json.loads(path.read_bytes())


def features(path):
    fft_size = 640
    with pcm_file(path) as (count, fetch):
        if not RATE <= count <= RATE * 360:
            raise ValueError("Ce prototype quadratique est borné à six minutes")
        signal = np.frombuffer(fetch(0, count), dtype="<i2").astype(np.float32) / 32768
    frames = np.lib.stride_tricks.sliding_window_view(signal, fft_size)[::HOP]
    rms = np.sqrt(np.mean(frames * frames, axis=1))
    spectra = np.abs(np.fft.rfft(frames * np.hanning(fft_size), axis=1)).astype(np.float32) ** 2
    frequencies = np.fft.rfftfreq(fft_size, 1 / RATE)
    edges = np.geomspace(150, 7000, 25)
    bands = [spectra[:, (frequencies >= a) & (frequencies < b)].mean(axis=1) for a, b in zip(edges, edges[1:])]
    loudness = np.log10(np.maximum(np.stack(bands, axis=1), 1e-8))
    loudness -= loudness.mean(axis=1, keepdims=True)
    loudness = loudness[:len(loudness) // STRIDE * STRIDE].reshape(-1, STRIDE, len(bands)).mean(axis=1)
    windows = np.lib.stride_tricks.sliding_window_view(loudness, LENGTH // STRIDE, axis=0)
    embeddings = windows.reshape(len(windows), -1).copy()
    # A stable spectrum is common to the same singer or backing instrument.
    # Subtract it separately in each window to compare spectral evolution.
    embeddings -= windows.mean(axis=2, keepdims=True).repeat(LENGTH // STRIDE, axis=2).reshape(embeddings.shape)
    embeddings /= np.maximum(np.linalg.norm(embeddings, axis=1, keepdims=True), 1e-8)
    activity = np.lib.stride_tricks.sliding_window_view(rms, LENGTH)[::STRIDE][:len(windows)]
    return embeddings, np.mean(activity > VOICE_FLOOR, axis=1) >= .6


def candidates(embeddings, voice_activity=None):
    score = np.triu(embeddings @ embeddings.T, k=round(8 * RATE / (HOP * STRIDE)))
    if voice_activity is not None:
        score *= voice_activity[:, None] & voice_activity[None, :]
    for _ in range(MAX_PAIRS):
        i, j = np.unravel_index(score.argmax(), score.shape)
        certainty = float(score[i, j])
        if certainty <= 0:
            return
        yield i, j, certainty
        # Nearby alignments are the same proposed recurrence. Suppress the
        # neighborhood before looking for another position pair.
        margin = LENGTH // STRIDE
        score[max(0, i - margin):i + margin + 1, max(0, j - margin):j + margin + 1] = 0


def evaluate(pairs, reference, observed, compare):
    length = LENGTH * HOP / RATE
    hop = STRIDE * HOP / RATE
    report, asymmetry = [], Counter()
    for left, right, similarity in pairs:
        def phrases(series):
            return [[word for start, word in series
                     if origin * hop + MARGINS <= start < origin * hop + length - MARGINS]
                    for origin in (left, right)]

        first, second = phrases(reference)
        # Reference words are opened only for evaluation. A matching chord
        # progression or a repeated single interjection earns no positive.
        tested = min(len(first), len(second)) >= 3
        equal = first == second if tested else None
        scores = None
        if equal:
            # Labels measure how well the original decoder covered a true
            # repetition. The comparison never copies either hypothesis.
            heard = phrases(observed)
            scores = [compare(tokens, current) for tokens, current in zip((first, second), heard, strict=True)]
            for metric in ("errors", "substitutions", "deletions", "insertions"):
                asymmetry[metric] += scores[0][metric] != scores[1][metric]
        report.append(dict(left=left * hop, right=right * hop, similarity=similarity,
                           words_per_window=[len(first), len(second)], repeated=equal,
                           observed_scores=scores))
    return report, dict(asymmetry)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("cases", type=Path, nargs="+")
    args = parser.parse_args()
    os.umask(0o077)
    compare = runpy.run_path(str(HERE.with_name("qualify-kara-singing.py")))["lexical_errors"]
    results = []
    for case_path in args.cases:
        case = read(case_path)
        stem = read(case_path.with_name("demucs.json"))
        mix, vocals = Path(case["path"]), Path(stem["vocals"])
        if (case["end"] is not None or stem["offset"] != 0 or digest(mix) != case["audio_sha256"]
                or digest(vocals) != stem["sha256"]):
            raise ValueError("Signal audio ou chronologie différents du reçu")
        mix_vectors, _ = features(mix)
        voice_vectors, activity = features(vocals)
        if abs(len(mix_vectors) - len(voice_vectors)) > 1:
            raise ValueError("Le mix et les voix ne partagent pas la même chronologie")
        proposed = {"mix": list(candidates(mix_vectors)), "vocals": list(candidates(voice_vectors, activity))}
        annotation = Path(case["annotation"])
        if [digest(annotation / name) for name in ("words.words.txt", "manual.csv", "lines.csv")] != case["reference_hashes"]:
            raise ValueError("Annotations modifiées")
        lyrics = (annotation / "words.words.txt").read_text().splitlines()
        with (annotation / "manual.csv").open(newline="") as stream:
            seen = [(float(row["word_start"]), word) for lyric, row in zip(lyrics, DictReader(stream), strict=True)
                    if row["word_start"] for word in words(lyric)]
        measured, different = {}, {}
        for variant, pairs in proposed.items():
            audio_receipt = read(case_path.parent / "observations" / f"{variant}.json")
            if audio_receipt["score"]["expected"] != len(case["expected"]):
                raise ValueError("Le reçu de transcription ne correspond pas à la référence")
            observed = [(item[1], item[0]) for item in audio_receipt["words"]]
            measured[variant], different[variant] = evaluate(pairs, seen, observed, compare)
        results.append(dict(id=case["id"], mix=digest(mix), vocals=digest(vocals), pairs=measured, asymmetry=different))
    print(json.dumps(results, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
