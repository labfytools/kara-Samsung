"""Corpus linguistique local pour qualifier un contexte Turbo."""

from collections import Counter
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import re
import sqlite3
from unicodedata import normalize

from _kara_words import FORM, IGNORED_HINTS, detect_language, words
from _parole_index import key, sidecar
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc

MAX_FORMS = 512
MARKERS = re.compile(r"\[[^\]\n]*\]")


def lexical_forms(text):
    # Preserve contractions, accented spelling and capitalization. Folding
    # for ranking or acoustic comparison never changes the stored form.
    counts = Counter()
    for line in text.splitlines():
        forms = [form for form in FORM.findall(MARKERS.sub(" ", normalize("NFC", line))) if len(form) <= 32]
        counts.update(forms)
        counts.update(" ".join(pair) for pair in zip(forms, forms[1:]))
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0].casefold(), item[0]))[:MAX_FORMS])


def origin(row, info, root):
    # Generated or uncertain material is excluded even when its timing is
    # structurally valid. This keeps model output out of its own corpus.
    providers = [str(info.get(name, "")).casefold() for name in ("lyrics_provider", "lrc_provider")]
    if any("turbo" in name or "whisper" in name or "transcription" in name for name in providers):
        return "", ""
    if row["lrc"] == "généré":
        return "", ""
    if row["lyrics"] == "Strawberry" and isinstance(info.get("plain"), str) and info["plain"].strip():
        if len(info["plain"].encode()) <= MAX_LRC_BYTES:
            return "Strawberry ou tags intégrés", info["plain"]
    if row["lrc"] in {"local", "externe"}:
        path = Path(row["path"])
        try:
            usable = path.is_relative_to(root) and path.resolve() == path
        except (OSError, RuntimeError):
            usable = False
        if usable:
            _, text = sidecar(path, ".lrc")
            lines = [(stamp, line) for stamp, line in parse_lrc(text) if line.strip()]
            if lines and row["duration"] > 0 and lines[-1][0] <= row["duration"] + 3:
                return "LRC local validé structurellement", "\n".join(line for _, line in lines)
    return "", ""


def identities(row, info):
    tags = info.get("tags", {})
    recording = tags.get("recordingid") or tags.get("musicbrainztrackid")
    return {("id", row["id"]), ("credit", (key(row["owner"]), key(row["title"])))} | {
        (kind, value) for kind, value in (("pcm", row["audio"]), ("recording", recording)) if value
    }


def records(index_path, root, *, exclude=(), genres=None):
    """Yield trusted records from an SQLite snapshot opened read-only."""
    index_path, root = index_path.resolve(strict=True), root.resolve(strict=True)
    held_out = set(exclude)
    with closing(sqlite3.connect(index_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        blocked = set()
        blocked_text = set()
        for identity in sorted(held_out):
            row = db.execute("SELECT * FROM songs WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("Morceau d’évaluation absent de l’index")
            info = json.loads(row["info"])
            blocked.update(identities(row, info))
            _, text = origin(row, info, root)
            if text:
                blocked_text.add(text_digest(text))
        for row in db.execute("SELECT * FROM songs ORDER BY id"):
            info = json.loads(row["info"])
            if identities(row, info) & blocked:
                continue
            source, text = origin(row, info, root)
            if not text or text_digest(text) in blocked_text:
                continue
            lines = [line for line in text.splitlines() if line.strip()]
            forms = lexical_forms(text)
            if forms:
                tag = ((genres or {}).get(row["path"]) or info.get("tags", {}).get("genre") or "").strip()
                yield dict(song=row["id"], source=source, confidence="local", digest=text_digest(text),
                           language=detect_language(words(text)), genre=tag, artist=row["owner"],
                           album=row["edition"], forms=forms, lines=len(lines))


def text_digest(text):
    # Case, punctuation, timestamps and line endings do not distinguish
    # training copies of the same lyrics from held-out reference text.
    return sha256(" ".join(words(text)).encode()).hexdigest()


def ranked_forms(corpus, *, language="", genre="", artist="", album="", single=False):
    scores = Counter()
    digests = set()
    genre_key, artist_key = key(genre), key(artist)
    for record in corpus:
        if album and record["album"] != album:
            continue
        if artist_key and key(record["artist"]) != artist_key:
            continue
        if genre_key and key(record["genre"]) != genre_key:
            continue
        if language and record["language"] != language:
            continue
        if record["digest"] in digests:
            continue
        digests.add(record["digest"])
        for form, count in record["forms"].items():
            if single and " " in form:
                continue
            if any(len(token) >= 3 and token not in IGNORED_HINTS for token in form.casefold().split()):
                scores[form] += min(count, 3)
        # A bounded candidate set keeps the experiment practical on a
        # large library. Pruning is deterministic in corpus row order.
        if len(scores) > 65_536:
            scores = Counter(dict(sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:32_768]))
    return sorted(scores, key=lambda form: (-scores[form], " " in form, form.casefold(), form))


def select_prompt(corpus, **profile):
    """Select a bounded context using only the supplied training records."""
    ranked = ranked_forms(corpus, **profile)
    # Leave room under the native tokenizer bound, including multilingual
    # forms whose characters can occupy more than one UTF-8 byte.
    chosen = []
    for form in ranked[:24]:
        if len(form.encode()) <= 24 and len(", ".join((*chosen, form)).encode()) <= 144:
            chosen.append(form)
    return ", ".join(chosen)
