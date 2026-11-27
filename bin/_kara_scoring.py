"""Compare les mots du micro à ceux de la ligne en cours."""
from dataclasses import dataclass
import re

SCORE_TOLERANCE = 1.0
MAX_PREPARED_WINDOW = 30.

HELD_VOWEL = re.compile(r"([aeiouy])\1{2,}")


def same_word(expected, heard):
    if expected == heard:
        return True
    if len(expected) < 3 or not HELD_VOWEL.search(heard):
        return False
    return expected in (HELD_VOWEL.sub(r"\1", heard), HELD_VOWEL.sub(r"\1\1", heard))


@dataclass(frozen=True)
class AudioWindow:
    line: int
    start: float
    stop: float
    rate: float
    word_times: tuple = ()

    def observed(self, spans, tokenize):
        heard = []
        for begin, end, text in spans:
            tokens = tokenize(text)
            for index, word in enumerate(tokens):
                middle = begin + (end - begin) * (index + .5) / len(tokens)
                point = self.start + middle * self.rate
                if self.start <= point < self.stop:
                    heard.append((point, word))
        heard.sort(key=lambda item: item[0])
        return heard

    def evidence(self, expected, heard, *, tolerance=None):
        # Préfère le mot le plus proche de son repère lorsqu'il se répète.
        calibrated = len(self.word_times) == len(expected)
        row = [(0, 0., ())] * (len(heard) + 1)
        for index, target in enumerate(expected):
            previous, row = row, [(0, 0., ())]
            for column, (point, word) in enumerate(heard, 1):
                best = max(previous[column], row[-1])
                distance = abs(point - self.word_times[index]) if calibrated else 0.
                if same_word(target, word) and (tolerance is None or distance <= tolerance):
                    count, cost, indices = previous[column - 1]
                    cost -= min(4, distance / (tolerance or SCORE_TOLERANCE))
                    best = max(best, (count + 1, cost, indices + ((index, point),)))
                row.append(best)
        return row[-1][2]

    def matches(self, expected, heard):
        return tuple(index for index, _ in self.evidence(expected, heard))


def prepared_window(index, start, end, duration, rate, onsets, tolerance, *, post_roll=0.):
    """Use the same sampling envelope for live capture and file qualification."""
    valid = [time for time in onsets if time is not None]
    if not valid:
        return None
    start, end = min(start, valid[0]), max(end, valid[-1] + .2)
    if duration:
        end = min(duration, end)
    if end <= max(0., start):
        return None
    context = max(1.5, tolerance)
    head = max(0., start - context)
    tail = min(duration, end + context) if duration else end + context
    # Qualification may add trailing PCM while retaining the production
    # word clock, tolerance and 30-second admission bound.
    if post_roll and tail <= head + MAX_PREPARED_WINDOW:
        tail = min(tail + post_roll, head + MAX_PREPARED_WINDOW)
        if duration:
            tail = min(tail, duration)
    return AudioWindow(index, head, tail, rate, onsets)


def add_evidence(confirmed, index, point, expected):
    """Ajoute un mot sans reprendre une occurrence déjà créditée au même endroit."""
    if index in confirmed:
        return
    for other, time in confirmed.items():
        if expected[index] == expected[other] and abs(point - time) <= .24:
            return
        if (index - other) * (point - time) < 0 and abs(point - time) > .15:
            return
    confirmed[index] = point


MIN_RECOGNITION_CONFIDENCE = .50
MIN_ROUND_COVERAGE = .70
TIMING_EPSILON = 1e-8  # Tolerate round-off on the inclusive sample-clock boundary.


def outside_tolerance(delta, tolerance):
    return abs(delta) > tolerance + TIMING_EPSILON


def score_reference_line(reference, observed, tolerance=SCORE_TOLERANCE, *, source="voice", audit=None, lexical_only_allowed=False):
    """Align one independently decoded line and retain lexical/timing evidence."""
    words = reference["words"]
    if source != "voice":
        return [{"id": word["id"], "expected": word["text"],
                 "expected_start": word["start"],
                 "status": "missing" if source == "silence" and (lexical_only_allowed or word["start"] is not None) else "uncertain",
                 "reason": "reference" if word["start"] is None and not lexical_only_allowed else source}
                for word in words]
    # Minimize lexical edits first. For repeated words, prefer in-tolerance
    # matches, then the nearest occurrence on the common musical clock.
    n, m = len(words), len(observed)
    candidates = []
    omitted = 0
    cells = [[None] * (m + 1) for _ in range(n + 1)]
    cells[0][0] = ((0, 0, 0.), "")
    for i in range(1, n + 1):
        cells[i][0] = ((i, 0, 0.), "missing")
    for j in range(1, m + 1):
        cells[0][j] = ((j, 0, 0.), "extra")
    for i, word in enumerate(words, 1):
        start = word["start"]
        for j, item in enumerate(observed, 1):
            equal = same_word(word["normalized"], item["word"])
            delta = abs(item["start"] - start) if start is not None else 0.
            edits, outside, distance = cells[i - 1][j - 1][0]
            above, left = cells[i - 1][j][0], cells[i][j - 1][0]
            # An uncertain lexical candidate must not displace a supported
            # match to another occurrence of the same normalized word.
            weak = item["confidence"] < MIN_RECOGNITION_CONFIDENCE
            if audit is not None and equal:
                # Observe the actual comparison inputs while the recurrence
                # still owns them. The bounded trace does not steer a match.
                if len(candidates) < 256:
                    candidates.append({"expected_id": word["id"], "observed_index": j - 1,
                                       "delta_s": None if start is None else item["start"] - start,
                                       "supported": start is not None and not weak,
                                       "within_tolerance": start is not None and not outside_tolerance(delta, tolerance)})
                else:
                    omitted += 1
            cells[i][j] = min(
                ((edits + (not equal), outside + (2 if weak or start is None
                                                   else outside_tolerance(delta, tolerance)),
                  distance + min(delta, 60)), "match" if equal else "incorrect"),
                ((above[0] + 1, above[1], above[2]), "missing"),
                ((left[0] + 1, left[1], left[2]), "extra"))
    aligned = []
    if audit is not None:
        audit.update(candidates=candidates, omitted=omitted)
    i, j = n, m
    while i or j:
        action = cells[i][j][1]
        word = words[i - 1] if action != "extra" else None
        item = observed[j - 1] if action != "missing" else None
        if item is not None:
            j -= 1
        if word is not None:
            i -= 1
        result = {"id": word["id"] if word else None, "expected": word["text"] if word else None,
                  "status": action}
        if item is not None:
            result.update(heard=item["word"], start=item["start"], end=item.get("end"),
                          confidence=item["confidence"])
            if item["confidence"] < MIN_RECOGNITION_CONFIDENCE:
                result["status"] = "uncertain"
                result["reason"] = "confidence"
        if word is not None:
            start = word["start"]
            result["expected_start"] = start
            if start is None:
                if lexical_only_allowed:
                    if action == "match" and result["status"] != "uncertain":
                        result["status"] = "correct"
                else:
                    result["status"] = "uncertain"
                    result["reason"] = "reference"
            elif item is not None:
                delay = item["start"] - start
                result["delay"] = round(delay, 4)
                if action == "match" and result["status"] != "uncertain":
                    result["status"] = (("early" if delay < 0 else "late")
                                        if outside_tolerance(delay, tolerance) else "correct")
        aligned.append(result)
    return list(reversed(aligned))


def score_breakdown(reference, results):
    total = sum(len(line["words"]) for line in reference["lines"])
    original_lrc = reference.get("method") == "lrc-original"
    eligible = total if original_lrc else sum(
        word["start"] is not None for line in reference["lines"] for word in line["words"])
    verified, uncertain, correct, lexical, extras = 0, 0, 0, 0, 0
    timed, in_time = 0, 0
    for index, line in results.items():
        words = reference["lines"][index]["words"]
        for result in line:
            status = result["status"]
            if result["id"] is None:
                extras += status == "extra"
                continue
            has_onset = words[result["id"]]["start"] is not None
            if not has_onset and not original_lrc:
                continue
            timed += has_onset and status in ("correct", "early", "late")
            in_time += has_onset and status == "correct"
            uncertain += status == "uncertain"
            verified += status != "uncertain"
            correct += status == "correct"
            lexical += status in ("correct", "early", "late")
    summary = score_summary(total, eligible, verified, uncertain, correct, lexical, extras)
    if original_lrc:
        # Line-only words can earn lexical credit but provide no timing evidence.
        summary["timing"] = round(100 * in_time / timed) if timed else None
    return summary


def score_summary(total, eligible, verified, uncertain, correct, lexical, extras):
    # Live scoring and replay share the same coverage gate over all written
    # words, including lines the player never reached. Insertions are reported
    # separately because a decoder can repeat across overlapping windows.
    scorable = bool(total and verified / total >= MIN_ROUND_COVERAGE)
    return {"score": round(100 * correct / verified) if scorable else None,
            "lyrics": round(100 * lexical / verified) if verified else 0,
            "timing": round(100 * correct / lexical) if lexical else None,
            "coverage": 100 * verified // total if total else 0,
            "reference_coverage": 100 * eligible // total if total else 0,
            "total": total, "eligible": eligible, "verified": verified, "uncertain": uncertain,
            "correct": correct, "incorrect": verified - correct, "extras": extras,
            "unevaluable": total - verified, "unreliable_reference": total - eligible,
            "not_assessed": eligible - verified - uncertain, "lexical_correct": lexical}
