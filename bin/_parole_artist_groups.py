"""Regroupement réversible des dossiers d'édition sous un artiste choisi."""
from hashlib import sha256
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from uuid import uuid4

from _kara_paths import STATE
from _musique_oz import AUDIO, INBOX, component, shared_mutation
from _parole_index import MEDIA, PREPARATION, document, key

KIND = "artist_group"


def artist_dir(root, name):
    if not name or name in {INBOX, "YouTube"} or name.startswith(".") or component(name, "") != name:
        raise ValueError("Nom de dossier d’artiste invalide.")
    folder = root / name
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError(f"Dossier d’artiste indisponible · {name}")
    return folder


def directory_identity(path):
    info = path.lstat()
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"Dossier d’édition invalide · {path.name}")
    return [info.st_dev, info.st_ino]


def contents(folder):
    entries = []
    audio = 0

    def fail(error):
        raise error

    for base, dirs, files in os.walk(folder, onerror=fail):
        for name in sorted(dirs + files):
            path = Path(base) / name
            info = path.lstat()
            if path.is_symlink() or not (path.is_file() or path.is_dir()):
                raise ValueError(f"Entrée spéciale dans l’édition · {path.name}")
            if path.is_file():
                entries.append([str(path.relative_to(folder)), info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns])
                audio += path.suffix.lower() in AUDIO
    return sorted(entries), audio


def preview(root, target, names):
    destination = artist_dir(root, target)
    if not destination.is_dir():
        raise ValueError("L’artiste conservé doit exister dans la bibliothèque.")
    occupied = {path.name.casefold() for path in destination.iterdir()}
    moves = []
    for name in sorted(set(names) - {target}):
        source = artist_dir(root, name)
        for folder in sorted(source.iterdir()):
            if not folder.is_dir() or folder.is_symlink() or folder.name.startswith("."):
                raise ValueError(f"Entrée hors dossier d’édition · {name} / {folder.name}")
            original, audio = contents(folder)
            if not audio:
                raise ValueError(f"Dossier sans piste audio · {name} / {folder.name}")
            if folder.stat().st_dev != destination.stat().st_dev:
                raise ValueError("Le regroupement d’une édition exige le même système de fichiers.")
            relative = str(folder.relative_to(root))
            chosen = folder.name
            collision = chosen.casefold() in occupied
            if collision:
                # Separate editions get distinct directories even if their
                # files or physical recordings might be identical.
                identity = sha256(relative.encode()).hexdigest()[:12]
                stem = component(chosen, "Album")
                chosen = f"{stem} [édition {identity}]"
                counter = 2
                while chosen.casefold() in occupied:
                    chosen = f"{stem} [édition {identity}-{counter}]"
                    counter += 1
            occupied.add(chosen.casefold())
            moves.append(dict(source=relative, destination=str((destination / chosen).relative_to(root)),
                              directory=directory_identity(folder), files=len(original), audio=audio,
                              file_names=[entry[0] for entry in original],
                              fingerprint=sha256(document(original).encode()).hexdigest(), collision=collision))
    if not moves:
        raise ValueError("Aucune édition à déplacer.")
    return {"target": target, "moves": moves}


def save_manifest(path, value):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as temp:
        draft = Path(temp.name)
        try:
            temp.write(document(value))
            temp.flush()
            os.fsync(temp.fileno())
            os.replace(draft, path)
            sync(path.parent)
        finally:
            draft.unlink(missing_ok=True)


def sync(directory):
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def endpoints(root, row, undo):
    first, second = (Path(row[name]) for name in ("source", "destination"))
    for path in (first, second):
        if len(path.parts) != 2 or ".." in path.parts or path.is_absolute() or path.name.startswith("."):
            raise ValueError("Chemin d’édition invalide.")
        artist_dir(root, path.parts[0])
    if undo:
        first, second = second, first
    return root / first, root / second


def verify(root, plan, *, undo=False, recovering=False):
    for row in plan["moves"]:
        source, target = endpoints(root, row, undo)
        if not source.exists() and not source.is_symlink() and recovering and target.exists():
            if directory_identity(target) == row["directory"]:
                continue
        if directory_identity(source) != row["directory"]:
            raise ValueError(f"L’édition a changé · {source.name}")
        if target.exists() or target.is_symlink():
            raise ValueError(f"La destination est occupée · {target.name}")
        if not undo and not recovering:
            actual, _ = contents(source)
            if sha256(document(actual).encode()).hexdigest() != row["fingerprint"]:
                raise ValueError(f"Les fichiers ont changé depuis l’aperçu · {source.name}")


def persist_paths(index, plan, identity, undo, queue):
    moves = [endpoints(index.root, row, undo) for row in plan["moves"]]
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        for row, (source, target) in zip(plan["moves"], moves):
            prefix, replacement = str(source) + "/", str(target) + "/"
            indexed = db.execute("SELECT id,path,artist,album,title,info FROM songs WHERE substr(path,1,?)=?",
                                 (len(prefix), prefix)).fetchall()
            for song in indexed:
                owner = target.parent.name
                path = replacement + song["path"][len(prefix):]
                db.execute("UPDATE songs SET path=?,owner=?,previous=?,search=? WHERE id=?",
                           (path, owner, song["path"], key(" ".join((song["title"], song["artist"], owner, song["album"]))),
                            song["id"]))
            index.notify((target / name for name in row["file_names"]), MEDIA | PREPARATION, db=db)
            db.execute("UPDATE canonical_references SET path=?||substr(path,?) WHERE substr(path,1,?)=?",
                       (replacement, len(prefix) + 1, len(prefix), prefix))
            if not undo:
                rules = {(source.parent.name, source.name, target.parent.name, identity)}
                for song in indexed:
                    tags = json.loads(song["info"]).get("tags", {})
                    credit = tags.get("albumartist") or tags.get("artist")
                    if credit and song["album"]:
                        rules.add((credit, song["album"], target.parent.name, identity))
                db.executemany("INSERT OR IGNORE INTO associations VALUES(?,?,?,?)", sorted(rules))
        if undo:
            db.execute("DELETE FROM associations WHERE provenance=?", (identity,))
    if queue is not None:
        queue.relocate_album_paths(moves)


def publish(index, identity, plan, queue, state, *, undo=False, recovering=False):
    phase = "undoing" if undo else "moving"
    done = "undone" if undo else "done"
    manifest = Path(state) / "kara/music-tidy-backups" / identity / "manifest.json"
    with shared_mutation(index.root, wait=True):
        verify(index.root, plan, undo=undo, recovering=recovering)
        if queue is not None:
            # Keep the verification/claim boundary atomic with the import
            # queue. New requests may still be recorded during this pause.
            with queue.connect(write=True) as db:
                if db.execute("SELECT 1 FROM jobs WHERE state='running' LIMIT 1").fetchone():
                    raise ValueError("Attends le point d’arrêt des imports actifs.")
                paused = bool(db.execute("SELECT paused FROM settings WHERE id=1").fetchone()[0])
                if not recovering:
                    plan = {**plan, "resume_imports": not paused}
                db.execute("UPDATE settings SET paused=1 WHERE id=1")
        # The durable intent precedes the first rename. Recovery recognizes
        # an already moved directory by device and inode, never by its name.
        try:
            save_manifest(manifest, {**plan, "status": phase})
            index.review(KIND, identity, phase, plan)
        except (OSError, sqlite3.Error, ValueError):
            if queue is not None and not recovering and plan.get("resume_imports"):
                queue.configure(paused=False)
            raise
        for row in plan["moves"]:
            source, target = endpoints(index.root, row, undo)
            if not source.exists() and not source.is_symlink():
                continue
            target.parent.mkdir(mode=0o770, exist_ok=True)
            source.rename(target)
            sync(source.parent)
            sync(target.parent)
        persist_paths(index, plan, identity, undo, queue)
        for source, _ in (endpoints(index.root, row, undo) for row in plan["moves"]):
            if source.parent.name != plan["target"] and source.parent.exists() and not any(source.parent.iterdir()):
                source.parent.rmdir()
                sync(index.root)
        save_manifest(manifest, {**plan, "status": "undone" if undo else "complete"})
        index.review(KIND, identity, done, plan)
        if queue is not None and plan.get("resume_imports"):
            queue.configure(paused=False)
    return identity


def apply(index, plan, queue=None, *, state=STATE):
    return publish(index, "parole-artistes-" + uuid4().hex, plan, queue, state)


def last_group(index):
    with index.db() as db:
        row = db.execute("SELECT subject,value FROM reviews WHERE kind=? AND state='done' ORDER BY checked DESC LIMIT 1",
                         (KIND,)).fetchone()
    return (row["subject"], json.loads(row["value"])) if row else None


def undo(index, identity, queue=None, *, state=STATE):
    with index.db() as db:
        row = db.execute("SELECT state,value FROM reviews WHERE kind=? AND subject=?", (KIND, identity)).fetchone()
    if row is None or row["state"] != "done":
        raise ValueError("Aucun regroupement à annuler.")
    return publish(index, identity, json.loads(row["value"]), queue, state, undo=True)


def recover(index, queue=None, *, state=STATE):
    with index.db() as db:
        rows = db.execute("""SELECT subject,state,value FROM reviews WHERE kind=? AND state IN ('moving','undoing')
                            ORDER BY checked""", (KIND,)).fetchall()
    for row in rows:
        publish(index, row["subject"], json.loads(row["value"]), queue, state,
                undo=row["state"] == "undoing", recovering=True)
    return len(rows)


def main():
    import argparse
    from _kara_import_queue import ImportQueue
    from _musique_oz import SHARED_LIBRARY
    from _parole_index import Index

    parser = argparse.ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--cible")
    operation.add_argument("--annuler", metavar="REÇU")
    operation.add_argument("--reprendre", action="store_true")
    parser.add_argument("--appliquer", action="store_true")
    parser.add_argument("artistes", nargs="*")
    args = parser.parse_args()
    index = Index(SHARED_LIBRARY)
    queue = ImportQueue()
    try:
        if args.reprendre:
            result = {"recovered": recover(index, queue)}
        elif args.annuler:
            with index.db() as db:
                row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?", (KIND, args.annuler)).fetchone()
            if row is None:
                raise ValueError("Reçu inconnu.")
            undo(index, args.annuler, queue)
            result = {"undo": args.annuler}
        else:
            plan = preview(index.root, args.cible, args.artistes)
            if args.appliquer:
                identity = apply(index, plan, queue)
                result = {"receipt": identity}
            else:
                result = plan
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, str(error) + "\n")


if __name__ == "__main__":
    main()
