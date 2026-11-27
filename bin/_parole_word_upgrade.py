"""Révision des LRC à la ligne avec les recherches persistantes de Parole."""
from collections import Counter
from pathlib import Path
import json
import math

from _kara_files import file_token
from _kara_lrc import read_source
from _musique_oz import shared_mutation
from _parole_index import document, encoded_key, index_path, sidecar_token
from _paroles_lrc import MAX_LRC_BYTES, has_word_timing

KIND = "lrc_words"


def queue_library(index, *, preview=False):
    counts = Counter()
    after = ""
    while True:
        with index.db() as db:
            songs = db.execute("""SELECT s.id,s.path,s.token,s.lrc,
                EXISTS(SELECT 1 FROM exclusions e WHERE e.kind='artist' AND e.subject=s.owner) AS excluded
                FROM songs s WHERE s.id>? ORDER BY s.id LIMIT 128""", (after,)).fetchall()
        if not songs:
            return dict(counts)
        after = songs[-1]["id"]
        counts["total"] += len(songs)
        pending = []
        for song in songs:
            path = Path(song["path"])
            if not index_path(index.root, path):
                counts["invalid"] += 1
                continue
            base = sidecar_token(path.with_suffix(".lrc"))
            if not base:
                counts["missing"] += 1
                continue
            source = read_source(path) if base != "invalid" else None
            if source is None or song["lrc"] == "à vérifier":
                counts["invalid"] += 1
            elif any(marks for _, _, marks in source[2]):
                counts["words"] += 1
            else:
                counts["lines"] += 1
                if not song["excluded"]:
                    pending.append((song["id"], encoded_key(KIND, song["token"], base)))
        counts["eligible"] += len(pending)
        # Commit in small batches so the live import and search owners can
        # still reserve work. The same audio and original reuse their task.
        if pending and not preview:
            with index.db() as db:
                db.execute("BEGIN IMMEDIATE")
                for identity, revision in pending:
                    counts["queued"] += index.enqueue(KIND, identity, revision, 45, db=db)


def verify_track(index, identity, engine):
    song = index.song(identity)
    if song is None:
        return {"state": "excluded"}
    path = Path(song["path"])
    if not index_path(index.root, path) or path.resolve() != path:
        return {"state": "excluded"}
    base = sidecar_token(path.with_suffix(".lrc"))
    token = document(file_token(path))
    expected = encoded_key(KIND, token, base)
    with index.db() as db:
        task = db.execute("""SELECT revision FROM tasks WHERE kind=? AND subject=? AND state='running'""",
                          (KIND, identity)).fetchone()
        excluded = db.execute("SELECT 1 FROM exclusions WHERE kind='artist' AND subject=?",
                              (song["owner"],)).fetchone()
    if excluded or not task or task[0] != expected or song["token"] != token:
        return {"state": "excluded"}
    original = read_source(path) if base not in ("", "invalid") else None
    if not original or has_word_timing(original[0]):
        return {"state": "excluded"}
    if variant(index, path, base) is not None:
        return {"state": "present"}
    tags, _ = engine.probe(path, include_duration=True)
    duration = tags.get("duration_seconds", 0.)
    title, artist = tags.get("title"), tags.get("artist")
    if not title or not artist or not math.isfinite(duration) or not 1 <= duration <= 3600:
        return {"state": "invalid"}
    client = engine.LyricsSources()
    try:
        raw, provider = client.find(title, artist, tags.get("album"), round(duration), tags.get("albumartist"),
                                    synced=True, registry=index, strict=True, context=engine.contexts(tags),
                                    isrc=tags.get("isrc"), original_lrc=True, word_reference=original[0])
    except engine.ServiceUnavailable as error:
        return {"state": error.reason}
    except engine.AmbiguousLyrics:
        return {"state": "ambiguous"}
    finally:
        client.close()
    if not raw or not has_word_timing(raw):
        return {"state": "not_found"}
    with shared_mutation(index.root):
        if document(file_token(path)) != token or sidecar_token(path.with_suffix(".lrc")) != base:
            return {"state": "excluded"}
        # The original stays on disk. A single receipt owns the alternative
        # and binds its exact UTF-8 bytes to both current file versions.
        index.review(KIND, encoded_key(str(path)), "verified",
                     {"audio": token, "base": base, "text": raw, "source": provider})
    return {"state": "found", "source": provider}


def variant(index, path, base):
    if base in ("", "invalid"):
        return None
    with index.db(wait=.05) as db:
        row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=? AND state='verified'",
                         (KIND, encoded_key(str(path)))).fetchone()
    if row:
        value = json.loads(row[0])
        raw = value["text"]
        if (isinstance(raw, str) and value["base"] == base and value["audio"] == document(file_token(path))
                and len(raw.encode()) <= MAX_LRC_BYTES):
            return raw
    return None
