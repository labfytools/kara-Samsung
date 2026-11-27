"""Références vérifiées réutilisables sur le même signal PCM."""
from dataclasses import dataclass
import json
from pathlib import Path
import re

from _kara_files import file_token
from _parole_index import document, sidecar
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc

SOURCE = "Bibliothèque · PCM identique"
PCM = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class LocalReference:
    path: Path
    token: str
    suffix: str
    side_token: str
    text: str

    def current(self):
        try:
            return (self.path.resolve(strict=True) == self.path
                    and document(file_token(self.path)) == self.token
                    and (not self.suffix or sidecar(self.path, self.suffix)[0] == self.side_token))
        except (OSError, ValueError):
            return False


def trusted_sidecar(row, info, suffix, token, reviews):
    if token != json.loads(row["side"])[0 if suffix == ".lrc" else 1]:
        return False
    receipt = reviews.get(row["id"] + suffix)
    if receipt and receipt.get("audio") == row["token"] and receipt.get("sidecar") == token:
        return True
    return suffix == ".lrc" and (bool(info.get("report", {}).get("reference_validated"))
                                  or row["lrc"] == "externe" and bool(info.get("lrc_provider")))


def candidate(root, row, reviews, synced):
    path = Path(row["path"])
    if not path.is_relative_to(root) or path.resolve(strict=True) != path:
        return None
    info = json.loads(row["info"])
    if not synced:
        token, text = sidecar(path, ".txt")
        if token and text.strip() and trusted_sidecar(row, info, ".txt", token, reviews):
            return LocalReference(path, row["token"], ".txt", token, text)
        text = info.get("plain", "")
        if info.get("lyrics_provider") and text.strip() and len(text.encode()) <= MAX_LRC_BYTES:
            return LocalReference(path, row["token"], "", "", text)
    token, text = sidecar(path, ".lrc", preserve_bom=synced)
    if not token or not text or not trusted_sidecar(row, info, ".lrc", token, reviews):
        return None
    rows = parse_lrc(text)
    if not rows or rows[-1][0] > row["duration"] + 3 or not any(phrase.strip() for _, phrase in rows):
        return None
    return LocalReference(path, row["token"], ".lrc", token,
                          text if synced else "\n".join(phrase for _, phrase in rows if phrase.strip()))


def local_reference(index, song, *, synced):
    pcm = song["audio"]
    if not PCM.fullmatch(pcm):
        return None
    # Only exact decoded PCM is strong enough to copy existing timing.
    # A catalogue identifier or sharing a commercial edition is a lead.
    with index.db(wait=.2) as db:
        sources = db.execute("""SELECT id,path,token,side,info,lrc,duration FROM songs
                                WHERE audio=? AND id<>? ORDER BY id LIMIT 9""", (pcm, song["id"])).fetchall()
        if not sources or len(sources) == 9:
            return None
        subjects = [row["id"] + suffix for row in sources for suffix in (".txt", ".lrc")]
        placeholders = ",".join("?" for _ in subjects)
        reviews = dict(db.execute(f"""SELECT subject,value FROM reviews
                      WHERE kind='transcription_validation' AND state='validated'
                      AND subject IN ({placeholders})""", subjects))
    receipts = {subject: json.loads(value) for subject, value in reviews.items()}
    matches = {}
    for row in sources:
        try:
            ref = candidate(index.root, row, receipts, synced)
            if ref and ref.current():
                matches.setdefault(ref.text.strip(), ref)
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            continue
    # Two different verified references deserve review, even on identical PCM.
    return next(iter(matches.values())) if len(matches) == 1 else None
