"""Produce bounded, min/max waveform tiles for Studio."""
import json

import numpy as np

from _kara_blackbox import TRACK_NAME, events, private_file, wave_layout

BINS = 320
MAX_EVENTS = 160
TRACKS = ("wifi", "playback", "monitor", "whisper", "return", "witness")
SECOND = 1_000_000_000


def read_json(path):
    with open(path, "r", opener=private_file, encoding="utf-8") as source:
        return json.load(source)


def tiles(folder, turn, origin, start, seconds, recalculated=None):
    """Summarize publication time; raw source clocks remain in event detail."""
    elapsed = round(start * SECOND)
    begin = origin + elapsed
    end = begin + round(seconds * SECOND)
    lo, hi = {}, {}
    listed, layouts = [], {}
    total = 0
    score_marks = []
    for row in events(folder / "events.jsonl"):
        stamp = row["monotonic_ns"]
        if stamp < begin:
            continue
        if stamp >= end:
            break
        if turn and row.get("turn") != turn:
            continue
        data = row.get("data", {})
        if row["event"] != "pcm":
            total += 1
            if len(listed) < MAX_EVENTS:
                listed.append(row)
            if row["event"] == "line-decision" and len(score_marks) < MAX_EVENTS:
                score_marks.append({"component": row["component"], "event": "score-decision",
                                    "monotonic_ns": stamp,
                                    "data": {"tier": "notation historique", "line": data.get("line")}})
            continue
        track = data["track"]
        if track not in TRACKS:
            continue
        name = data["file"]
        if (not name.startswith("audio/") or not TRACK_NAME.fullmatch(name[6:]) or (folder / "audio").is_symlink()):
            continue
        path = folder / name
        if path not in layouts:
            layouts[path] = wave_layout(path)
        rate, channels, frame_count, width, header = layouts[path]
        first, length = data["sample"], data["frames"]
        if not 0 <= first < first + length <= frame_count or length * width * channels > 65536:
            continue
        with open(path, "rb", opener=private_file) as stream:
            stream.seek(header + first * width * channels)
            pcm = stream.read(length * width * channels)
        if len(pcm) != length * width * channels:
            continue
        sample = np.frombuffer(pcm, dtype="<f4" if width == 4 else "<i2")
        if not np.isfinite(sample).all():
            continue
        scale = 1 if width == 4 else 32768
        idx = (stamp - begin) * BINS // (end - begin)
        if track not in lo:
            lo[track], hi[track] = [None] * BINS, [None] * BINS
        low, high = float(sample.min()) / scale, float(sample.max()) / scale
        lo[track][idx] = low if lo[track][idx] is None else min(lo[track][idx], low)
        hi[track][idx] = high if hi[track][idx] is None else max(hi[track][idx], high)
    from _kara_blackbox_lyric_clock import project

    markers, references = project(folder, turn, begin, end, BINS, recalculated)
    peaks = {**references, **{name: list(zip(lo[name], hi[name])) for name in TRACKS if name in lo}}
    return {"peaks": peaks, "markers": markers + score_marks,
            "events": sorted(listed + [row for row in markers if row["event"] != "phone"][:MAX_EVENTS],
                             key=lambda row: row["monotonic_ns"]),
            "total": total + sum(row["event"] != "phone" for row in markers),
            "begin": begin, "end": end, "origin": origin}
