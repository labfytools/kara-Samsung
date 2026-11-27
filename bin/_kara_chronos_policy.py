"""Selection and lexical evidence from CHRONOS b4cbb40."""
from math import inf
from statistics import mean

from _kara_reference import align_words
from _kara_words import words as normalize

HOP = 768 / 22050


def difficult(rows):
    return sum(row is None or row["margin"] < -3 for row in rows) / max(1, len(rows)) > .20


def select(lines, cheap, deep):
    # Keep a single acoustic path per line. When joining two different
    # views, compare their observed intervals on the original file clock.
    if deep is None or not difficult(cheap):
        return cheap, ["umx"] * len(cheap)
    paths = (cheap, deep)
    states = [(0., []), (0., [])]
    left = 0
    for line in lines:
        length = len(line)
        right = left + length
        options = []
        for mode, rows in enumerate(paths):
            block = rows[left:right]
            found = [row for row in block if row]
            margins = sorted(row["margin"] if row else -15 for row in block)
            quality = mean(margins[:max(1, (length + 2) // 3)])
            valid = []
            for previous, (score, trace) in enumerate(states):
                prior = next((row for row in reversed(paths[previous][:left]) if row), None)
                if left and found and prior and prior["end"] > found[0]["start"] + 1e-7:
                    continue
                switch = .3 if left and previous != mode else 0.
                valid.append((score + quality - switch, trace + [mode]))
            options.append(max(valid, key=lambda item: item[0]) if valid else (-inf, []))
        states = options
        left = right
    _, trace = max(states, key=lambda item: item[0])
    if len(trace) != len(lines):
        return cheap, ["umx"] * len(cheap)
    modes = [mode for line, mode in zip(lines, trace, strict=True) for _ in line]
    return [paths[mode][i] for i, mode in enumerate(modes)], ["demucs" if mode else "umx" for mode in modes]


def decide(lines, cheap, deep, heard, contrasts):
    tokens = [token for line in lines for token in line]
    if len(cheap) != len(tokens) or deep is not None and len(deep) != len(tokens):
        raise ValueError("Identité des mots CHRONOS incohérente.")
    proposals, sources = select(lines, cheap, deep)
    flags = support(tokens, proposals, heard)
    decisions = []
    for i, (row, source, lexical) in enumerate(zip(proposals, sources, flags, strict=True)):
        contrast = contrasts[source][i] if source in contrasts else None
        if row is None:
            decisions.append(None)
            continue
        # The model's delay was observed in development. Subtract precisely
        # one model hop on all tiers of this phonetic clock.
        corrected = dict(row, start=max(0., row["start"] - HOP), end=max(0., row["end"] - HOP),
                         phones=[[phone, max(0., stamp - HOP)] for phone, stamp in row["phones"]],
                         source=source, lexical=lexical,
                         acoustic=bool(contrast and contrast["impostor"] >= -.5 and row["margin"] >= -3),
                         impostor=None if not contrast else contrast["impostor"])
        decisions.append(corrected if corrected["end"] > corrected["start"] else None)
    return decisions


def support(reference, rows, heard):
    flat, positions = [], []
    for word in reference:
        left = len(flat)
        flat.extend(normalize(word))
        positions.append(range(left, len(flat)))
    tokens = [(part, stamp, confidence) for word, stamp, confidence in heard for part in normalize(word)]
    matches, _, _ = align_words(flat, [row[0] for row in tokens])
    flags = []
    for proposal, group in zip(rows, positions, strict=True):
        indices = [matches.get(i) for i in group]
        confirmed = bool(proposal and indices and all(j is not None for j in indices))
        if confirmed:
            first = indices[0]
            confirmed = (indices == list(range(first, first + len(indices)))
                         and abs(tokens[first][1] - proposal["start"]) <= .65
                         and min(tokens[j][2] for j in indices) >= .25)
        flags.append(confirmed)
    return flags
