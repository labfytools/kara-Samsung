"""Prépare une transcription à écouter avant publication."""

from bisect import bisect_left
from contextlib import closing
import json
from math import isfinite
from pathlib import Path
import signal
import sys

from _kara_prepare import MAX_MANIFEST, PreparedCache, Preparer, atomic, failed_first_pass, file_token
from _kara_turbo import digest
from _kara_words import words
from _paroles_lrc import MAX_LRC_BYTES

NAME = "transcription.json"


def proposals(duration, heard, segments, *, review=None):
    """Only propose segment text supported by its own acoustic word times."""
    lines, last, omitted = [], -1, 0
    times = [word.start for word in heard]
    for segment in segments:
        start, end = segment["start"], segment["end"]
        if not 0 <= start < end <= duration or segment["no_speech"] > .55:
            continue
        text = " ".join(segment["text"].split())
        spelling = words(text)
        interval = heard[bisect_left(times, start - .08):bisect_left(times, end)]
        if (not spelling or spelling != [w.text for w in interval] or
                len(interval) < 2 or min(w.confidence for w in interval) < .55):
            omitted += 1
            if review is not None and len(review) < 20:
                review.append((start, end, text[:160]))
            continue
        ms = round(interval[0].start * 1000)
        if ms <= last:
            omitted += 1
            if review is not None and len(review) < 20:
                review.append((start, end, text[:160]))
            continue
        minute, remainder = divmod(ms, 60_000)
        second, fraction = divmod(remainder, 1000)
        lines.append((text, f"[{minute:02d}:{second:02d}.{fraction:03d}]{text}"))
        last = ms
    plain = "\n".join(line for line, _ in lines)
    timed = "\n".join(row for _, row in lines)
    if len(timed.encode()) > MAX_LRC_BYTES or len(plain.encode()) > MAX_LRC_BYTES:
        raise ValueError("Brouillon de transcription trop volumineux.")
    return plain, timed, omitted


def review_ranges(draft):
    """Project private observations into a bounded list for manual listening."""
    ranges = []
    try:
        observed = failed_first_pass(draft, draft["audio"]["sha256"], draft["model"], draft["settings"], "")
        if observed:
            duration, _, words, segments = observed
            proposals(duration, words, segments, review=ranges)
    except (KeyError, TypeError, ValueError):
        return ()
    return tuple(ranges)


def read_draft(cache, song):
    try:
        with (cache.folder(song) / NAME).open("rb") as stream:
            raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            return None
        value = json.loads(raw)
        if value.get("version") != 1 or value["audio"]["file"] != file_token(song):
            return None
        duration = value["duration"]
        if type(duration) not in (float, int) or not isfinite(duration) or not 1 <= duration <= 3600:
            return None
        if any(not isinstance(value[name], str) or len(value[name].encode()) > MAX_LRC_BYTES
               for name in ("plain", "timed")):
            return None
        return value
    except (OSError, TypeError, ValueError, KeyError):
        return None


def transcribe(cache, song, progress):
    with cache.locked(song) as folder:
        saved = read_draft(cache, song)
        with closing(Preparer(cache, require_vulkan=True)) as preparer:
            settings, model = preparer.settings(), cache.model_info()
            if saved and saved.get("settings") == settings and saved.get("model") == model:
                return saved
            token = file_token(song)
            audio_hash = digest(song)
            duration, language, heard, segments, _, _ = preparer.transcribe(song, "", progress)
            if file_token(song) != token or file_token(cache.model) != model["file"]:
                raise ValueError("Le morceau ou le modèle a changé pendant la transcription.")
            plain, timed, omitted = proposals(duration, heard, segments)
            value = dict(version=1, duration=duration, language=language, model=model, settings=settings,
                         audio={"file": token, "sha256": audio_hash},
                         observation={"input": "original", "acceleration": "vulkan"},
                         first_pass={"language_request": "", "words": [[w.text, w.start, w.end, w.confidence,
                                                                        w.end_origin] for w in heard],
                                     "segments": segments},
                         plain=plain, timed=timed, omitted=omitted)
            raw = json.dumps(value, ensure_ascii=False, allow_nan=False).encode()
            if len(raw) > MAX_MANIFEST:
                raise ValueError("Observations acoustiques trop volumineuses.")
            atomic(folder / NAME, raw)
            return value


def main():
    def cancel(*_):
        raise SystemExit(1)

    signal.signal(signal.SIGTERM, cancel)
    cache = PreparedCache(Path(sys.argv[1]))
    song = Path(sys.argv[2]).resolve(strict=True)
    data = transcribe(cache, song, lambda phase, percent: print(
        json.dumps({"event": "progress", "phase": phase, "percent": percent}), flush=True))
    print(json.dumps({"event": "draft", "lines": len(data["plain"].splitlines()), "omitted": data["omitted"]}), flush=True)


if __name__ == "__main__":
    main()
