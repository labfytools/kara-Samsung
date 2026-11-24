"""Alignement monotone et validation des références de karaoké."""
from array import array
from collections import defaultdict
from math import isfinite
from statistics import mean

from _kara_words import word_boundaries

VERSION = 2
METHOD = "turbo-dtw-monotone-v2"
MIN_TOKEN_CONFIDENCE = .35
MIN_WORD_COVERAGE = .70
MIN_LINE_COVERAGE = .70
MIN_TRANSCRIPT_AGREEMENT = .48
PRESERVE_SECONDS = .075


class RankTree:
    """Prefix maximum for sparse, ordered sequence alignment."""
    def __init__(self, length):
        self.ranks = array("H", [0]) * (length + 1)

    def before(self, column):
        rank = 0
        while column:
            rank = max(rank, self.ranks[column])
            column -= column & -column
        return rank

    def update(self, column, rank):
        column += 1
        while column < len(self.ranks):
            if rank <= self.ranks[column]:
                break
            self.ranks[column] = rank
            column += column & -column


def align_words(expected, heard):
    """Only retain a match if every maximum ordered alignment uses it.

    A match with forward and reverse ranks summing to optimum + 1 occurs
    at that exact rank in some optimal path. If several pairs can occupy
    that rank, none may provide a timestamp, even when their text agrees.
    """
    if max(len(expected), len(heard)) > 8192:
        raise ValueError("Trop de mots pour l’alignement automatique.")
    positions = defaultdict(list)
    for column, word in enumerate(heard):
        positions[word].append(column)
    if sum(len(positions[word]) for word in expected) > 400_000:
        raise ValueError("Trop d’occurrences concurrentes pour aligner les paroles.")
    tree = RankTree(len(heard))
    history = []
    for word in expected:
        columns = positions[word]
        ranks = array("H", (tree.before(j) + 1 for j in columns))
        for j, rank in zip(columns, ranks):
            tree.update(j, rank)
        history.append(ranks)
    optimum = tree.before(len(heard))
    tree = RankTree(len(heard))
    choices = {}
    ambiguous = set()
    for i in range(len(expected) - 1, -1, -1):
        columns = positions[expected[i]]
        ranks = array("H", (tree.before(len(heard) - 1 - j) + 1 for j in columns))
        for j, left, right in zip(columns, history[i], ranks):
            tree.update(len(heard) - 1 - j, right)
            if left + right == optimum + 1:
                if left not in choices:
                    choices[left] = (i, j)
                else:
                    ambiguous.add(left)
    return {i: j for rank, (i, j) in choices.items() if rank not in ambiguous}, optimum, len(ambiguous)


def lrc_text(lines):
    result = []
    for line in lines:
        ms = round(line["display_start"] * 1000)
        minute, ms = divmod(ms, 60_000)
        second, ms = divmod(ms, 1000)
        stamp = f"[{minute:02d}:{second:02d}.{ms:03d}]"
        result.extend(stamp + part for part in line["text"].split("\n"))
    return "\n".join(result) + "\n"


def alignment(rows, heard, duration, progress=lambda *_: None):
    # Include weak observations in the order check. A weak token may disambiguate
    # its neighbors but supplies no timestamp and earns no timing coverage.
    heard = [w for w in heard if 0 <= w.start < duration and isfinite(w.confidence)]
    bounds = [word_boundaries(text) for _, text in rows]
    if any(len(terms) > 256 for terms in bounds):
        raise ValueError("Une ligne LRC contient trop de mots.")
    expected = [word for terms in bounds for word, _, _ in terms]
    if not expected:
        raise ValueError("Aucune parole minutée exploitable.")
    progress("comparison", 77)
    matches, optimum, ambiguous = align_words(expected, [w.text for w in heard])
    progress("correction", 87)
    lines, cursor = [], 0
    for index, ((original, text), terms) in enumerate(zip(rows, bounds)):
        refs = []
        for j, (term, left, right) in enumerate(terms):
            observation = heard[matches[cursor]] if cursor in matches else None
            cursor += 1
            usable = observation is not None and observation.confidence >= MIN_TOKEN_CONFIDENCE
            refs.append({"id": j, "text": text[left:right], "normalized": term,
                         "characters": [left, right],
                         "start": observation.start if usable else None,
                         "end": observation.end if usable else None,
                         "confidence": observation.confidence if observation else None,
                         "origin": "whisper_dtw" if usable else "uncertain",
                         "end_origin": observation.end_origin if usable else None})
        lines.append({"id": index, "text": text, "source_start": original, "words": refs})
    progress("words", 93)
    return finish_alignment(lines, duration, {"observed_words": len(heard),
                            "transcript_agreement": optimum / max(1, len(heard)),
                            "ambiguous_ranks": ambiguous})


def finish_alignment(lines, duration, basis):
    """Rebuild display and acceptance from the retained acoustic word onsets."""
    count = total_words = 0
    for line in lines:
        refs = line["words"]
        known = sum(word["start"] is not None for word in refs)
        count += known
        total_words += len(refs)
        # The first word needs local support to establish the line onset.
        begin = (refs[0]["start"] if refs and known >= max(min(2, len(refs)), len(refs) * .55)
                 else None)
        if begin is not None and known == 1 and len(refs[0]["normalized"]) < 3:
            begin = None
        line.update(start=begin, end=refs[-1]["end"] if begin is not None else None,
                    confidence=round(known / len(refs), 4) if refs else None)
    # A display-only timestamp can fall back to a locally transformed LRC time.
    # Neither that fallback nor its implied word positions enters the score.
    anchors = []
    for i, line in enumerate(lines):
        begin = line["start"]
        if begin is None:
            continue
        if anchors and lines[anchors[-1]]["start"] + .001 > begin:
            line["start"] = line["end"] = None
            continue
        anchors.append(i)
    # Preserve a good LRC start when it remains between neighboring DTW anchors.
    for i, index in enumerate(anchors):
        line = lines[index]
        original, start = line["source_start"], line["start"]
        previous = lines[anchors[i - 1]]["display_start"] if i else -.001
        following = lines[anchors[i + 1]]["start"] if i + 1 < len(anchors) else duration
        line["display_start"] = (original if abs(start - original) <= PRESERVE_SECONDS
                                 and previous + .001 <= original <= following - .001 else start)
    for left, right in zip([-1, *anchors], [*anchors, len(lines)]):
        before = lines[left]["display_start"] if left >= 0 else -.001
        after = lines[right]["display_start"] if right < len(lines) else duration
        low = lines[left]["source_start"] if left >= 0 else 0.
        high = lines[right]["source_start"] if right < len(lines) else max(duration, lines[-1]["source_start"] + 1)
        source = [line["source_start"] for line in lines[left + 1:right]]
        increasing = all(a < b for a, b in zip(source, source[1:]))
        keep = increasing and all(before < stamp < after for stamp in source)
        interpolate = increasing and all(low < stamp < high for stamp in source)
        for index in range(left + 1, right):
            line = lines[index]
            if keep:
                moment = line["source_start"]
            elif interpolate:
                fraction = (line["source_start"] - low) / (high - low)
                moment = before + (after - before) * fraction
            else:
                moment = before + (after - before) * (index - left) / (right - left)
            line["display_start"] = max(0, moment)
    stamps = [round(max(0, line["display_start"]), 3) for line in lines]
    chronologic = all(a < b for a, b in zip(stamps, stamps[1:])) and stamps[-1] < duration
    for line, stamp in zip(lines, stamps):
        line["display_start"] = stamp
        line["start_origin"] = (line["words"][0]["origin"] if line["start"] is not None else
                                "source_lrc" if line["source_start"] == stamp else "display_only")
    for line, following in zip(lines, stamps[1:] + [duration]):
        if line["end"] is not None and not line["start"] < line["end"] <= following:
            line["end"] = None
    delta = [abs(line["start"] - line["source_start"]) for line in lines if line["start"] is not None]
    changed = [line for line in lines if line["start"] is not None
               and line["display_start"] != line["source_start"]]
    lexical_lines = sum(bool(line["words"]) for line in lines)
    coverage = count / total_words
    line_coverage = len(anchors) / lexical_lines
    agreement = basis["transcript_agreement"]
    report = {**basis, "lines": lexical_lines, "words": total_words,
              "matched_words": count, "word_coverage": round(coverage, 4),
              "line_coverage": round(line_coverage, 4), "transcript_agreement": round(agreement, 4),
              "uncertain_words": total_words - count, "uncertain_lines": lexical_lines - len(anchors),
              "changed": len(changed),
              "display_only_changes": sum(line["start"] is None and line["display_start"] != line["source_start"]
                                          for line in lines),
              "mean_delta": round(mean(delta), 3) if delta else None,
              "max_delta": round(max(delta), 3) if delta else None,
              "examples": [{"line": line["id"] + 1, "before": line["source_start"],
                            "after": line["display_start"]} for line in changed[:6]]}
    ready = (chronologic and coverage >= MIN_WORD_COVERAGE and line_coverage >= MIN_LINE_COVERAGE
             and agreement >= MIN_TRANSCRIPT_AGREEMENT)
    return lines, report, ready


def display_marks(line):
    """Character highlighting is cosmetic; the scorer consumes DTW onsets."""
    if line["start"] is None:
        return None
    # A partial line gets a line-level display. Filling missing word marks
    # here would make the visual interpolation resemble measured timings.
    if any(word["start"] is None for word in line["words"]):
        return None
    points = []
    distinct = {tuple(word["characters"]): word for word in line["words"]}
    words = list(distinct.values())
    for word, following in zip(words, [*words[1:], None]):
        begin, end = word["start"], word["end"]
        left, right = word["characters"]
        next_start = following["start"] if following else end
        if points and begin < points[-1][0]:
            return None
        points.append((begin, left))
        if end is not None and next_start is not None:
            points.append((min(end, next_start), right))
    return tuple(points) if len(points) >= 2 else None
