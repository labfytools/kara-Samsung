"""Contrast expected phones against unrestricted phones on the same frames."""
import numpy as np


def path_value(logp, tokens, blank):
    # Local endpoints allow blank or space. Retain float64 so the margins
    # between an expected and alternative pronunciation survive long blanks.
    labels = np.full(2 * len(tokens) + 1, blank)
    labels[1::2] = tokens
    jump = (labels[2:] != blank) & (labels[2:] != labels[:-2])
    previous = np.full(len(labels), -np.inf)
    previous[0] = 0
    for frame in logp:
        current = previous.copy()
        current[1:] = np.maximum(current[1:], previous[:-1])
        current[2:] = np.maximum(current[2:], np.where(jump, previous[:-2], -np.inf))
        previous = current + frame[labels]
    return max(previous[-1], previous[-2])


def scores(logp, rows, inventory, hop):
    scored = []
    for row in rows:
        if not row:
            scored.append(None)
            continue
        left = max(0, round(row["start"] / hop) - 2)
        right = min(len(logp), round(row["end"] / hop) + 2)
        labels = [inventory.get(phone, 70) for phone, _ in row["phones"]]
        if right - left > 110 or not labels or len(labels) > 32:
            scored.append(None)
            continue
        local = logp[left:right].copy()
        local[:, 71] = np.maximum(local[:, 71], local[:, 69])
        # Each competing slot excludes its expected label, the blank and
        # the space. Forced alignment can form a path even for missing words.
        peaks = np.argsort(local[:, :69], axis=1)[:, -2:]
        frames = np.arange(len(local))
        competitors = np.stack([np.where(peaks[:, 1] != label, local[frames, peaks[:, 1]],
                                         local[frames, peaks[:, 0]]) for label in labels], axis=1)
        paths = np.concatenate([local, competitors], axis=1)
        expected = path_value(paths, labels, 71)
        unrestricted = path_value(paths, list(range(72, 72 + len(labels))), 71)
        silence = local[:, 71].sum(dtype=np.float64)
        scored.append({"impostor": float((expected - unrestricted) / len(labels)),
                       "silence": float((expected - silence) / len(labels))})
    return scored
