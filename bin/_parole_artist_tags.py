"""Verified publication of album-artist edits and repair of approved editions."""
from hashlib import file_digest
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
from tempfile import TemporaryDirectory
from time import time
from uuid import uuid4

from _kara_audio_identity import pcm_digest
from _kara_import_resources import run_checked
from _kara_paths import STATE
from _musique_oz import SHARED_LIBRARY, album_groups, audio_file, component, manual_album_artist, probe, share_file, shared_mutation
from _parole_index import Index, revision
from _parole_taglib import helper as taglib_helper, read as taglib_read

WRITABLE = {".opus", ".ogg", ".oga", ".flac", ".m4a", ".mp3"}
FFMPEG = ("ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe")


def remux(source, draft, tags, updates):
    metadata = []
    for field, value in updates.items():
        assignment = f"{'album_artist' if field == 'albumartist' else field}={value}"
        metadata.extend(("-metadata", assignment, "-metadata:s:a:0", assignment))
    run_checked((*FFMPEG, "-y", "-i", str(source), "-map", "0", "-c", "copy", *metadata, str(draft)),
                capture_output=True, check=True, timeout=60)
    actual, _ = probe(draft, include_duration=True, include_codec=True)
    for key, value in (tags | updates).items():
        if key not in ("encoder", "duration_seconds", "codec") and actual.get(key) != value:
            raise ValueError(f"Le tag {key} changerait lors de la réécriture.")
    return actual


def stream_hash(path):
    # Copy every stream, including embedded cover art, into the verifier.
    return run_checked((*FFMPEG, "-xerror", "-i", str(path), "-map", "0", "-c", "copy",
                        "-f", "streamhash", "-hash", "sha256", "pipe:1"),
                       capture_output=True, check=True, timeout=90).stdout


def verify_audio(source, draft, *, preserve_all=False):
    original = pcm_digest(source)
    if pcm_digest(draft) != original:
        raise ValueError("La réécriture changerait l’identité PCM.")
    if preserve_all:
        original_streams = stream_hash(source)
        if not original_streams or stream_hash(draft) != original_streams:
            raise ValueError("La réécriture changerait un flux audio ou image.")
    return original


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def checksum(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def save_original(source, draft, token, pcm, *, state=STATE):
    # The receipt and private byte-for-byte copy reach stable storage before
    # the live file is replaced. They also say which replacement was intended
    # if the process stops between the backup and the atomic rename.
    receipt = Path(state) / "kara/albumartist-backups" / uuid4().hex
    receipt.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt.mkdir(mode=0o700)
    original = receipt / ("original" + source.suffix.lower())
    with source.open("rb") as opened, original.open("xb") as output:
        original.chmod(0o600)
        shutil.copyfileobj(opened, output)
        output.flush()
        os.fsync(output.fileno())
    if revision(source.lstat()) != token or checksum(source) != checksum(original):
        raise ValueError("La piste a changé pendant la sauvegarde.")
    manifest = receipt / "manifest.json"
    with manifest.open("x", encoding="utf-8") as output:
        manifest.chmod(0o600)
        json.dump({"path": str(source), "previous": token, "pcm": pcm,
                   "original_sha256": checksum(original), "updated_sha256": checksum(draft)}, output)
        output.flush()
        os.fsync(output.fileno())
    sync_directory(receipt)
    sync_directory(receipt.parent)
    return receipt


def has_preparation(path):
    from _kara_prepare import PreparedCache
    from _kara_stems import stem_directory

    return ((PreparedCache().folder(path) / "prepared.json").exists()
            or (stem_directory(path) / "meta.json").exists())


def repair(index, path, *, apply=False, state=STATE, expected=None):
    if not path.is_relative_to(index.root) or path.resolve() != path or not audio_file(path):
        return "hors bibliothèque"
    parts = path.relative_to(index.root).parts
    if len(parts) < 3:
        return "hors classement"
    owner = parts[0]
    token = revision(path.lstat())
    if expected is not None and token != expected:
        raise ValueError("Piste modifiée depuis le relevé.")
    tags, _ = probe(path, include_duration=True, include_codec=True)
    if tags.get("albumartist") == owner or manual_album_artist(album_groups(index.path), tags) != owner:
        return "sans correction autorisée"
    if path.suffix.lower() not in WRITABLE:
        return "format à examiner"
    # A bound cache remains playable until a receipt-specific rebind is
    # available. A metadata repair never invalidates a published preparation.
    if has_preparation(path):
        return "préparation à réassocier"
    if not apply:
        return "correction autorisée"
    with shared_mutation(index.root):
        if (revision(path.lstat()) != token or path.resolve() != path
                or manual_album_artist(album_groups(index.path), tags) != owner):
            raise ValueError("La piste ou son regroupement a changé.")
        with TemporaryDirectory(prefix=".albumartist-", dir=path.parent) as workspace:
            draft = Path(workspace) / path.name
            verified = remux(path, draft, tags, {"albumartist": owner})
            pcm = verify_audio(path, draft, preserve_all=True)
            save_original(path, draft, token, pcm, state=state)
            shutil.copymode(path, draft)
            share_file(draft, path)
            with draft.open("rb") as output:
                os.fsync(output.fileno())
            if revision(path.lstat()) != token or has_preparation(path):
                raise ValueError("La piste a changé avant la publication.")
            os.replace(draft, path)
            sync_directory(path.parent)
            # A filesystem event can recover the published version if the
            # process ends before this verified index notification commits.
            index.notify([path], proofs={str(path): {"token": revision(path.lstat()), "tags": verified,
                                                     "same_streams_as": token, "published": time()}})
    return "corrigée"


def paths_for(index, owner):
    if not owner or owner.startswith(".") or component(owner, "") != owner or owner in {"A_trier", "YouTube"}:
        raise ValueError("Nom d’artiste de classement invalide.")
    target = index.root / owner
    prefix = str(target) + "/"
    with index.db() as db:
        return [Path(path) for path, in db.execute(
            "SELECT path FROM songs WHERE path>=? AND path<? ORDER BY path", (prefix, prefix[:-1] + "0"))]


def main():
    import argparse
    from collections import Counter

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artiste", required=True)
    parser.add_argument("--appliquer", action="store_true")
    args = parser.parse_args()
    index = Index(SHARED_LIBRARY)
    reader = taglib_helper()
    rules = album_groups(index.path)
    counts = Counter()
    for path in paths_for(index, args.artiste):
        try:
            token = revision(path.lstat())
            fast = taglib_read(path, reader)
            tags = fast.tags if fast is not None else probe(path)[0]
            # The fast audit is a candidate filter. The writer repeats the
            # underlying test with FFprobe and the current rule at admission.
            if tags.get("albumartist") == args.artiste:
                counts["déjà cohérente"] += 1
            elif manual_album_artist(rules, tags) != args.artiste:
                counts["sans correction autorisée"] += 1
            else:
                counts[repair(index, path, apply=args.appliquer, expected=token)] += 1
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError):
            counts["à examiner"] += 1
    print(json.dumps(counts, ensure_ascii=False, sort_keys=True))
    if counts["à examiner"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
