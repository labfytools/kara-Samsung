"""Project reference evidence on BlackBox's measured software clock."""
from base64 import b64decode
from bisect import bisect_right
from functools import lru_cache
from math import isfinite
from pathlib import Path
import subprocess

import numpy as np

from _kara_blackbox import digest, events
from _kara_blackbox_lyrics import load
from _kara_paths import music_library

SECOND = 1_000_000_000


def anchors(rows, turn, end):
    previous = None
    for row in rows:
        if row["monotonic_ns"] >= end + 2 * SECOND:
            break
        if row.get("turn") != turn:
            continue
        if row["event"] == "seek":
            previous = None
            continue
        if row["component"] != "audio" or row["event"] != "music-clock":
            continue
        data = row["data"]
        position, clock, rate = row.get("position"), data.get("strawberry_monotonic_ns"), data.get("playback_rate")
        if (type(position) not in (int, float) or not isfinite(position) or type(clock) is not int
                or type(rate) not in (int, float) or not 0 < rate <= 4):
            previous = None
            continue
        if previous:
            first, old, speed = previous
            elapsed = (clock - first) / SECOND
            # Extrapolation over seeks and missing observations creates
            # false evidence. Only continuous pairs with matching rate enter.
            if 0 < elapsed <= 2.5 and rate == speed and position > old and abs(position - old - rate * elapsed) <= .25:
                yield first, clock, old, position
        previous = clock, position, rate


def chapter_markers(lines, intervals, begin, end):
    def emit(stamp, kind, fields):
        if type(stamp) not in (int, float) or not isfinite(stamp):
            return
        for first, last, low, high in intervals:
            if low <= stamp < high:
                projected = round(first + (last - first) * (stamp - low) / (high - low))
                if begin <= projected < end:
                    yield {"monotonic_ns": projected, "position": stamp, "component": "chronos",
                           "event": kind, "data": fields}

    for index, line in enumerate(lines):
        onset = line.get("start")
        if onset is not None:
            yield from emit(onset, "line", {"index": index, "text": line["text"],
                                            "status": line.get("status", "historical")})
        for word in line.get("words", ()):
            item = {**word, "line": index}
            proposal = word.get("proposal")
            if proposal and word.get("start") is None:
                yield from emit(proposal.get("start"), "abstention", item)
            else:
                yield from emit(word.get("start"), "word-start", item)
                yield from emit(word.get("end"), "word-end", item)
            if proposal:
                for label, stamp in proposal.get("phones", ()):
                    yield from emit(stamp, "phone", {"label": label, "line": index,
                                                     "status": word.get("status")})


@lru_cache(maxsize=24)
def effective_hash(path, version):
    return digest(path)


def waveforms(document, intervals, begin, end, bins):
    if not intervals:
        return {}
    files = document["audio"]
    reference = files["original"]
    library = music_library()
    start, stop = min(row[2] for row in intervals), max(row[3] for row in intervals)
    if not 0 <= start < stop <= start + 45:
        return {}
    sources = [("musique", library, reference.get("library_path", ""), reference["sha256"])]
    result = {}
    for name, root, relative, expected in sources:
        try:
            path = (root / relative).resolve(strict=True)
            if not path.is_file() or not path.is_relative_to(root.resolve()):
                continue
            info = path.stat()
            if effective_hash(path, (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)) != expected:
                continue
            output = subprocess.check_output(("ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                "-threads", "1", "-ss", str(start), "-i", str(path), "-t", str(stop - start),
                "-map", "0:a:0", "-ac", "1", "-ar", "8000", "-f", "s16le", "-"), timeout=6)
            data = np.frombuffer(output, dtype="<i2")
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
        times = [first for first, _, _, _ in intervals]
        peaks = [(None, None)] * bins
        for i in range(bins):
            left = begin + (end - begin) * i // bins
            j = bisect_right(times, left) - 1
            if j < 0:
                continue
            first, last, low, high = intervals[j]
            if not first <= left < last:
                continue
            right = min(last, begin + (end - begin) * (i + 1) // bins)
            a = round((low - start + (left - first) / (last - first) * (high - low)) * 8000)
            b = round((low - start + (right - first) / (last - first) * (high - low)) * 8000)
            window = data[max(0, a):max(0, b)]
            if len(window):
                peaks[i] = (float(window.min()) / 32768, float(window.max()) / 32768)
        result[name] = peaks
    return result



def project(folder, turn, begin, end, bins):
    if not turn:
        return [], {}
    try:
        document = load(folder, turn)["reference"]
    except (OSError, ValueError):
        return [], {}
    intervals = [row for row in anchors(events(folder / "events.jsonl"), turn, end)
                 if row[0] < end and row[1] > begin]
    tracks = [("historical", document.get("historical"))]
    results = []
    for kind, track in tracks:
        if track:
            for row in chapter_markers(track["lines"], intervals, begin, end):
                row["data"]["tier"] = kind
                row["data"]["engine"] = track.get("engine", track.get("method", kind))
                results.append(row)
    sound = waveforms(document, intervals, begin, end, bins)
    return sorted(results, key=lambda row: row["monotonic_ns"]), sound
