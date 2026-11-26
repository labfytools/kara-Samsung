#!/usr/bin/env python3
"""Comparer les repères acoustiques de Kara aux annotations JamendoLyrics."""

import argparse
import csv
import json
from math import ceil, isfinite
from pathlib import Path
from statistics import median
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_words import words


def onset(value):
    number = float(value) if value not in ("", None) else None
    return number if number is not None and isfinite(number) and number >= 0 else None


def summarize(truth, observed):
    deltas = sorted(abs(expected - supplied) * 1000
                    for expected, supplied in zip(truth, observed, strict=True)
                    if expected is not None and supplied is not None)
    annotated = sum(expected is not None for expected in truth)
    seen = len(deltas)
    within = {limit: sum(delta <= limit + 1e-8 for delta in deltas) for limit in (250, 500, 1000)}
    return {
        "total": len(truth), "non_evaluable": len(truth) - annotated,
        "annotated": annotated, "evaluated": seen, "absent": annotated - seen,
        "coverage": seen / annotated if annotated else None,
        "median_ms": median(deltas) if seen else None,
        "p90_ms": deltas[ceil(.90 * seen) - 1] if seen else None,
        "p95_ms": deltas[ceil(.95 * seen) - 1] if seen else None,
        "max_ms": deltas[-1] if seen else None,
        **{f"within_{limit}_ms": count / seen if seen else None for limit, count in within.items()},
        "coverage_within_500_ms": within[500] / annotated if annotated else None,
    }


def qualify(directory, observed):
    with (directory / "lines.csv").open(newline="") as stream:
        rows = list(csv.reader(stream))[1:]
    with (directory / "manual.csv").open(newline="") as stream:
        onsets = [onset(row["word_start"]) for row in csv.DictReader(stream)]
    labels = (directory / "words.words.txt").read_text().splitlines()
    lyric_words = []
    word_truth = []
    for label, expected in zip(labels, onsets, strict=True):
        tokens = words(label)
        lyric_words.extend(tokens)
        # A single Jamendo word may split into multiple Kara tokens. The
        # manual onset cannot qualify each subtoken independently.
        word_truth.extend([expected if len(tokens) == 1 else None] * len(tokens))
    observed_words = [word for line in observed for word in line["words"]]
    if ([words(row[2]) for row in rows] != [words(line["text"]) for line in observed]
            or lyric_words != [word["normalized"] for word in observed_words]):
        raise ValueError("Le texte de référence et celui analysé ne correspondent pas")
    return {
        "words": summarize(word_truth, [onset(word["start"]) for word in observed_words]),
        # Source/display fallbacks never count as acoustic evidence.
        "lines": summarize([onset(row[0]) for row in rows], [onset(line["start"]) for line in observed]),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("annotations", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    observed = json.loads(args.evidence.read_text())
    if isinstance(observed, dict):
        observed = observed["lines"]
    print(json.dumps(qualify(args.annotations, observed), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
