"""Réglages de minutage par morceau pour Paroles Strawberry."""
from bisect import bisect_left, bisect_right
from hashlib import sha256
import json
from math import ceil, isfinite
import os
from pathlib import Path
from tempfile import NamedTemporaryFile


TIMING_VERSION = 3
MAX_TIMING_STEP = .5


def line_signature(lines):
    encoded = json.dumps(lines, ensure_ascii=False, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def audio_signature(song):
    info = song.stat()
    return [info.st_size, info.st_mtime_ns]


class LyricTiming:
    def __init__(self, path):
        self.path = path
        try:
            entries = json.loads(path.read_text())
        except (OSError, ValueError):
            entries = {}
        self.entries = entries if isinstance(entries, dict) else {}

    def entry(self, song):
        item = self.entries.get(str(song), {})
        return item if isinstance(item, dict) else {}

    def offset(self, song):
        value = self.entry(song).get("offset", 0.0)
        return float(value) if isinstance(value, (float, int)) and isfinite(value) else 0.0

    def matches(self, song, lines):
        if song is None:
            return False
        entry = self.entry(song)
        if entry.get("signature") != line_signature(lines):
            return False
        try:
            return entry.get("audio") == audio_signature(song)
        except OSError:
            return False

    def current(self, song, lines):
        return self.entry(song).get("version") == TIMING_VERSION and self.matches(song, lines)

    def marks(self, song, lines):
        if self.entry(song).get("version") not in (2, TIMING_VERSION) or not self.matches(song, lines):
            return ()
        marks = self.entry(song).get("word_marks")
        if not isinstance(marks, list) or len(marks) != len(lines):
            return ()
        return tuple(dense_marks(points) for points in marks)

    def times(self, song, lines):
        entry = self.entry(song)
        stamps = entry.get("times")
        offset = self.offset(song)
        if (self.matches(song, lines)
                and isinstance(stamps, list) and len(stamps) == len(lines)
                and all(isinstance(t, (int, float)) and isfinite(t) for t in stamps)
                and all(a < b for a, b in zip(stamps, stamps[1:]))):
            return [time + offset for time in stamps]
        return [time + offset for time, _ in lines]

    def set_offset(self, song, offset):
        self.save(song, {**self.entry(song), "offset": round(offset, 2)})

    def save(self, song, entry):
        updated = {**self.entries, str(song): entry}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as draft:
                temp = Path(draft.name)
                json.dump(updated, draft, ensure_ascii=False, indent=2)
                draft.write("\n")
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.entries = updated


def dense_marks(points):
    if not points:
        return None
    result = [tuple(points[0])]
    for (start, first), (stop, last) in zip(points, points[1:]):
        steps = max(1, ceil((stop - start) / MAX_TIMING_STEP))
        for step in range(1, steps):
            fraction = step / steps
            result.append((start + (stop - start) * fraction, first + (last - first) * fraction))
        result.append((stop, last))
    return tuple(result)


def time_at_character(points, character):
    index = bisect_left(points, character, key=lambda point: point[1])
    if index == 0:
        return points[0][0]
    if index == len(points):
        return points[-1][0]
    start, first = points[index - 1]
    stop, last = points[index]
    return start + (stop - start) * (character - first) / (last - first)


def highlighted_characters(checkpoints, position):
    next_point = bisect_right(checkpoints, position, key=lambda point: point[0])
    if next_point == 0:
        return 0.0
    if next_point == len(checkpoints):
        return float(checkpoints[-1][1])
    a, start = checkpoints[next_point - 1]
    b, stop = checkpoints[next_point]
    return start + (stop - start) * (position - a) / (b - a)
