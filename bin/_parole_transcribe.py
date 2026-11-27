"""Prépare une transcription à écouter avant publication."""

from bisect import bisect_left
from collections import defaultdict
from contextlib import closing
from hashlib import sha256
import json
from math import isfinite
from pathlib import Path
import signal
import sys

from _kara_prepare import MAX_MANIFEST, PreparedCache, Preparer, atomic, failed_first_pass, file_token
from _kara_turbo import RATE, digest
from _kara_words import words
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc

NAME = "transcription.json"
VERSION = 2
MAX_COMPARISONS = 6


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


def repeated_lines(text):
    lines = [tuple(words(line)) for line in text.splitlines()]
    occurrences, pairs = defaultdict(list), defaultdict(list)
    for i, line in enumerate(lines):
        if len(line) >= 2:
            occurrences[line].append(i + 1)
            if i + 1 < len(lines) and len(lines[i + 1]) >= 2:
                pairs[line, lines[i + 1]].append(i + 1)
    return {
        "repeated_lines": [positions[:20] for positions in occurrences.values() if len(positions) >= 2][:20],
        "possible_choruses": [positions[:20] for positions in pairs.values()
                              if positions[-1] - positions[0] >= 2][:20],
    }


def compare_windows(decoder, read, result, progress):
    duration, language, heard, segments = result
    ranges = []
    proposals(duration, heard, segments, review=ranges)
    comparisons = []
    for start, end, initial in ranges:
        if len(comparisons) >= MAX_COMPARISONS:
            break
        if not initial or end - start > 7:
            continue
        low, high = max(0, round((start - .4) * RATE)), min(round(duration * RATE), round((end + .4) * RATE))
        progress("comparison", 77 + len(comparisons) * 2)
        _, spans = decoder.decode(read(low, high), language if language != "auto" else "", low / RATE, beam=True)
        # Only observations in the original interval inform a suggestion.
        # A second pass never adds words to the draft that is used in Kara.
        witness = [word for span in spans if span.no_speech <= .55 for word in span.words
                   if start - .08 <= word.start < end and word.confidence >= .55]
        alternative = " ".join(word.text for word in witness)[:160]
        agreement = ("concordant" if words(alternative) and words(initial) == words(alternative) else
                     "divergent" if alternative else "insufficient")
        comparisons.append({"start": start, "end": end, "first": initial, "alternate": alternative,
                            "decision": agreement})
    return comparisons


def provenance(draft):
    timed = draft["timed"]
    model = draft.get("model", {})
    uncertainty = draft["omitted"] if "omitted" in draft else len(review_ranges(draft))
    supported = len(parse_lrc(timed))
    level = "partial" if supported else "insufficient"
    return {"kind": "generated-transcription", "status": "experimental", "reliability": level,
            "sha256": sha256((draft["plain"] + "\0" + timed).encode()).hexdigest(),
            "model": model.get("name", "large-v3-turbo"), "model_sha256": model.get("sha256"),
            "decoder": draft.get("settings", {}).get("backend", "whisper.cpp-dtw"),
            "language": draft.get("language"), "supported_lines": supported, "uncertain_segments": uncertainty,
            "compared_segments": len(draft.get("comparisons", ())), "input": "original"}


def draft_source(cache, song, draft=None):
    draft = read_draft(cache, song) if draft is None else draft
    if draft and draft["plain"].strip():
        return {"kind": "generated-transcription", "provider": "Turbo · brouillon expérimental",
                "original": draft["plain"], "timed": False, "transcription": provenance(draft)}


def draft_display(cache, song, before):
    draft = read_draft(cache, song)
    if not draft or not (source := draft_source(cache, song, draft)):
        return None
    return {"engine": "Turbo", "status": "experimental", "tokens": before,
            "audio_sha256": draft["audio"]["sha256"], "source": source,
            "lines": [{"start": stamp, "text": line, "words": [], "status": "experimental"}
                      for stamp, line in parse_lrc(draft["timed"]) if line]}


def correction_input(draft):
    ranges = review_ranges(draft)
    alternatives = {(item["start"], item["end"]): item["alternate"] for item in draft.get("comparisons", ())}
    return [{"id": i, "first": text, "alternate": alternatives.get((start, end), "")}
            for i, (start, end, text) in enumerate(ranges[:12]) if text]

def correction_identity(song_id, draft):
    from _parole_index import encoded_key

    return encoded_key("draft_correction", song_id, draft["audio"]["sha256"],
                       provenance(draft)["sha256"], correction_input(draft))


def read_draft(cache, song):
    try:
        with (cache.folder(song) / NAME).open("rb") as stream:
            raw = stream.read(MAX_MANIFEST + 1)
        if len(raw) > MAX_MANIFEST:
            return None
        value = json.loads(raw)
        if value.get("version") not in (1, VERSION) or value["audio"]["file"] != file_token(song):
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
            if (saved and saved.get("version") == VERSION
                    and saved.get("settings") == settings and saved.get("model") == model):
                return saved
            token = file_token(song)
            audio_hash = digest(song)
            comparisons = []
            duration, language, heard, segments, _, _ = preparer.transcribe(
                song, "", progress, review=lambda decoder, read, result: comparisons.extend(
                    compare_windows(decoder, read, result, progress)))
            if file_token(song) != token or file_token(cache.model) != model["file"]:
                raise ValueError("Le morceau ou le modèle a changé pendant la transcription.")
            plain, timed, omitted = proposals(duration, heard, segments)
            value = dict(version=VERSION, duration=duration, language=language, model=model, settings=settings,
                         audio={"file": token, "sha256": audio_hash},
                         observation={"input": "original", "acceleration": "vulkan"},
                         first_pass={"language_request": "", "words": [[w.text, w.start, w.end, w.confidence,
                                                                        w.end_origin] for w in heard],
                                     "segments": segments},
                         plain=plain, timed=timed, omitted=omitted,
                         comparisons=comparisons, structure=repeated_lines(plain))
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
    chronos = "unavailable"
    if (source := draft_source(cache, song, data)) and data["duration"] < 600:
        from _kara_chronos import prepare
        from _parole_process import Interrupted

        print(json.dumps({"event": "progress", "phase": "chronos", "percent": 90}), flush=True)
        try:
            chronos = prepare(song, cache, "huang", supplied=source, allow_demucs=False)
        except (OSError, ValueError, RuntimeError, KeyError, Interrupted):
            # The signed draft was published before alignment and remains
            # usable when CHRONOS abstains or its resource guard declines.
            pass
    print(json.dumps({"event": "draft", "lines": len(data["plain"].splitlines()),
                      "omitted": data["omitted"], "chronos": chronos}), flush=True)


if __name__ == "__main__":
    main()
