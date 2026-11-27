"""Lecture des repères temporels LRC utilisés par l’affichage Strawberry."""
from bisect import bisect_left
import re
from unicodedata import normalize

MAX_LRC_BYTES = 64_000
STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]\s*$", re.I | re.M)
WORD_STAMP = re.compile(r"<\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?>")


def parse_lrc(text, *, sort=True):
    offset = OFFSET.search(text)
    offset_seconds = int(offset[1]) / 1000 if offset else 0
    lines = {}
    for line in text.splitlines():
        line = line.lstrip("\ufeff \t")
        stamps = []
        while (stamp := STAMP.match(line)):
            minutes, seconds, fraction = stamp.groups()
            time = int(minutes) * 60 + int(seconds) + int((fraction or "").ljust(3, "0")) / 1000
            stamps.append(max(0.0, time - offset_seconds))
            line = line[stamp.end():].lstrip()
        phrase = normalize("NFC", WORD_STAMP.sub("", line).strip())
        for time in stamps:
            old = lines.get(time)
            if old and phrase and phrase != old:
                lines[time] = old + "\n" + phrase
            elif not old:
                lines[time] = phrase
    return sorted(lines.items()) if sort else list(lines.items())


def source_orders(text):
    """A single-stamp LRC can carry correct lyric order despite inverted times."""
    chronological = parse_lrc(text)
    for row in text.splitlines():
        row = row.lstrip("\ufeff \t")
        stamp = STAMP.match(row)
        if stamp and STAMP.match(row[stamp.end():].lstrip()):
            # Multi-stamp refrains encode occurrences in the timestamps.
            return {"timestamps": chronological}
    physical = parse_lrc(text, sort=False)
    return ({"timestamps": chronological, "file": physical} if physical != chronological else
            {"timestamps": chronological})


INLINE_STAMP = re.compile(r"<(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?>")


def _milliseconds(stamp):
    minute, second, fraction = stamp.groups()
    return int(minute) * 60000 + int(second) * 1000 + int((fraction or "").ljust(3, "0"))


def playback_lrc(text):
    """Return (seconds, original text, (seconds, first, last) word markers).

    An enhanced marker preceding a word owns that written word. Repeated
    line stamps reuse the text; a word marker keeps its own absolute clock.
    """
    offset = OFFSET.search(text)
    shift = int(offset[1]) if offset else 0
    lines = {}
    for row in text.splitlines():
        row = row.lstrip("\ufeff \t")
        stamps = []
        while (stamp := STAMP.match(candidate := row.lstrip(" \t"))):
            stamps.append(_milliseconds(stamp))
            row = candidate[stamp.end():]
        if not stamps:
            continue
        parts, positions = [], []
        size = last = 0
        for mark in INLINE_STAMP.finditer(row):
            part = row[last:mark.start()]
            parts.append(part)
            size += len(part)
            positions.append((_milliseconds(mark), size))
            last = mark.end()
        parts.append(row[last:])
        phrase = "".join(parts)
        # Locate the next written word without altering any characters. A
        # trailing marker has no word to highlight. Multiple markers inside
        # one written word do not invent new text boundaries.
        written = tuple(re.finditer(r"\S+", phrase))
        starts = tuple(word.start() for word in written)
        marks = []
        for seconds, char in positions:
            index = bisect_left(starts, char)
            if index < len(written):
                word = written[index]
                marks.append((seconds, word.start(), word.end()))
        for seconds in dict.fromkeys(stamps):
            old, points = lines.get(seconds, ("", []))
            if old and phrase and phrase != old:
                skip = len(old) + 1
                points.extend((point, first + skip, end + skip) for point, first, end in marks)
                lines[seconds] = (old + "\n" + phrase, points)
            elif not old:
                lines[seconds] = (phrase, list(marks))

    ordered = sorted(lines)
    result = []
    for index, start in enumerate(ordered):
        phrase, points = lines[start]
        stop = ordered[index + 1] if index + 1 < len(ordered) else float("inf")
        # Keep each word on the occurrence containing its explicit time.
        markers = tuple(((point - shift) / 1000, first, end)
                        for point, first, end in sorted(set(points)) if start <= point < stop)
        result.append(((start - shift) / 1000, phrase, markers))
    return result


def has_word_timing(text):
    # A bracket alone does not establish a highlight. Use the same parser
    # as Kara so that misplaced or trailing markers cannot win a download.
    return bool(WORD_STAMP.search(text) and any(marks for _, _, marks in playback_lrc(text)))
