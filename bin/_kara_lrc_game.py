"""Read the library LRC as a display and, independently, scoring input."""
from hashlib import sha256

from _kara_words import LyricVocabulary, word_boundaries
from _paroles_lrc import MAX_LRC_BYTES, playback_lrc


def read_source(song):
    try:
        with song.with_suffix(".lrc").open("rb") as source:
            raw = source.read(MAX_LRC_BYTES + 1)
        if len(raw) > MAX_LRC_BYTES:
            return None
        text = raw.decode("utf-8-sig")
        rows = playback_lrc(text)
    except (OSError, ValueError):
        return None
    return (text, sha256(raw).hexdigest(), rows) if any(row.strip() for _, row, _ in rows) else None


def score_source(source, duration):
    if source is None:
        return None
    _, digest, rows = source
    vocabulary = LyricVocabulary(text for _, text, _ in rows)
    lines = []
    for index, (start, text, marks) in enumerate(rows):
        tokens = word_boundaries(text)
        # The lexical alignment has quadratic cost per line. Keep unusual
        # provider lines visible while declining an unbounded scoring window.
        if len(tokens) > 256:
            return None
        word_starts = {first: point for point, first, _ in reversed(marks)}
        words = [{"id": number, "text": text[first:last], "normalized": token,
                  "start": word_starts.get(first)}
                 for number, (token, first, last) in enumerate(tokens)]
        following = rows[index + 1][0] if index + 1 < len(rows) else duration
        stop = min(start + 8, following) if following > start else start + 2.5
        lines.append({"text": text, "display_start": start, "end": stop, "words": words})
    if not any(line["words"] for line in lines):
        return None
    # This ephemeral identity denotes the original bytes. No prepared
    # manifest or inferred word time enters the live score.
    return {"lrc_sha256": digest, "song_id": digest, "prepared_at": "lrc-original",
            "source_order": "timestamps", "language": vocabulary.language, "duration": duration,
            "method": "lrc-original", "lines": lines}


def score_clock(reference):
    if reference is None:
        return (), (), ()
    lines = reference["lines"]
    return (tuple(line["display_start"] for line in lines),
            tuple(tuple(word["start"] for word in line["words"]) for line in lines),
            tuple(line["end"] for line in lines))
