"""Raccord durable entre une publication audio et la maintenance Parole."""
import json
from pathlib import Path

from _musique_oz import grouped_path
from _parole_index import encoded_key

LYRICS = "import_lyrics"
COVER = "import_cover"
KINDS = (LYRICS, COVER)
BATCH_SIZE = 16


def schedule(index, tracks):
    paths = sorted(set(tracks))
    albums = sorted({str(Path(path).parent) for path in paths})
    subjects = {kind: [] for kind in KINDS}
    if not paths:
        return subjects
    with index.db() as db:
        # Commit delivery before the import outcome. A crash in between can
        # replay the publication receipt and enqueue the same tasks safely.
        index.notify(paths, db=db)
        for kind, values, size in ((LYRICS, paths, BATCH_SIZE), (COVER, albums, 1)):
            for offset in range(0, len(values), size):
                batch = values[offset:offset + size]
                subject = encoded_key(kind, batch)
                index.enqueue(kind, subject, subject, 15, {"paths": batch}, db=db)
                subjects[kind].append(subject)
    return subjects


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


def pending(index, now):
    with index.db(wait=.15) as db:
        return bool(db.execute("""SELECT 1 FROM tasks WHERE kind IN (?,?)
                               AND state IN ('queued','temporary','suspended') AND due<=? LIMIT 1""",
                               (*KINDS, now)).fetchone())
