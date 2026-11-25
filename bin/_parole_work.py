"""Exécute une opération bornée avec les moteurs de la bibliothèque."""
import json
from collections import defaultdict
from importlib.machinery import SourceFileLoader
import math
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

from _kara_audio_identity import pcm_digest
from _kara_paths import BIN, music_library
from _kara_prepare import file_token
from _musique_oz import shared_mutation
from _parole_catalogue import check_album, check_discography, check_recording, MBID
from _parole_index import Index, document, key, sidecar
from _parole_sources import ProviderUnavailable, LyricsEndpoint, validate_endpoint, record, fetch
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc


def lyrics_engine():
    loader = SourceFileLoader("_parole_lyrics_engine", str(BIN / "paroles-musique"))
    import importlib.util

    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def lookup_song(index, identity, synced, *, read_only=False):
    os.environ["KARA_IMPORT_PROCESSING"] = "1"
    engine = lyrics_engine()
    song = index.song(identity)
    if not song:
        return {"state": "excluded"}
    path = Path(song["path"])
    if path.resolve(strict=True) != path or not path.is_relative_to(index.root):
        return {"state": "excluded"}
    token = file_token(path)
    if document(token) != song["token"]:
        return {"state": "rescan"}
    version = tuple(token)
    tags, _ = engine.probe(path)
    stored = json.loads(song["info"])
    text = next((tags[k] for k in engine.LYRICS_TAGS if tags.get(k)), "") or stored.get("plain", "")
    local_text = sidecar(path, ".txt")[1] or text
    if read_only and not synced and local_text.strip():
        if len(local_text.encode()) <= MAX_LRC_BYTES:
            return {"state": "found", "source": "bibliothèque", "lyrics": local_text}
        return {"state": "invalid"}
    if not synced and (local_text or song["lrc"] in ("local", "externe", "généré")):
        return {"state": "present"}
    if synced and not read_only and (path.with_suffix(".lrc").exists() or path.with_suffix(".lrc").is_symlink()):
        return {"state": "present" if song["lrc"] != "à vérifier" else "invalid"}
    title, artist = tags.get("title"), tags.get("artist")
    duration = engine.duration_seconds(path)
    if not title or not artist or not math.isfinite(duration) or duration <= 0:
        return {"state": "invalid"}
    if (synced and (rows := parse_lrc(text)) and rows[-1][0] <= duration + 3
            and any(content for _, content in rows)):
        lyrics, provider = text, "Strawberry"
    else:
        try:
            lyrics, provider = engine.LyricsSources().find(
                title, artist, tags.get("album"), duration, tags.get("albumartist"),
                synced=synced, local_text=local_text, registry=index, strict=True)
        except engine.AmbiguousLyrics:
            return {"state": "ambiguous"}
        except engine.ServiceUnavailable as error:
            return {"state": error.reason}
    if lyrics is None:
        return {"state": "not_found"}
    if not lyrics:
        return {"state": "instrumental", "source": provider}
    if read_only:
        if len(lyrics.encode()) > MAX_LRC_BYTES:
            return {"state": "invalid"}
        return {"state": "found", "source": provider, "lyrics": lyrics}
    root = index.root
    if synced:
        with shared_mutation(root):
            if file_token(path) != token or path.resolve() != path:
                return {"state": "rescan"}
            if not engine.save_lrc(path.with_suffix(".lrc"), lyrics):
                return {"state": "present"}
    elif path.suffix.lower() in engine.STREAM_TAGS | engine.FILE_TAGS:
        # The existing engine compares tags and the original audio/image
        # streams before replacing the container, with stream copy only.
        audio = pcm_digest(path)
        engine.save_lyrics(path, version, tags, lyrics, root)
        return {"state": "found", "source": provider, "token": file_token(path), "audio": audio}
    else:
        return {"state": "invalid"}
    return {"state": "found", "source": provider}


def edition(index, identity):
    with index.db() as db:
        local = db.execute("SELECT * FROM songs WHERE edition=?", (identity,)).fetchall()
        if not local:
            return "excluded", {}
        associations = [r[0] for r in db.execute("SELECT old_artist FROM associations WHERE album=? AND new_artist=?",
                                                (local[0]["album"], local[0]["owner"]))]
    return check_album(local, associations)


def discography(index, owner, settings):
    with index.db() as db:
        rows = db.execute("SELECT DISTINCT artist_id,album FROM songs WHERE owner=?", (owner,)).fetchall()
        editions = db.execute("""SELECT value FROM reviews WHERE kind='album'
                         AND subject IN (SELECT DISTINCT edition FROM songs WHERE owner=?)
                         AND state IN ('complete','incomplete')""", (owner,))
        known = [json.loads(r[0]) for r in editions]
        excluded = {r[0] for r in db.execute("SELECT subject FROM exclusions WHERE kind='release'")}
    artist_ids = {r["artist_id"] for r in rows if MBID.fullmatch(r["artist_id"])}
    if not artist_ids:
        # An album group without exact edition provenance cannot establish
        # artist identity. A title match by itself leaves homonyms unresolved.
        proofs = defaultdict(set)
        for row in known:
            for artist_id, artist in row.get("artists", []):
                if artist_id and key(artist) == key(owner):
                    if row.get("verified"):
                        artist_ids.add(artist_id)
                    elif row.get("verified_tracks", 0) >= 3 and row.get("release_group"):
                        proofs[artist_id].add(row["release_group"])
        artist_ids.update(artist_id for artist_id, groups in proofs.items() if len(groups) >= 2)
    identity = next(iter(artist_ids)) if len(artist_ids) == 1 else ""
    state, found = check_discography(identity, {r.get("release_group") for r in known},
                                     {key(r["album"]) for r in rows}, settings["releases"])
    if "missing" in found:
        found["missing"] = [r for r in found["missing"] if r["id"] not in excluded]
    found["artist_id"] = identity
    return state, found


def cover(index, subject):
    with index.db() as db:
        row = db.execute("SELECT path FROM songs WHERE edition=? LIMIT 1", (subject,)).fetchone()
        if not row:
            return "excluded"
    if not index.source_enabled("YouTube Music"):
        return "temporary"
    folder = Path(row["path"]).parent
    if folder.resolve(strict=True) != folder or len(folder.relative_to(index.root).parts) != 2:
        return "excluded"
    result = subprocess.run([str(BIN / "musique-pochettes"), "--manquantes", "--en-ligne", "--suivi-activite", "--", str(folder)],
                            env=dict(os.environ, KARA_IMPORT_PROCESSING="1"), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, timeout=110)
    if result.returncode:
        return "temporary"
    if b"KARA_POCHETTES 1 0" in result.stdout:
        return "found"
    return "not_found" if b"KARA_POCHETTES 0 1" in result.stdout else "present"


def verify_documentation(index, subject):
    with index.db() as db:
        r = db.execute("SELECT config,state FROM sources WHERE id=?", (subject,)).fetchone()
    if not r or r["state"] == "excluded":
        return "excluded"
    config = json.loads(r["config"])
    addresses = set(filter(None, (config.get("docs"), config.get("terms"))))
    if not addresses:
        return "missing"
    for url in sorted(addresses):
        if fetch(url, accept="text/html") is None:
            return "missing"
    with index.db() as db:
        from time import time
        db.execute("UPDATE sources SET checked=? WHERE id=?", (time(), subject))
        db.execute("INSERT INTO source_history VALUES(?,?,?)", (time(), subject, "documentation_reached"))
    return "available"


def main():
    kind, subject = sys.argv[1:3]
    index = Index(music_library())
    if kind in ("lyrics", "lrc"):
        result = lookup_song(index, subject, kind == "lrc")
    elif kind in ("prepare_plain", "prepare_lrc"):
        # A nocturnal lookup can supply a candidate to Turbo. It never
        # retags audio or publishes any provider text to the library.
        result = lookup_song(index, subject, kind == "prepare_lrc", read_only=True)
    elif kind == "cover":
        result = {"state": cover(index, subject)}
    elif kind == "source_probe":
        result = {"state": verify_documentation(index, subject)}
    elif kind == "source":
        with index.db() as db:
            row = db.execute("SELECT payload FROM tasks WHERE kind='source' AND subject=?", (subject,)).fetchone()
        payload = json.loads(row["payload"])
        validate_endpoint(index, subject, LyricsEndpoint(payload["host"], payload["path"]),
                          payload["terms"], payload["fixtures"], lyrics_engine().best_transcript)
        result = {"state": "complete"}
    elif not index.source_enabled("MusicBrainz"):
        result = {"state": "temporary"}
    else:
        began = monotonic()
        try:
            if kind == "album":
                state, value = edition(index, subject)
            elif kind == "metadata":
                song = index.song(subject)
                if song:
                    from re import sub

                    parts = Path(song["path"]).relative_to(index.root).parts
                    tags = json.loads(song["info"])["tags"]
                    title = tags.get("title") or sub(r"^\d{1,3}\s*-\s*", "", song["title"])
                    artist = tags.get("artist") or (parts[0] if len(parts) >= 3 else "")
                    album = tags.get("album") or (parts[1] if len(parts) >= 3 else "")
                    state, value = check_recording(tags, title, artist, album, song["duration"])
                    if state == "identified":
                        value["token"] = song["token"]
                else:
                    state, value = "excluded", {}
            else:
                state, value = discography(index, subject, index.settings())
        except ProviderUnavailable:
            record(index, "MusicBrainz", "error", monotonic() - began)
            result = {"state": "temporary"}
        else:
            record(index, "MusicBrainz", "found" if state in ("complete", "incomplete", "checked", "partial", "identified")
                   else "absent", monotonic() - began)
            result = {"state": state, "value": value}
    print(document(result))


def save_correction(index, identity, suffix, original_version, text):
    from _kara_prepare import atomic, lrc_token
    from _musique_oz import share_file
    from _paroles_lrc import MAX_LRC_BYTES
    from tempfile import NamedTemporaryFile
    from time import time_ns

    song = index.song(identity)
    if not song or suffix not in (".txt", ".lrc"):
        raise ValueError("Chanson ou format indisponible")
    audio = Path(song["path"])
    if audio.resolve(strict=True) != audio or not audio.is_relative_to(index.root):
        raise ValueError("Morceau hors bibliothèque")
    raw = (text.strip() + "\n").encode()
    if not text.strip() or len(raw) > MAX_LRC_BYTES or "\x00" in text:
        raise ValueError("Texte vide ou trop volumineux")
    if suffix == ".lrc":
        stamps = [stamp for stamp, phrase in parse_lrc(text) if phrase]
        if not stamps or song["duration"] and stamps[-1] > song["duration"] + 3:
            raise ValueError("Minutage LRC invalide pour cette chanson")
    target = audio.with_suffix(suffix)
    with shared_mutation(index.root):
        if lrc_token(target) != original_version or document(file_token(audio)) != song["token"]:
            raise ValueError("Le morceau ou le texte a changé. Rouvre les détails.")
        with NamedTemporaryFile(dir=target.parent, prefix=".parole-correction-", delete=False) as file:
            draft = Path(file.name)
            try:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
                share_file(draft, target)
                if original_version is None:
                    os.link(draft, target)
                else:
                    backup = index.folder / "corrections" / identity
                    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("rb") as source:
                        atomic(backup / (str(time_ns()) + suffix), source.read(MAX_LRC_BYTES + 1))
                    if lrc_token(target) != original_version:
                        raise ValueError("Texte modifié pendant la sauvegarde")
                    os.replace(draft, target)
                directory = os.open(target.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                draft.unlink(missing_ok=True)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError, SystemExit):
        print(document({"state": "invalid" if len(sys.argv) > 1 and sys.argv[1] == "source" else "temporary"}))
