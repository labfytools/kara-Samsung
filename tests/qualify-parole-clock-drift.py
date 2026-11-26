#!/usr/bin/env python3
"""Mesurer le recalage de repères Turbo sur des blocs d’annotations tenus à part."""

from argparse import ArgumentParser
from csv import DictReader
import json
from pathlib import Path
import runpy
from statistics import median


BLOCK_SECONDS = 30
GUARD_SECONDS = 2
MIN_ANCHORS = 12
HERE = Path(__file__).resolve()


def fit(anchors):
    # Robust estimates from separate early and late anchors avoid treating
    # a single misplaced refrain as an offset or a whole-song clock slope.
    half = len(anchors) // 2
    early, late = anchors[:half], anchors[half:]
    origin, finish = (median(supplied for _, supplied in group) for group in (early, late))
    first, last = (median(expected - supplied for expected, supplied in group) for group in (early, late))
    slope = (last - first) / (finish - origin) if finish > origin else 0.
    offset = median(expected - supplied for expected, supplied in anchors)
    return offset, origin, first, slope


def cross_validate(onsets, series, summarize):
    blocks = sorted({int(t // BLOCK_SECONDS) for t in onsets if t is not None})
    anchors = [(truth, supplied) for truth, supplied in zip(onsets, series["words"][1], strict=True)
               if truth is not None and supplied is not None]
    models = {}
    for block in blocks:
        low = block * BLOCK_SECONDS - GUARD_SECONDS
        high = (block + 1) * BLOCK_SECONDS + GUARD_SECONDS
        train = [(truth, supplied) for truth, supplied in anchors if truth < low or truth >= high]
        if len(train) >= MIN_ANCHORS:
            models[block] = fit(train)
    if len(models) != len(blocks):
        raise ValueError("Ancrages insuffisants hors bloc pour une mesure comparable")

    def shift(reference, supplied, owner, drift):
        if reference is None or supplied is None:
            return supplied
        offset, origin, first, slope = models[int((owner if owner is not None else reference) // BLOCK_SECONDS)]
        return supplied + (first + slope * (supplied - origin) if drift else offset)

    # Preserve word/line matches and the missing-reper denominator. A
    # correction cannot gain coverage by choosing a new matching after fit.
    return {
        "training_blocks": len(models),
        "offset_ms_by_block": {i: round(model[0] * 1000, 3) for i, model in models.items()},
        "drift_ms_per_minute_by_block": {i: round(model[3] * 60000, 3) for i, model in models.items()},
        "reperes": {
            label: {
                "raw": summarize(truth, measured),
                **{name: summarize(truth, [shift(t, s, owner, drift)
                                           for t, s, owner in zip(truth, measured, owners, strict=True)])
                   for name, drift in (("offset", False), ("drift", True))},
            }
            for label, (truth, measured, owners) in series.items()
        },
    }


def assess(case_file, variant):
    bench = runpy.run_path(str(HERE.with_name("qualify-parole-separation.py")))
    timing = runpy.run_path(str(HERE.with_name("qualify-kara-reference-timing.py")))
    read, words, onset = bench["read"], bench["words"], timing["onset"]
    case = read(case_file)
    directory = Path(case["annotation"])
    restored = bench["restore_variant"](case_file.parent / "observations", variant)
    if restored is None:
        raise ValueError("Reçu Turbo de cette variante absent")
    _, heard, score, _, _ = restored
    with (directory / "manual.csv").open(newline="") as stream:
        rows = list(DictReader(stream))
    labels = (directory / "words.words.txt").read_text().splitlines()
    boundaries = []
    for column in ("word_start", "word_end"):
        boundaries.append([onset(row[column]) if len(tokens) == 1 else None
                           for label, row in zip(labels, rows, strict=True)
                           if (tokens := words(label)) for _ in tokens])
    start, end = boundaries
    matched = bench["alignment"](case["expected"], heard, start)
    series = {
        "words": (start, [word.start if word else None for word in matched], start),
        "word_ends": (end, [word.end if word else None for word in matched], start),
        "lines": ([], [], []),
        "line_ends": ([], [], []),
    }
    # Assign a line's endpoints to the block holding its constituent
    # word annotation. This keeps them out of their predictor's training set.
    position = 0
    with (directory / "lines.csv").open(newline="") as stream:
        for line in DictReader(stream):
            tokens = words(line["lyrics_line"])
            if not tokens:
                continue
            last = position + len(tokens) - 1
            for key, column, index, attr in (("lines", "start_time", position, "start"),
                                             ("line_ends", "end_time", last, "end")):
                truth, measured, owners = series[key]
                truth.append(onset(line[column]))
                measured.append(getattr(matched[index], attr, None))
                owners.append(start[index])
            position = last + 1
    result = cross_validate(start, series, timing["summarize"])
    for label, metrics in result["reperes"].items():
        if metrics["raw"] != score["independent_timing"][label]:
            raise ValueError("Les repères bruts ne reproduisent pas le reçu")
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("case", type=Path)
    parser.add_argument("variant")
    args = parser.parse_args()
    print(json.dumps(assess(args.case, args.variant), ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
