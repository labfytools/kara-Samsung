"""Consolidate local, unprompted Turbo observations around fixed word anchors."""
from bisect import bisect_left, bisect_right, insort
from copy import deepcopy
from dataclasses import dataclass
from statistics import median

from _kara_reference import MIN_LINE_COVERAGE, MIN_TRANSCRIPT_AGREEMENT, align_words, finish_alignment
from _kara_turbo import RATE, pcm_file

VERSION = 1
TARGET_COVERAGE = .95
MAX_WINDOWS = 28
MAX_STEM_WINDOWS = 6
MIN_LOCAL_CONFIDENCE = .55
MAX_DISAGREEMENT = .30


@dataclass(frozen=True)
class Gap:
    first: int
    stop: int
    low: float
    high: float
    audit: bool


def audit_prefixes(lines):
    """Defer an isolated onset when the body and nearby LRC drift contradict it.

    The source LRC is only used to select conflicting evidence for review.
    A sustained last token with a measured extent is sheltered from the rule.
    """
    withheld = {}
    for line_idx, line in enumerate(lines):
        known = [w for w in line["words"] if w["start"] is not None]
        if len(known) < 4:
            continue
        near = [item["start"] - item["source_start"] for item in lines[max(0, line_idx - 4):line_idx + 5]
                if item is not line and item["start"] is not None
                and abs(item["source_start"] - line["source_start"]) <= 45]
        if len(near) < 3:
            continue
        predicted = line["source_start"] + median(near)
        for word in line["words"]:
            if word["start"] is None and word.get("rejected", {}).get("reason") == "isolated_prefix":
                withheld[line_idx, word["id"]] = predicted
        if len(known) < 5 or line["start"] is None:
            continue
        for count in (1, 2):
            before, after = known[count - 1], known[count]
            if (after["id"] - before["id"] <= 2 and after["start"] - before["start"] > 3.0
                    and (before["end"] is None or after["start"] - before["end"] > 2.5)
                    and predicted - before["start"] > 2.5 and abs(after["start"] - predicted) <= 1.25
                    and known[count + 2]["start"] - after["start"] <= 2.0):
                for word in known[:count]:
                    withheld[(line_idx, word["id"])] = predicted
                    word["rejected"] = {"start": word["start"], "confidence": word["confidence"],
                                        "reason": "isolated_prefix"}
                    word.update(start=None, end=None, confidence=None, origin="uncertain", end_origin=None)
                break
    return withheld


def target_gaps(entries, lines, duration, withheld):
    active = [i for i, (_, w) in enumerate(entries) if w["start"] is not None]
    for left, right in zip([-1, *active], [*active, len(entries)]):
        if right <= left + 1:
            continue
        low = entries[left][1]["start"] + .019 if left >= 0 else 0.
        high = entries[right][1]["start"] - .019 if right < len(entries) else duration
        if high - low < .035:
            continue
        first = left + 1
        if high - low <= 6.0 and not any(
                (line, word["id"]) in withheld for line, word in entries[first:right]):
            yield Gap(first, right, low, high, False)
            continue
        while first < right:
            line_idx = entries[first][0]
            stop = first + 1
            while stop < right and entries[stop][0] == line_idx:
                stop += 1
            flags = [withheld[(line_idx, word["id"])] for _, word in entries[first:stop]
                     if (line_idx, word["id"]) in withheld]
            if lines[line_idx]["source_start"] is None:
                # Without a reference time, a long window cannot localize a
                # phrase safely. Short intervals between words are still tried.
                first = stop
                continue
            shift = [item["start"] - item["source_start"] for item in lines[max(0, line_idx - 5):line_idx + 6]
                     if item["start"] is not None]
            # Long gaps can span an instrumental. The LRC helps select where
            # to decode, but it never becomes a word timestamp.
            hint = median(flags) if flags else lines[line_idx]["source_start"] + (median(shift) if shift else 0.)
            lo, hi = max(low, hint - (1.25 if flags else 2.0)), min(high, hint + (1.25 if flags else 5.0))
            if hi - lo >= .035:
                yield Gap(first, stop, lo, hi, bool(flags))
            first = stop


def group_gaps(gaps, entries, limit):
    windows = []
    for gap in sorted(gaps, key=lambda x: (x.low, x.high, x.first)):
        if (windows and not gap.audit and not windows[-1][2][0].audit
                and max(windows[-1][1], gap.high) - windows[-1][0] <= 9.0):
            low, high, members = windows[-1]
            windows[-1] = low, max(high, gap.high), [*members, gap]
        else:
            windows.append((gap.low, gap.high, [gap]))
    ordered = sorted(windows, key=lambda w: (
        -sum(g.audit for g in w[2]),
        -sum(entries[g.first][1]["id"] == 0 for g in w[2]),
        -sum(g.stop - g.first for g in w[2]), w[0]))
    return ordered[:limit]


def two_observations(decoder, pcm, language, low, high, members):
    # Suspect prefixes need the body of their own line in the decode window.
    # A narrow clip ending at the next known word can turn it into an orphan again.
    if members[0].audit:
        point = round(high)
        windows = ((point - 12, point + 4), (point - 3, point + 4))
    else:
        windows = ((low - .80, high + 1.15), (low - 1.65, high + .70))
    samples, read = pcm
    bounds = dict.fromkeys((max(0, round(start * RATE)), min(samples, round(stop * RATE)))
                           for start, stop in windows)
    # The two margins may collapse to one identical frame on a very short
    # file. Decode that frame once and keep it as a single observation.
    decoded = [observe(decoder, read, language, start, stop, beam=not members[0].audit)
               for start, stop in bounds if stop - start >= RATE]
    count = len(decoded)
    decoded.extend([()] * (2 - count))
    return decoded[0], decoded[1], count


def observe(decoder, read, language, start, stop, *, beam=False):
    _, spans = decoder.decode(read(start, stop), language, start / RATE, beam=beam)
    return sorted((word for span in spans if span.no_speech <= .65 for word in span.words),
                  key=lambda word: word.start)


def candidates(gap, entries, anchors, observed):
    local = [word for word in observed if gap.low <= word.start <= gap.high
             and word.confidence >= MIN_LOCAL_CONFIDENCE]
    nearby = anchors[max(0, bisect_left(anchors, gap.first) - 4):bisect_right(anchors, gap.stop - 1) + 4]
    # Re-identify a known neighboring word in this very transcript. The time
    # fence and the unique ordered match rule both apply to the unknown run.
    supported = any(abs(item.start - anchor["start"]) <= .42 and item.text == anchor["normalized"]
                    for index in nearby for anchor in (entries[index][1],) for item in observed
                    if item.confidence >= MIN_LOCAL_CONFIDENCE)
    if not supported or not local:
        return {}
    matches, _, _ = align_words([word["normalized"] for _, word in entries[gap.first:gap.stop]],
                                [word.text for word in local])
    return {gap.first + index: local[column] for index, column in matches.items()}


def consolidate(entries, gap, first, second, source, known):
    added = 0
    for index in range(gap.first, gap.stop):
        _, ref = entries[index]
        if ref["start"] is not None:
            continue
        a, b = first.get(index), second.get(index)
        position = bisect_left(known, index)
        lower = entries[known[position - 1]][1]["start"] if position else -1.
        upper = entries[known[position]][1]["start"] if position < len(known) else float("inf")
        if (a is not None and b is not None and lower <= a.start <= upper and lower <= b.start <= upper
                and abs(a.start - b.start) <= MAX_DISAGREEMENT):
            confidence = min(a.confidence, b.confidence)
            if max(a.confidence, b.confidence) >= .70:
                # Prefer the first observed DTW time. Averaging would invent
                # a point that neither decoder reported. A disputed end is
                # left open even when the onset is reproducible.
                end = (a.end if a.end is not None and b.end is not None
                       and abs(a.end - b.end) <= MAX_DISAGREEMENT else None)
                ref.update(start=a.start, end=end, confidence=confidence, origin=source,
                           end_origin=a.end_origin if end is not None else None,
                           evidence={"alternate_start": b.start,
                                     "difference": round(abs(a.start - b.start), 4)})
                ref.pop("candidate", None)
                insort(known, index)
                added += 1
                continue
        # A single lexical hit is useful for annotation, but has no scoring time.
        proposal = a or b
        if proposal is not None and proposal.confidence >= .70 and "candidate" not in ref:
            ref["candidate"] = {"start": proposal.start, "confidence": proposal.confidence, "origin": source}
    return added


def refine(song, lines, basis, duration, language, decoder, *, vocal_stem=None,
           audit_only=False, progress=lambda *_: None):
    lines = deepcopy(lines)
    timed_source = lines[0]["source_start"] is not None
    withheld = audit_prefixes(lines) if timed_source else {}
    entries = [(i, word) for i, line in enumerate(lines) for word in line["words"]]
    n_known = sum(w["start"] is not None for _, w in entries)
    report = {"version": VERSION, "windows": 0, "decodes": 0, "stem_windows": 0,
              "recovered_mix": 0, "recovered_stem": 0, "quarantined": len(withheld)}
    # An untimed source has no inherited onsets. High word coverage can
    # justify skipping retries only when every line already has evidence.
    enough_lines = (basis["line_coverage"] >= MIN_LINE_COVERAGE if timed_source
                    else basis["uncertain_lines"] == 0)
    if not withheld and (basis["word_coverage"] >= TARGET_COVERAGE and enough_lines or n_known < 3
                         or basis["transcript_agreement"] < MIN_TRANSCRIPT_AGREEMENT):
        finished, summary, ready = finish_alignment(lines, duration, basis)
        return finished, summary, ready, report
    gaps = list(target_gaps(entries, lines, duration, withheld))
    if audit_only:
        gaps = [gap for gap in gaps if gap.audit]
    windows = group_gaps(gaps, entries, MAX_WINDOWS)
    if windows:
        progress("refinement", 94)
        engine = decoder()
        anchors = [i for i, (_, word) in enumerate(entries) if word["start"] is not None]
        known = anchors.copy()
        audits_left = sum(gap.audit for _, _, members in windows for gap in members)
        # Merge one gap once. Multiple windows supply corroboration, not
        # multiple scoring events or further rounds of weak anchors.
        with pcm_file(song) as pcm:
            for low, high, members in windows:
                primary, secondary, decodes = two_observations(engine, pcm, language, low, high, members)
                report["windows"] += 1
                report["decodes"] += decodes
                for gap in members:
                    audits_left -= gap.audit
                    first = candidates(gap, entries, anchors, primary)
                    second = candidates(gap, entries, anchors, secondary)
                    report["recovered_mix"] += consolidate(entries, gap, first, second, "whisper_local_mix", known)
                if enough_lines and not audits_left and (n_known + report["recovered_mix"]) / len(entries) >= TARGET_COVERAGE:
                    break
        pending = list(target_gaps(entries, lines, duration, withheld))
        still_under_target = not enough_lines or (n_known + report["recovered_mix"]) / len(entries) < TARGET_COVERAGE
        stem = vocal_stem() if pending and vocal_stem and still_under_target and not audit_only else None
        stem_windows = group_gaps(pending, entries, MAX_STEM_WINDOWS) if stem else ()
        if stem_windows:
            progress("vocal_refinement", 96)
            engine = decoder()
            with pcm_file(stem) as pcm:
                if abs(pcm[0] / RATE - duration) <= .085:
                    for low, high, members in stem_windows:
                        primary, secondary, decodes = two_observations(engine, pcm, language, low, high, members)
                        report["stem_windows"] += 1
                        report["decodes"] += decodes
                        for gap in members:
                            first = candidates(gap, entries, anchors, primary)
                            second = candidates(gap, entries, anchors, secondary)
                            report["recovered_stem"] += consolidate(entries, gap, first, second,
                                                                   "whisper_local_vocals", known)
                        recovered = report["recovered_mix"] + report["recovered_stem"]
                        if enough_lines and (n_known + recovered) / len(entries) >= TARGET_COVERAGE:
                            break
    finished, summary, ready = finish_alignment(lines, duration, basis)
    return finished, summary, ready, report
