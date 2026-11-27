"""Raccord durable entre une publication audio et la maintenance Parole."""
import json
from pathlib import Path
from time import time

from _musique_oz import audio_files, grouped_path
from _parole_index import document, encoded_key, index_path, path_rows, revision

LYRICS = "import_lyrics"
COVER = "import_cover"
KINDS = (LYRICS, COVER)
BATCH_SIZE = 16


def queue_lyrics(index, db, paths, *, manual=None, versions=None):
    # Sharing is based on the files inside a batch, even when an import
    # completes with a larger selection or an artist request overlaps it.
    active = {}
    for row in db.execute("""SELECT t.subject,t.payload,j.value AS path
        FROM tasks t,json_each(t.payload,'$.paths') j WHERE t.kind=?
        AND t.state IN ('queued','running','temporary','suspended')
        AND j.value IN (SELECT value FROM json_each(?)) ORDER BY t.subject""", (LYRICS, document(paths))):
        active.setdefault(row["path"], (row["subject"], row["payload"]))
    for subject, payload in sorted(set(active.values())):
        data = json.loads(payload)
        if manual is None and data.get("manual") and not data.get("automatic"):
            data["automatic"] = True
            db.execute("UPDATE tasks SET payload=? WHERE kind=? AND subject=?", (document(data), LYRICS, subject))
        elif manual is not None:
            if not data.get("manual"):
                data["automatic"] = True
            data["manual"] = manual
            db.execute("""UPDATE tasks SET priority=min(priority,12),due=min(due,?),payload=?
                          WHERE kind=? AND subject=?""", (time(), document(data), LYRICS, subject))
    remaining = [path for path in paths if path not in active]
    result = {path: choice[0] for path, choice in active.items()}
    for offset in range(0, len(remaining), BATCH_SIZE):
        batch = remaining[offset:offset + BATCH_SIZE]
        subject = encoded_key(LYRICS, batch)
        payload = {"paths": batch}
        if manual is not None:
            payload["manual"] = manual
        previous = db.execute("SELECT state,payload FROM tasks WHERE kind=? AND subject=?", (LYRICS, subject)).fetchone()
        # An automatic receipt is idempotent. An explicit search may retry
        # a settled batch, and an import may take over a cancelled manual one.
        promote = previous and previous["state"] == "excluded" and json.loads(previous["payload"]).get("manual")
        generation = (encoded_key(subject, [versions.get(path) for path in batch])
                      if versions is not None else subject)
        index.enqueue(LYRICS, subject, generation, 12 if manual else 15, payload,
                      force=bool(manual or promote), db=db)
        result.update(dict.fromkeys(batch, subject))
    return result


def schedule(index, tracks):
    paths = sorted({str(path) for path in tracks})
    albums = sorted({str(Path(path).parent) for path in paths})
    subjects = {kind: [] for kind in KINDS}
    if not paths:
        return subjects
    versions = {}
    for value in paths:
        try:
            versions[value] = revision(Path(value).lstat())
        except FileNotFoundError:
            continue
    with index.db(wait=.15) as db:
        db.execute("BEGIN IMMEDIATE")
        # An existing index event owns delivery and its retry cadence.
        missing = []
        for offset in range(0, len(paths), 128):
            batch = paths[offset:offset + 128]
            known = path_rows(db, batch)
            queued = {row[0] for row in db.execute(
                "SELECT path FROM index_events WHERE path IN (SELECT value FROM json_each(?))", (document(batch),))}
            missing.extend(path for path in batch if path in versions and path not in queued
                           and (path not in known or known[path]["token"] != versions[path]))
        index.notify(missing, db=db)
        subjects[LYRICS] = sorted(set(queue_lyrics(index, db, paths, versions=versions).values()))
        for album in albums:
            subject = encoded_key(COVER, [album])
            index.enqueue(COVER, subject, subject, 15, {"paths": [album]}, db=db)
            subjects[COVER].append(subject)
    return subjects


def queue_artist(index, artist, request):
    if Path(artist).name != artist or artist in ("", ".", ".."):
        raise ValueError("Artiste invalide.")
    with index.db() as db:
        indexed = {Path(row["path"]) for row in db.execute("SELECT path FROM songs WHERE owner=?", (artist,))}
    folder = index.root / artist
    files = indexed | (set(audio_files(folder)) if folder.is_dir() and not folder.is_symlink() else set())
    paths = [str(path) for path in sorted(files)
             if index_path(index.root, path) and path.resolve() == path and path.is_file()]
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        return queue_lyrics(index, db, paths, manual=request)


def cancel_artist(index, request):
    with index.db() as db:
        # An import that joined the request continues to own the batch.
        return [row[0] for row in db.execute("""UPDATE tasks SET state='excluded',
            detail='recherche annulée',updated=? WHERE kind=?
            AND json_extract(payload,'$.manual')=? AND coalesce(json_extract(payload,'$.automatic'),0)=0
            AND state IN ('queued','running','temporary','suspended') RETURNING subject""", (time(), LYRICS, request))]


def artist_results(index, paths, started):
    subjects = sorted(set(paths.values()))
    with index.db(wait=.2) as db:
        rows = {row["subject"]: row for row in db.execute("""SELECT t.subject,t.state,t.updated,t.detail,
             r.checked,r.value FROM tasks t LEFT JOIN reviews r ON r.kind='import_lyrics_metrics' AND r.subject=t.subject
             WHERE t.kind=? AND t.subject IN (SELECT value FROM json_each(?))""", (LYRICS, document(subjects)))}
    answers = {}
    for path, subject in paths.items():
        row = rows.get(subject)
        if row is None:
            continue
        if row["checked"] is not None and row["checked"] >= started:
            if result := json.loads(row["value"]).get("tracks", {}).get(path):
                answers[path] = result
                continue
        if row["state"] in ("done", "review", "excluded") and row["updated"] >= started:
            answers[path] = {"lyrics": "unknown", "lrc": "unknown", "errors": [row["detail"] or row["state"]]}
    return answers


def paths_for(index, kind, subject):
    with index.db() as db:
        row = db.execute("SELECT payload FROM tasks WHERE kind=? AND subject=? AND state='running'",
                         (kind, subject)).fetchone()
    if row is None:
        return []
    values = json.loads(row["payload"])["paths"]
    if len(values) > (BATCH_SIZE if kind == LYRICS else 1):
        raise ValueError("Lot d’enrichissement trop volumineux.")
    return [path for value in values
            if (path := grouped_path(index.root, Path(value), database=index.path)).is_relative_to(index.root)
            and path != index.root and path.resolve() == path
            and (path.is_file() if kind == LYRICS else path.is_dir())]
