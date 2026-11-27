"""Lecture bornée du LRC original pour l’affichage."""
from hashlib import sha256

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
