#!/usr/bin/env python3
"""Vérifie l’appui d’un chemin forcé dans le décodage CTC libre des mêmes émissions."""
from argparse import ArgumentParser
from collections import defaultdict
from csv import DictReader
import json
from pathlib import Path
import runpy
import sys

import numpy as np

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import digest
from _kara_words import words

SUBSTITUTIONS = {"a": "the", "and": "but", "i": "he", "in": "on", "is": "was",
                 "it": "this", "my": "your", "to": "for", "you": "we"}


def greedy(logp, vocabulary, offset, hop):
    alphabet = {value: key for key, value in vocabulary.items()}
    blank, separator = vocabulary["<pad>"], vocabulary["|"]
    symbols, start, end, previous = [], None, None, blank
    for frame, symbol in enumerate(logp.argmax(axis=1)):
        if symbol == separator and symbols:
            normalized = words("".join(symbols))
            if len(normalized) == 1:
                yield normalized[0], offset + start * hop, offset + (end + 1) * hop
            symbols, start, end = [], None, None
        character = alphabet[symbol]
        if len(character) == 1 and character != "|":
            if symbol != previous:
                symbols.append(character)
            if start is None:
                start = frame
            end = frame
        previous = symbol
    if symbols:
        normalized = words("".join(symbols))
        if len(normalized) == 1:
            yield normalized[0], offset + start * hop, offset + (end + 1) * hop


def corroborate(known, forced, independent, tolerance=.5, *, require_overlap=True):
    by_word = defaultdict(list)
    for i, (text, start, end) in enumerate(independent):
        by_word[text].append((i, start, end))
    previous, result = -1, []
    for text, (first, last, _, _) in zip(known, forced, strict=True):
        matching = [(abs(first - start), i) for i, start, end in by_word[text]
                    if i > previous and abs(first - start) <= tolerance
                    and (not require_overlap or start < last and first < end)]
        if matching:
            _, previous = min(matching)
            result.append(first)
        else:
            result.append(None)
    return result


def common_substitutions(known, starts):
    occurrences = defaultdict(list)
    for text, start in zip(known, starts, strict=True):
        if start is not None:
            occurrences[text].append(start)
    altered, positions = known.copy(), []
    for i, (text, start) in enumerate(zip(known, starts, strict=True)):
        alternate = SUBSTITUTIONS.get(text)
        if (alternate is None or start is None or len(positions) == 8
                or positions and start - starts[positions[-1]] < 8
                or any(abs(start - heard) <= 1 for heard in occurrences[alternate])):
            continue
        altered[i] = alternate
        positions.append(i)
    return altered, positions


def middle_line(annotations, known, parse):
    position, candidates = 0, []
    with (annotations / "lines.csv").open(newline="") as stream:
        for line in DictReader(stream):
            tokens = words(line["lyrics_line"])
            end = position + len(tokens)
            if known[position:end] != tokens:
                raise ValueError("Les paroles et les annotations par ligne divergent")
            left, right = parse(line["start_time"]), parse(line["end_time"])
            if len(tokens) >= 3 and left is not None and right is not None and right > left:
                candidates.append((position, end, left, right))
            position = end
    if position != len(known) or not candidates:
        raise ValueError("Aucune ligne complète à retirer du témoin")
    return min(candidates, key=lambda line: abs(len(known) - line[0] - line[1]))


def main():
    parser = ArgumentParser(description=__doc__)
    for name in ("case", "parent_result", "vocabulary"):
        parser.add_argument(name, type=Path)
    parser.add_argument("--turbo", type=Path, help="Dossier des observations Turbo du même mix")
    args = parser.parse_args()
    candidate = runpy.run_path(str(HERE.with_name("qualify-parole-ctc.py")))
    timing = runpy.run_path(str(HERE.with_name("qualify-kara-reference-timing.py")))
    case = json.loads(args.case.read_bytes())
    result = json.loads(args.parent_result.read_bytes())
    if not result["result"] or result["case"] != digest(args.case) or result["vocabulary"] != digest(args.vocabulary):
        raise ValueError("Le reçu d’émissions n’appartient pas à cette référence")
    folder = args.parent_result.resolve().with_suffix(".windows")
    manifest = json.loads((folder / "manifest.json").read_bytes())
    if (manifest.keys() != {"model", "source", "case", "tool", "vocabulary"}
            or any(result[key] != expected for key, expected in manifest.items())):
        raise ValueError("Signature des émissions différente du reçu")
    windows = sorted(folder.glob("*.npy"))
    if [int(path.stem) for path in windows] != list(range(0, len(windows)*candidate["CORE"], candidate["CORE"])):
        raise ValueError("Fenêtre acoustique manquante")
    vocabulary = json.loads(args.vocabulary.read_bytes())
    logp = np.concatenate([np.load(path, allow_pickle=False) for path in windows])
    if (logp.shape != (result["result"]["frames"], len(vocabulary)) or logp.dtype != np.float32
            or not np.isfinite(logp).all()):
        raise ValueError("Chronologie des émissions incomplète")
    known = case["expected"]
    annotations = Path(case["annotation"])
    if [digest(annotations / name) for name in ("words.words.txt", "manual.csv", "lines.csv")] != case["reference_hashes"]:
        raise ValueError("Annotations modifiées")
    labels = (annotations / "words.words.txt").read_text().splitlines()
    with (annotations / "manual.csv").open(newline="") as stream:
        starts = [timing["onset"](row["word_start"]) if len(tokens) == 1 else None
                  for label, row in zip(labels, DictReader(stream), strict=True)
                  if (tokens := words(label)) for _ in tokens]
    step = candidate["HOP"] / candidate["RATE"]
    offset = (candidate["KERNEL"] // 2 - candidate["HOP"] // 2) / candidate["RATE"]
    unrestricted = list(greedy(logp, vocabulary, offset, step))
    forced = candidate["predict"](logp, vocabulary, known)
    plausible = corroborate(known, forced, unrestricted)
    altered = known.copy()
    negative = range(19, len(altered), max(1, len(altered) // 8))
    for i in negative:
        altered[i] = "xylophone"
    falsified = candidate["predict"](logp, vocabulary, altered)
    counterfeit = corroborate(altered, falsified, unrestricted)
    # Common substitutions are a harder negative control than an outlier
    # word. A guided match to another occurrence still counts as a miss.
    ordinary, positions = common_substitutions(known, starts)
    mismatched = candidate["predict"](logp, vocabulary, ordinary)
    aliases = corroborate(ordinary, mismatched, unrestricted)
    left, right, gap_start, gap_end = middle_line(annotations, known, timing["onset"])
    omitted = known[:left] + known[right:]
    surviving = starts[:left] + starts[right:]
    timing_without_line = candidate["predict"](logp, vocabulary, omitted)
    confirmed_without_line = corroborate(omitted, timing_without_line, unrestricted)

    def strayed_into_gap(observed):
        return sum(actual is not None and expected is not None and not gap_start <= expected <= gap_end
                   and gap_start + .25 < actual < gap_end - .25
                   for expected, actual in zip(surviving, observed, strict=True))

    forced_without_line = [word[0] for word in timing_without_line]
    report = {
        "id": case["id"],
        "timing": timing["summarize"](starts, plausible),
        "corrupted_accepted": sum(counterfeit[i] is not None for i in negative),
        "corrupted_total": len(negative),
        "common_substitutions": {
            "total": len(positions),
            "geometric_20_accepted": sum(mismatched[i][3] >= .20 for i in positions),
            "corroborated": sum(aliases[i] is not None for i in positions),
        },
        "missing_line": {
            "removed_words": right - left,
            "forced": timing["summarize"](surviving, forced_without_line),
            "corroborated": timing["summarize"](surviving, confirmed_without_line),
            "forced_into_gap": strayed_into_gap(forced_without_line),
            "corroborated_into_gap": strayed_into_gap(confirmed_without_line),
        },
    }
    if args.turbo:
        turbo_case = json.loads((args.turbo / "case.json").read_bytes())
        if (turbo_case["audio_sha256"] != result["source"] or turbo_case["expected"] != known
                or turbo_case["end"] is not None or turbo_case["start"] != 0):
            raise ValueError("Les observations Turbo doivent venir du même mix entier")
        observations = json.loads((args.turbo / "observations" / "mix.json").read_bytes())
        if observations["language"] != "en":
            raise ValueError("La langue détectée par Turbo diffère de celle du banc CTC")
        turbo = [(text, start, end) for text, start, end, confidence, _ in observations["words"] if confidence >= .50]

        def both(text, proposed):
            # Turbo supplies an independent beginning. Some observations
            # have no measured end, so use proximity without inventing one.
            return corroborate(text, proposed, turbo, .30, require_overlap=False)

        shared = both(known, forced)
        false_nonce, false_common = both(altered, falsified), both(ordinary, mismatched)
        report["turbo_agreement"] = dict(timing=timing["summarize"](starts, shared),
                                         corrupted=sum(false_nonce[i] is not None for i in negative),
                                         substitutions=sum(false_common[i] is not None for i in positions))
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
