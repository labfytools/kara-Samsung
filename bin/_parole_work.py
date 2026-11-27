"""Exécute une opération bornée avec les moteurs de la bibliothèque."""
import json
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
from functools import partial
from importlib.machinery import SourceFileLoader
import math
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from threading import Lock, local
from time import monotonic

from _kara_paths import BIN, DATA, music_library
from _kara_files import file_token
from _musique_oz import shared_mutation
from _parole_catalogue import check_album, check_discography, check_recording, request, MBID
from _parole_index import SIDECAR, Index, collection_reader, document, key, reference_proof, sidecar
from _parole_import_enrichment import COVER, KINDS, LYRICS, paths_for
from _parole_import_metrics import snapshot
from _parole_local_reference import SOURCE as LOCAL_SOURCE, local_reference
from _parole_lyrics_lookup import prune
from _parole_sources import ProviderUnavailable, LyricsEndpoint, validate_endpoint, record, fetch
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc


def lyrics_engine():
    loader = SourceFileLoader("_parole_lyrics_engine", str(BIN / "paroles-musique"))
    import importlib.util

    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def lookup_song(index, identity, synced, *, read_only=False, local_reference_only=False, original_lrc=False):
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
    tags, _ = engine.probe(path, include_duration=True)
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
    duration = round(tags.pop("duration_seconds", 0.))
    if not title or not artist or not math.isfinite(duration) or duration <= 0:
        return {"state": "invalid"}
    missing = "not_found"
    if (not original_lrc and synced and (rows := parse_lrc(text)) and rows[-1][0] <= duration + 3
            and any(content for _, content in rows)):
        lyrics, provider = text, "Strawberry"
    else:
        local = None if original_lrc else local_reference(index, song, synced=synced)
        if local:
            if read_only:
                return {"state": "found", "source": LOCAL_SOURCE, "lyrics": local.text}
            # The library mutation lease coordinates the target with imports
            # and manual edits. Publish an absent sidecar without retagging.
            with shared_mutation(index.root):
                if file_token(path) != token or path.resolve() != path or not local.current():
                    return {"state": "rescan"}
                extension = ".lrc" if synced else ".txt"
                if not engine.save_lrc(path.with_suffix(extension), local.text if synced else local.text.strip(),
                                       original=synced):
                    return {"state": "present"}
                proof = reference_proof(path, extension, token, LOCAL_SOURCE)
            index.notify([path], SIDECAR, proofs={str(path): proof})
            return {"state": "found", "source": LOCAL_SOURCE, "audio": song["audio"]}
        if local_reference_only:
            # A discovery hint need not yield a trusted reference. Leave the
            # ordinary task available when a network provider can serve it.
            return {"state": "local_miss"}
        try:
            lyrics, provider = engine.LyricsSources().find(
                title, artist, tags.get("album"), duration, tags.get("albumartist"),
                synced=synced, local_text=local_text, registry=index, strict=True,
                context=engine.contexts(tags), isrc=tags.get("isrc"), recording=(path, token),
                original_lrc=synced)
        except engine.AmbiguousLyrics:
            lyrics, missing = None, "ambiguous"
        except engine.ServiceUnavailable as error:
            lyrics, missing = None, error.reason
    if lyrics is None:
        if not read_only and not original_lrc:
            from _parole_unison import discover as unison

            unison(index, song, tags.get("isrc"), lambda data: (
                engine.normalized(data.get("song")) == engine.normalized(title)
                and engine.credits(data.get("artist")) == engine.credits(artist)))
        return {"state": missing}
    if not lyrics:
        return {"state": "instrumental", "source": provider}
    if read_only:
        if len(lyrics.encode()) > MAX_LRC_BYTES:
            return {"state": "invalid"}
        return {"state": "found", "source": provider, "lyrics": lyrics}
    extension = ".lrc" if synced else ".txt"
    with shared_mutation(index.root):
        # The reader may already have the audio open. Publication is an
        # exclusive sidecar creation for the exact recording version.
        if file_token(path) != token or path.resolve() != path:
            return {"state": "rescan"}
        if not engine.save_lrc(path.with_suffix(extension), lyrics, original=synced):
            return {"state": "present"}
        proof = reference_proof(path, extension, token, provider, external=synced and provider != "Strawberry")
    index.notify([path], SIDECAR, proofs={str(path): proof})
    return {"state": "found", "source": provider}


def edition(index, identity):
    with index.db() as db:
        local = db.execute("SELECT * FROM songs WHERE edition=?", (identity,)).fetchall()
        if not local:
            return "excluded", {}
        associations = [r[0] for r in db.execute("SELECT old_artist FROM associations WHERE album=? AND new_artist=?",
                                                (local[0]["album"], local[0]["owner"]))]
    return check_album(local, associations, lookup=partial(request, registry=index))


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
                                     {key(r["album"]) for r in rows}, settings["releases"],
                                     lookup=partial(request, registry=index))
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
    result = subprocess.run([str(BIN / "musique-pochettes"), "--bibliotheque", str(index.root),
                             "--manquantes", "--en-ligne", "--suivi-activite", "--", str(folder)],
                            env=dict(os.environ, KARA_IMPORT_PROCESSING="1", KARA_PAROLE_COVER="1"), stdout=subprocess.PIPE,
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
        if fetch(url, accept="text/html", registry=index, provider=subject, validation=True) is None:
            return "missing"
    with index.db() as db:
        from time import time
        db.execute("UPDATE sources SET checked=? WHERE id=?", (time(), subject))
        db.execute("INSERT INTO source_history VALUES(?,?,?)", (time(), subject, "documentation_reached"))
    return "available"


def import_enrichment(index, kind, subject, report):
    paths = paths_for(index, kind, subject)
    if not paths:
        return {"state": "not_found"}
    os.environ["KARA_IMPORT_PROCESSING"] = "1"
    os.environ["KARA_SHARED_WAIT"] = "texte"
    if kind == COVER:
        result = subprocess.run([str(BIN / "musique-pochettes"), "--bibliotheque", str(index.root),
                                 "--manquantes", "--en-ligne", "--suivi-activite", "--", str(paths[0])],
                                env=dict(os.environ, KARA_PAROLE_COVER="1"), stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, timeout=110)
        state = ("temporary" if result.returncode else "not_found" if b"KARA_POCHETTES 0 1" in result.stdout
                 else "complete")
        return {"state": state}
    engine = lyrics_engine()
    settings = index.settings()
    with index.db() as db:
        task = db.execute("SELECT payload FROM tasks WHERE kind=? AND subject=?", (kind, subject)).fetchone()
        manual = task and json.loads(task["payload"]).get("manual")
        excluded = set() if manual else {row[0] for row in db.execute("SELECT subject FROM exclusions WHERE kind='artist'")}
    if manual:
        settings = {**settings, "lyrics": True, "lrc": True}
    try:
        with collection_reader(DATA / "strawberry/strawberry/strawberry.db", index.root, targeted=True) as read_batch:
            strawberry = read_batch(paths)
    except (OSError, sqlite3.Error):
        strawberry = {}
    limits = snapshot(index)
    worker_count = settings["import_lyrics_workers"]
    if type(worker_count) is not int or worker_count not in (4, 8, 16):
        worker_count = 4
    # The batch belongs to one maintenance owner for the whole collection.
    # HTTP gates and FFmpeg slots remain shared with other import processes.
    from _parole_sources import ready_sources
    from time import time
    ready = ready_sources(index, time())
    if (any(d.get("last_http") == 429 for d in limits.values())
            or sum(bool({"lyrics", "lrc"} & set(row["category"].split(","))) for row in ready.values()) <= 2):
        worker_count = min(worker_count, 2)
    try:
        available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:"))
        if available < settings["memory_mib"] * 1024 or os.getloadavg()[0] > (os.cpu_count() or 4):
            worker_count = min(worker_count, 2)
    except (OSError, ValueError, StopIteration):
        worker_count = min(worker_count, 2)
    clients = []
    storage = local()
    locks = tuple(Lock() for _ in range(31))
    activity_lock = Lock()
    active = [0, 0]

    def notify(path, mask, proof):
        # The receiving index coalesces these notices. Each verified file
        # becomes eligible even while another lookup in the batch is blocked.
        index.notify([path], mask, proofs={str(path): proof} if proof else None)

    def activity(synced, delta):
        with activity_lock:
            active[int(synced)] += delta
            report(dict(event="activity", lyrics=active[0], lrc=active[1]))

    def initialize():
        storage.source = engine.LyricsSources(persistent=True)
        clients.append(storage.source)

    def work(path):
        began = monotonic()
        errors, audit_items = [], []

        def audit(event):
            # Record sent requests before candidate samples. At most 100
            # candidate decisions per track keep the existing receipt bounded.
            candidates = event["type"] == "candidate"
            if len(audit_items) < 100 or not candidates:
                if "url" in event:
                    event["url"] = event["url"][:1200]
                audit_items.append(event)
            elif not any(item["type"] == "truncated" for item in audit_items):
                audit_items.append({"type": "truncated", "reason": "limite des décisions détaillées"})

        report({"event": "track-start", "path": str(path)})
        try:
            result = engine.process_import_track(path, storage.source, index, settings, excluded, locks, activity,
                                                 strawberry.get(str(path)), notify=notify, diagnostic=errors.append,
                                                 audit=audit if manual else None)
        except (OSError, ValueError, sqlite3.Error, subprocess.SubprocessError) as error:
            result = "temporary", "temporary"
            errors.append(type(error).__name__)
        detail = {"lyrics": result[0], "lrc": result[1], "errors": sorted(set(errors))}
        # Stream lightweight progress. The final SQLite receipt owns the
        # detailed audit so the process pipe carries each trace only once.
        report({"event": "track-result", "path": str(path), "result": detail})
        if manual:
            detail = {**detail, "audit": audit_items}
        return result, monotonic() - began, detail

    started = monotonic()
    try:
        with ThreadPoolExecutor(max_workers=min(worker_count, len(paths)), initializer=initialize) as pool:
            processed = list(pool.map(work, paths))
    finally:
        for source in clients:
            source.close()
    prune(index)
    plain = Counter(result[0] for result, _, _ in processed)
    timed = Counter(result[1] for result, _, _ in processed)
    missing = {"missing", "review", "invalid"}
    state = ("temporary" if any(plain[key] or timed[key] for key in ("temporary", "limited")) else
             "not_found" if missing & (plain.keys() | timed.keys()) else "complete")
    return {"state": state, "lyrics": dict(plain), "lrc": dict(timed), "seconds": monotonic() - started,
            "tracks_seconds": [seconds for _, seconds, _ in processed],
            "tracks": {str(path): row[2] for path, row in zip(paths, processed, strict=True)},
            "workers": min(worker_count, len(paths)),
            "requests": sum(c.gate.requests for source in clients for c in source.clients),
            "cache_hits": sum(c.cache_hits for c in clients),
            "providers": sorted({provider for c in clients for provider in c.providers})}


# Public diagnostic recordings keep the source test independent of the
# library and its sidecars. Catalogue matching uses the normal admission.
def test_lyrics_source(index, identity):
    from _parole_sources import lyrics_rows, seed
    from _parole_unison import probe as unison_probe

    seed(index)
    row = next((item for item in lyrics_rows(index) if item["id"] == identity), None)
    if not row:
        return {"state": "unknown"}
    if row["availability"] != "ready":
        return {"state": row["availability"], "next_available": row["next_available"]}
    if identity == "Unison":
        candidate, error = unison_probe(index, "FR8EU2300090", lambda data: data.get("song") == "Explorer")
        return {"state": "temporary" if error else "candidate" if candidate else "not_found",
                "format": candidate and candidate["format"], "detail": error}
    if identity == "LyricDown":
        # This source transfers line timings only after independent
        # verification against an existing full reference text.
        engine = lyrics_engine()
        try:
            page = engine.LyricsSources().lyricdown
            page.failure = None
            page.gate.provider = (index, identity)
            connected = page.page("/search/Yellow/") is not None
        except (OSError, ValueError, engine.ServiceUnavailable) as error:
            return {"state": "temporary", "detail": str(error)}
        return {"state": "reachable" if connected else "temporary",
                "detail": "La validation LRC exige un texte de référence." if connected else str(page.failure)}
    engine = lyrics_engine()
    client = engine.LyricsSources()
    signature = ("Yellow", "Coldplay", "Parachutes", 269, "Coldplay")
    forms = (True, False) if "lrc" in row["category"].split(",") else (False,)
    try:
        for synced in forms:
            text, _ = client.find(*signature, synced=synced, registry=index,
                                  strict=True, provider_only=identity, refresh=True)
            if text:
                return {"state": "found", "format": "mot" if synced and engine.WORD_STAMP.search(text)
                        else "ligne" if synced else "texte"}
    except engine.ServiceUnavailable as error:
        return {"state": "temporary", "detail": str(error)}
    except engine.AmbiguousLyrics as error:
        return {"state": "ambiguous", "detail": str(error)}
    return {"state": "not_found"}


def main():
    kind, subject = sys.argv[1:3]
    index = Index(Path(sys.argv[3]) if kind == "lyrics_source_test" and len(sys.argv) == 4 else music_library())
    if kind in ("lyrics", "lrc"):
        result = lookup_song(index, subject, kind == "lrc",
                             local_reference_only=sys.argv[3:] == ["--local-reference-only"],
                             original_lrc=kind == "lrc" and sys.argv[3:] == ["--original-lrc"])
    elif kind in KINDS:
        # Library-lock progress and tool diagnostics must not contaminate the
        # machine-readable result expected by the maintenance supervisor.
        output = sys.stdout
        def report(event):
            print(document(event), file=output, flush=True)
        if os.environ.get("KARA_ENRICHMENT_EVENTS") != "1":
            report = lambda _: None
        with redirect_stdout(sys.stderr):
            result = import_enrichment(index, kind, subject, report)
    elif kind in ("prepare_plain", "prepare_lrc"):
        # A nocturnal lookup can supply a candidate to Turbo. It never
        # retags audio or publishes any provider text to the library.
        result = lookup_song(index, subject, kind == "prepare_lrc", read_only=True,
                             local_reference_only=sys.argv[3:] == ["--local-reference-only"])
    elif kind == "cover":
        result = {"state": cover(index, subject)}
    elif kind == "lyrics_source_test":
        result = test_lyrics_source(index, subject)
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
                    state, value = check_recording(tags, title, artist, album, song["duration"],
                                                   lookup=partial(request, registry=index))
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


def save_correction(index, identity, suffix, original_version, text, *, expected_audio=None, draft=False):
    from _kara_files import atomic, lrc_token
    from _musique_oz import share_file
    from _paroles_lrc import MAX_LRC_BYTES
    from tempfile import NamedTemporaryFile
    from time import time_ns

    song = index.song(identity)
    if not song or suffix not in (".txt", ".lrc"):
        raise ValueError("Chanson ou format indisponible")
    if expected_audio is not None and song["token"] != expected_audio:
        raise ValueError("Le morceau a changé depuis l’ouverture de l’éditeur")
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
            temporary = Path(file.name)
            try:
                file.write(raw)
                file.flush()
                os.fsync(file.fileno())
                share_file(temporary, target)
                if original_version is None:
                    os.link(temporary, target)
                else:
                    backup = index.folder / "corrections" / identity
                    backup.mkdir(mode=0o700, parents=True, exist_ok=True)
                    with target.open("rb") as source:
                        atomic(backup / (str(time_ns()) + suffix), source.read(MAX_LRC_BYTES + 1))
                    if lrc_token(target) != original_version:
                        raise ValueError("Texte modifié pendant la sauvegarde")
                    os.replace(temporary, target)
                directory = os.open(target.parent, os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
        # The receipt qualifies the exact version saved and the origin of the
        # editor contents. Publication staging does not establish provenance.
        # If a crash precedes the receipt, the correction remains on disk.
        version, saved = sidecar(audio, suffix)
        if saved.encode() != raw or document(file_token(audio)) != song["token"]:
            raise ValueError("Le morceau ou la correction a changé après l’écriture")
        index.record_validation(identity, suffix, song["token"], version, draft=draft, path=audio)


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError, SystemExit):
        print(document({"state": "invalid" if len(sys.argv) > 1 and sys.argv[1] == "source" else "temporary"}))
