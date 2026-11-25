"""Bibliothèque et tâches de Parole. La collection Strawberry reste en lecture seule."""
from collections import defaultdict
from contextlib import closing, contextmanager
from hashlib import sha256
import json
from itertools import islice
import os
from pathlib import Path
import sqlite3
import subprocess
from datetime import date
import stat
from time import time
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from _kara_paths import STATE
from _kara_prepare import file_token
from _musique_oz import AUDIO, INBOX, UNKNOWN_ARTIST, probe
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc

DEFAULTS = {
    "startup": True, "paused": False, "lyrics": True, "lrc": True, "generate": True,
    "albums": True, "discography": True, "cover": True, "metadata": True, "autoprepare": True, "import_tracks": False, "import_albums": False,
    "codex": False, "codex_auto": False, "codex_daily": 2, "codex_timeout": 120,
    "codex_model": "", "codex_effort": "low",
    "codex_categories": ["lyrics", "lrc", "album", "discography", "track", "cover", "metadata", "identify", "import_failure"],
    "idle": 300, "battery": False, "cpu": 25, "memory_mib": 4096, "running_memory_mib": 1536, "temperature": 78,
    "gpu": 15, "disk_busy": 25, "network_interval": 20, "network_daily": 24, "network_timeout": 145, "heavy_daily": 3,
    "reserve_gib": 20, "cache_gib": 50, "scan_minutes": 15, "review_days": 30,
    "night_enabled": False, "night_screen_off": True, "night_lid": False, "night_lid_qualified": False,
    "night_idle": 420, "night_cpu_temperature": 78, "night_gpu_temperature": 75,
    "night_closed_temperature": 70, "night_minutes": 120,
    "releases": ["Album", "EP", "Mixtape/Street"],
}
LYRIC_KEYS = ("lyrics", "unsyncedlyrics", "lyricseng", "lyricsxxx")
SKIP_TOP = {INBOX, "YouTube"}


def key(text):
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold().replace("’", "'"))
                   if not unicodedata.combining(c)).strip()


def document(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def encoded_key(*parts):
    return sha256(document(parts).encode()).hexdigest()


class Index:
    def __init__(self, root, state=STATE):
        self.root = Path(root).resolve()
        self.folder = Path(state) / "kara/parole" / encoded_key(str(self.root))
        self.folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.path = self.folder / "maintenance.sqlite3"
        self.path.touch(mode=0o600, exist_ok=True)
        with self.db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS songs (
                    id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, token TEXT NOT NULL,
                    dev INTEGER NOT NULL, inode INTEGER NOT NULL, bytes INTEGER NOT NULL,
                    side TEXT NOT NULL, cache TEXT NOT NULL, previous TEXT,
                    title TEXT NOT NULL, artist TEXT NOT NULL, owner TEXT NOT NULL,
                    album TEXT NOT NULL, edition TEXT NOT NULL, duration REAL NOT NULL,
                    disc INTEGER NOT NULL, track INTEGER NOT NULL, year INTEGER NOT NULL,
                    artist_id TEXT NOT NULL, release_id TEXT NOT NULL, plays INTEGER NOT NULL,
                    lyrics TEXT NOT NULL, lrc TEXT NOT NULL, kara TEXT NOT NULL,
                    search TEXT NOT NULL, seen TEXT NOT NULL, added REAL NOT NULL,
                    checked REAL NOT NULL, info TEXT NOT NULL, audio TEXT NOT NULL DEFAULT ''
                );
                CREATE INDEX IF NOT EXISTS songs_owner ON songs(owner, album, edition, disc, track);
                CREATE INDEX IF NOT EXISTS songs_edition ON songs(edition, disc, track);
                CREATE INDEX IF NOT EXISTS songs_inode ON songs(dev, inode);
                CREATE INDEX IF NOT EXISTS songs_audio ON songs(audio);
                CREATE TABLE IF NOT EXISTS tasks (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL,
                    priority INTEGER NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                    revision TEXT NOT NULL, updated REAL NOT NULL, detail TEXT NOT NULL DEFAULT '',
                    payload TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(kind, subject)
                );
                CREATE INDEX IF NOT EXISTS tasks_ready ON tasks(state, due, priority);
                CREATE TABLE IF NOT EXISTS reviews (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, state TEXT NOT NULL,
                    checked REAL NOT NULL, value TEXT NOT NULL, PRIMARY KEY(kind, subject)
                );
                CREATE TABLE IF NOT EXISTS associations (
                    old_artist TEXT NOT NULL, album TEXT NOT NULL, new_artist TEXT NOT NULL,
                    provenance TEXT NOT NULL, PRIMARY KEY(old_artist, album, new_artist)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
                    state TEXT NOT NULL, adapter TEXT NOT NULL, config TEXT NOT NULL,
                    discovered REAL NOT NULL, checked REAL NOT NULL DEFAULT 0,
                    used REAL NOT NULL DEFAULT 0, attempts INTEGER NOT NULL DEFAULT 0,
                    successes INTEGER NOT NULL DEFAULT 0, failures INTEGER NOT NULL DEFAULT 0,
                    latency REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS source_history (
                    stamp REAL NOT NULL, source TEXT NOT NULL, action TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS exclusions (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, PRIMARY KEY(kind, subject)
                );
                CREATE TABLE IF NOT EXISTS daily_usage (
                    bucket TEXT NOT NULL, day TEXT NOT NULL, used INTEGER NOT NULL, PRIMARY KEY(bucket,day)
                );
                CREATE TABLE IF NOT EXISTS import_permissions (
                    kind TEXT NOT NULL, subject TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    decided REAL NOT NULL, job TEXT, PRIMARY KEY(kind,subject)
                );
                CREATE TABLE IF NOT EXISTS library_stats (
                    id INTEGER PRIMARY KEY CHECK(id=1), total INTEGER NOT NULL,
                    bytes INTEGER NOT NULL, lyrics INTEGER NOT NULL, lrc INTEGER NOT NULL,
                    ready INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO library_stats VALUES(1,0,0,0,0,0);
                CREATE TRIGGER IF NOT EXISTS song_added AFTER INSERT ON songs BEGIN
                    UPDATE library_stats SET total=total+1, bytes=bytes+new.bytes,
                        lyrics=lyrics+(new.lyrics NOT IN ('absent','instrumental','inconnu')),
                        lrc=lrc+(new.lrc IN ('local','intégré','externe','généré')),
                        ready=ready+(new.kara='prêt');
                END;
                CREATE TRIGGER IF NOT EXISTS song_removed AFTER DELETE ON songs BEGIN
                    UPDATE library_stats SET total=total-1, bytes=bytes-old.bytes,
                        lyrics=lyrics-(old.lyrics NOT IN ('absent','instrumental','inconnu')),
                        lrc=lrc-(old.lrc IN ('local','intégré','externe','généré')),
                        ready=ready-(old.kara='prêt');
                END;
                CREATE TRIGGER IF NOT EXISTS song_changed AFTER UPDATE OF bytes,lyrics,lrc,kara ON songs BEGIN
                    UPDATE library_stats SET bytes=bytes+new.bytes-old.bytes,
                        lyrics=lyrics+(new.lyrics NOT IN ('absent','instrumental','inconnu'))
                                     -(old.lyrics NOT IN ('absent','instrumental','inconnu')),
                        lrc=lrc+(new.lrc IN ('local','intégré','externe','généré'))
                               -(old.lrc IN ('local','intégré','externe','généré')),
                        ready=ready+(new.kara='prêt')-(old.kara='prêt');
                END;
            """)

    @contextmanager
    def db(self, wait=3):
        # Each caller owns its connection. WAL keeps readers responsive during
        # an inventory transaction and exposes only complete publications.
        with closing(sqlite3.connect(self.path, timeout=wait)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def settings(self, *, wait=3):
        with self.db(wait=wait) as db:
            result = dict(DEFAULTS)
            result.update((row["key"], json.loads(row["value"])) for row in db.execute("SELECT * FROM settings"))
            result["reserve_gib"] = max(DEFAULTS["reserve_gib"], result["reserve_gib"])
            return result

    def night_budget(self, since=...):
        # CLOCK_BOOTTIME values survive a service restart on this boot only.
        # Store them beside the other maintenance settings without a migration.
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        with self.db(wait=.15) as db:
            if since is None:
                db.execute("DELETE FROM settings WHERE key='night_budget'")
            elif since is not ...:
                db.execute("INSERT OR REPLACE INTO settings VALUES('night_budget',?)", (document([boot, since]),))
            else:
                row = db.execute("SELECT value FROM settings WHERE key='night_budget'").fetchone()
                if row:
                    saved_boot, started = json.loads(row[0])
                    if saved_boot == boot:
                        return started
        return None

    def preparation_due(self, now):
        with self.db(wait=.15) as db:
            return db.execute("""SELECT 1 FROM tasks WHERE kind='prepare' AND due<=?
                                 AND state IN ('queued','suspended','temporary') LIMIT 1""", (now,)).fetchone() is not None

    @staticmethod
    def read_night_report(db):
        row = db.execute("SELECT value FROM settings WHERE key='night_report'").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def write_night_report(db, report):
        db.execute("INSERT OR REPLACE INTO settings VALUES('night_report',?)", (document(report),))

    def night_progress(self):
        with self.db(wait=.15) as db:
            ready = db.execute("SELECT ready FROM library_stats WHERE id=1").fetchone()[0]
            remaining = db.execute("""
                SELECT count(*) FROM tasks JOIN songs ON songs.id=tasks.subject
                WHERE tasks.kind='prepare' AND tasks.state IN ('queued','running','suspended','temporary')
                AND songs.kara!='prêt' AND songs.lyrics!='instrumental'
                AND NOT EXISTS(SELECT 1 FROM exclusions WHERE kind='artist' AND subject=songs.owner)
            """).fetchone()[0]
            return ready, remaining, self.read_night_report(db)

    def configure(self, **changes):
        with self.db() as db:
            db.executemany("INSERT OR REPLACE INTO settings VALUES(?,?)",
                           ((name, document(value)) for name, value in changes.items() if name in DEFAULTS))

    def enqueue(self, kind, subject, revision, priority=30, payload=None, *, now=None, force=False, db=None):
        now = time() if now is None else now
        if db is None:
            with self.db() as db:
                return self.enqueue(kind, subject, revision, priority, payload, now=now, force=force, db=db)
        return db.execute("""
            INSERT INTO tasks(kind,subject,state,priority,due,revision,updated,payload)
            VALUES(?,?,'queued',?,?,?,?,?)
            ON CONFLICT(kind,subject) DO UPDATE SET state='queued', priority=excluded.priority,
                due=excluded.due, revision=excluded.revision, updated=excluded.updated, attempts=0,
                detail='', payload=excluded.payload
            WHERE tasks.state!='running' AND (tasks.revision!=excluded.revision OR ?)
        """, (kind, subject, priority, now, revision, now, document(payload or {}), force)).rowcount > 0

    def recover(self):
        # Only the application holding the maintenance lease may recover claims.
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            report = self.read_night_report(db)
            interrupted = bool(report and report["pending"] is not None)
            if interrupted:
                report["interrupted"] += 1
                report["pending"] = None
                self.write_night_report(db, report)
            db.execute("UPDATE tasks SET state='suspended' WHERE state='running'")
            return interrupted

    @staticmethod
    def can_reserve(db, bucket, day, now, limit, gap):
        if limit <= 0:
            return False
        used = db.execute("SELECT used FROM daily_usage WHERE bucket=? AND day=?", (bucket, day)).fetchone()
        if used and used[0] >= limit:
            return False
        if gap:
            stamp = db.execute("SELECT value FROM settings WHERE key=?", ("network_last_" + bucket,)).fetchone()
            if stamp and now < float(stamp[0]) + gap:
                return False
        return True

    @staticmethod
    def reserve(db, bucket, day, now, gap):
        db.execute("""INSERT INTO daily_usage VALUES(?,?,1) ON CONFLICT(bucket,day)
                      DO UPDATE SET used=used+1""", (bucket, day))
        db.execute("DELETE FROM daily_usage WHERE day<date(?,'-31 days')", (day,))
        if gap:
            db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", ("network_last_" + bucket, document(now)))

    def reserve_daily(self, bucket, limit, *, now=None, gap=0):
        now = time() if now is None else now
        day = date.fromtimestamp(now).isoformat()
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not self.can_reserve(db, bucket, day, now, limit, gap):
                return False
            self.reserve(db, bucket, day, now, gap)
            return True

    def claim(self, kinds, now, *, bucket=None, limit=0, high=False, codex_auto=True, gap=0, night=None):
        if not kinds:
            return None
        placeholders = ",".join("?" for _ in kinds)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            day = date.fromtimestamp(now).isoformat()
            if bucket and not self.can_reserve(db, bucket, day, now, limit, gap):
                return None
            task = db.execute(f"""
                UPDATE tasks SET state='running', updated=?, attempts=attempts+1
                WHERE rowid=(SELECT rowid FROM tasks WHERE kind IN ({placeholders})
                    AND state IN ('queued','suspended','temporary') AND due<=? AND (NOT ? OR priority<=15)
                    AND (? OR kind!='codex' OR json_extract(payload,'$.manual')=1
                         OR json_extract(payload,'$.kind') IN ('adapter','import_patch'))
                    ORDER BY max(10,priority-cast((?-due)/86400 AS INTEGER)),due,subject LIMIT 1)
                RETURNING *
            """, (now, *kinds, now, high, codex_auto, now)).fetchone()
            if bucket and task is not None:
                self.reserve(db, bucket, day, now, gap)
            if night is not None and task is not None:
                since, storage = night
                window = [Path("/proc/sys/kernel/random/boot_id").read_text().strip(), since]
                report = self.read_night_report(db)
                # Reserve one durable receipt with the queue claim. A crash
                # leaves the last completed totals and one interrupted item.
                if not report or report["window"] != window:
                    report = dict(window=window, started=now, seconds=0., ready=0, corrected=0, generated=0,
                                  failed=0, deferred=0, interrupted=0, cache_baseline=storage, cache_bytes=storage)
                report["pending"] = [task["subject"], task["revision"]]
                self.write_night_report(db, report)
            return task

    def finish(self, task, state, detail="", delay=0, *, night=None):
        now = time()
        with self.db() as db:
            changed = db.execute("""UPDATE tasks SET state=?, detail=?, updated=?, due=?
                          WHERE kind=? AND subject=? AND state='running' AND revision=?""",
                                 (state, detail[:300], now, now + delay,
                                  task["kind"], task["subject"], task["revision"])).rowcount
            if night is not None:
                report = self.read_night_report(db)
                if report and report["pending"] == [task["subject"], task["revision"]]:
                    report["pending"] = None
                    report["seconds"] += night["seconds"]
                    if night["cache_bytes"] is not None:
                        report["cache_bytes"] = night["cache_bytes"]
                    if changed:
                        report[night["outcome"]] += 1
                        report["corrected"] += night["corrected"]
                        report["generated"] += night["generated"]
                    else:
                        report["interrupted"] += 1
                    self.write_night_report(db, report)

    def exclude(self, kind, subject):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?)", (kind, subject))

    def cancel(self, kind, subject):
        with self.db() as db:
            db.execute("UPDATE tasks SET state='excluded',updated=? WHERE kind=? AND subject=?",
                       (time(), kind, subject))

    def song(self, identity):
        with self.db() as db:
            return db.execute("SELECT * FROM songs WHERE id=?", (identity,)).fetchone()

    def update(self, identity, **fields):
        assert set(fields) <= {"lyrics", "lrc", "kara", "info", "cache", "audio", "side", "previous", "checked"}
        with self.db() as db:
            db.execute(f"UPDATE songs SET {','.join(name+'=?' for name in fields)} WHERE id=?",
                       (*fields.values(), identity))

    def source_enabled(self, identity):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM sources WHERE id=? AND state='active'", (identity,)).fetchone())

    def review(self, kind, subject, state, value):
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                       (kind, subject, state, time(), document(value)))

    def associations(self, state=STATE):
        # A manifest proves a specific album move only. This registry never
        # treats a collaboration or a featuring as a global artist alias.
        rows = set()
        for path in (Path(state) / "kara/music-tidy-backups").glob("*/manifest.json"):
            try:
                if path.is_symlink() or path.stat().st_size > 8_000_000:
                    continue
                manifest = json.loads(path.read_text())
                if manifest.get("status") != "complete":
                    continue
                for row in manifest.get("files", []):
                    source, dest = (Path(row[k]) for k in ("source", "destination"))
                    if any(p.is_absolute() or ".." in p.parts or len(p.parts) < 3 for p in (source, dest)):
                        continue
                    if row.get("changes", {}).get("albumartist") == dest.parts[0] and source.parts[0] != dest.parts[0]:
                        rows.add((source.parts[0], source.parts[1], dest.parts[0], path.parent.name))
            except (OSError, ValueError, KeyError, TypeError):
                continue
        with self.db() as db:
            db.executemany("INSERT OR IGNORE INTO associations VALUES(?,?,?,?)", sorted(rows))
        return len(rows)


@contextmanager
def collection_reader(database, root):
    if not database or not Path(database).is_file():
        yield lambda paths: {}
        return
    with closing(sqlite3.connect(Path(database).absolute().as_uri() + "?mode=ro", uri=True, timeout=3)) as db:
        db.row_factory = sqlite3.Row
        available = {r[1] for r in db.execute("PRAGMA table_info(songs)")}
        if "url" not in available:
            yield lambda paths: {}
            return
        columns = ("url title artist album albumartist lyrics length track disc year mtime filesize playcount "
                   "musicbrainz_album_id musicbrainz_album_artist_id musicbrainz_recording_id").split()
        selected = ",".join(c if c in available else f"NULL AS {c}" for c in columns)
        where = " WHERE unavailable=0" if "unavailable" in available else ""
        catalog = {}
        for rowid, value in db.execute("SELECT rowid,url FROM songs" + where + " ORDER BY rowid"):
            url = urlsplit(value or "")
            if url.scheme == "file" and url.netloc in ("", "localhost"):
                path = str(Path(unquote(url.path)))
                if Path(path).is_relative_to(root):
                    catalog.setdefault(path, rowid)

        def batch(paths):
            ids = {catalog[str(path)]: str(path) for path in paths if str(path) in catalog}
            if not ids:
                return {}
            marks = ",".join("?" for _ in ids)
            return {ids[row[0]]: row for row in db.execute("SELECT rowid," + selected
                                                          + f" FROM songs WHERE rowid IN ({marks})", tuple(ids))}
        yield batch


def integer(value):
    try:
        return max(0, min(2**31 - 1, int(str(value).split("/")[0])))
    except (ValueError, TypeError):
        return 0


def sidecar(path, suffix, limit=MAX_LRC_BYTES):
    candidate = path.with_suffix(suffix)
    try:
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return "invalid", ""
        with os.fdopen(os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                return "invalid", ""
            version = os.fstat(stream.fileno())
            return document([version.st_dev, version.st_ino, version.st_size, version.st_mtime_ns,
                             version.st_ctime_ns]), raw.decode("utf-8-sig")
    except FileNotFoundError:
        return "", ""
    except (OSError, UnicodeError, ValueError):
        return "invalid", ""


def local_state(path, embedded, duration):
    lrc_token_text, lrc = sidecar(path, ".lrc")
    plain_token, txt = sidecar(path, ".txt")
    embedded_rows = parse_lrc(embedded)
    timed = [stamp for stamp, text in parse_lrc(lrc) if text.strip()]
    lrc_state = ("local" if timed and (not duration or 0 < timed[-1] <= duration + 3)
                 else "à vérifier" if lrc_token_text else
                 "intégré" if embedded_rows and (not duration or 0 < embedded_rows[-1][0] <= duration + 3)
                 else "absent")
    lyrics = ("fichier texte" if txt.strip() else "Strawberry" if embedded.strip() else
              "LRC" if lrc_state == "local" else "absent")
    return document([lrc_token_text, plain_token]), lyrics, lrc_state


def audio_paths(root, onerror=None):
    for folder, children, files in os.walk(root, onerror=onerror):
        children[:] = sorted(name for name in children if not name.startswith(".")
                             and (Path(folder) != root or name not in SKIP_TOP))
        for name in sorted(files):
            if not name.startswith(".") and Path(name).suffix.lower() in AUDIO:
                yield Path(folder) / name


def probe_track(path):
    return probe(path, include_duration=True)


def inventory(index, collection, inspect_cache, stop, progress, *, limit=None, paths=None, reader=probe_track):
    root = index.root
    if not root.is_dir():
        raise OSError("Bibliothèque indisponible")
    # Read the previous generation once. Only changed files need a tag probe;
    # presence and sidecar tokens are checked independently of the audio file.
    with index.db() as db:
        rows = (db.execute("SELECT * FROM songs") if paths is None else
                (row for path in paths for row in db.execute("SELECT * FROM songs WHERE path=?", (str(path),))))
        previous = {r["path"]: r for r in rows}
    inodes = defaultdict(list)
    for row in previous.values():
        inodes[row["dev"], row["inode"]].append(row)
    generation = uuid4().hex
    counts = dict(scanned=0, changed=0, missing=0, errors=0)
    pending, unchanged = [], []
    now = time()

    def flush():
        with index.db() as db:
            db.executemany("UPDATE songs SET seen=? WHERE id=?", ((generation, i) for i in unchanged))
            db.executemany("""
                INSERT INTO songs VALUES(
                  :id,:path,:token,:dev,:inode,:bytes,:side,:cache,:previous,:title,:artist,:owner,
                  :album,:edition,:duration,:disc,:track,:year,:artist_id,:release_id,:plays,
                  :lyrics,:lrc,:kara,:search,:seen,:added,:checked,:info,:audio
                ) ON CONFLICT(id) DO UPDATE SET
                  path=excluded.path,token=excluded.token,dev=excluded.dev,inode=excluded.inode,
                  bytes=excluded.bytes,side=excluded.side,cache=excluded.cache,previous=excluded.previous,
                  title=excluded.title,artist=excluded.artist,owner=excluded.owner,album=excluded.album,
                  edition=excluded.edition,duration=excluded.duration,disc=excluded.disc,track=excluded.track,
                  year=excluded.year,artist_id=excluded.artist_id,release_id=excluded.release_id,
                  plays=excluded.plays,lyrics=excluded.lyrics,lrc=excluded.lrc,kara=excluded.kara,
                  search=excluded.search,seen=excluded.seen,checked=excluded.checked,info=excluded.info,audio=excluded.audio
            """, pending)
        pending.clear()
        unchanged.clear()

    walk_failed = False

    def error(_):
        nonlocal walk_failed
        walk_failed = True
        counts["errors"] += 1

    def files():
        with collection_reader(collection, root) as read_batch:
            paths_iter = iter(audio_paths(root, error) if paths is None else paths)
            while batch := tuple(islice(paths_iter, 128)):
                rows = read_batch(batch)
                for path in batch:
                    yield path, rows.get(str(path))

    for path, strawberry in files():
        if stop() or (limit is not None and counts["scanned"] >= limit):
            break
        try:
            st = path.lstat()
            if not stat.S_ISREG(st.st_mode):
                continue
            token = document([st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns])
            old = previous.get(str(path))
            matches = inodes[st.st_dev, st.st_ino]
            if old is None and len(matches) == 1:
                known = json.loads(matches[0]["token"])
                if not Path(matches[0]["path"]).exists() and known[2:4] == [st.st_size, st.st_mtime_ns]:
                    old = matches.pop()
            same_audio = old is not None and old["token"] == token
            same_metadata = same_audio and old["path"] == str(path)
            duration = strawberry["length"] / 1e9 if strawberry and (strawberry["length"] or 0) > 0 else (
                old["duration"] if same_audio else 0)
            catalog_text = (strawberry["lyrics"] or "") if strawberry else ""
            catalog_matches = strawberry and (st.st_size, int(st.st_mtime)) == (strawberry["filesize"], strawberry["mtime"])
            catalog_revision = encoded_key(tuple(strawberry) if strawberry else ())
            info = json.loads(old["info"]) if old else {}
            if (same_metadata and catalog_revision == info.get("catalogue") and not info.get("probe_error")):
                tags = info["tags"]
            else:
                if catalog_matches:
                    tags = dict(strawberry)
                    tags["albumid"] = strawberry["musicbrainz_album_id"]
                    tags["artistid"] = strawberry["musicbrainz_album_artist_id"]
                    tags["recordingid"] = strawberry["musicbrainz_recording_id"]
                else:
                    try:
                        tags, _ = reader(path)
                    except (OSError, ValueError, subprocess.SubprocessError):
                        tags = dict(strawberry) if strawberry else {}
                        info["probe_error"] = True
                        counts["errors"] += 1
                    else:
                        info.pop("probe_error", None)
            duration = tags.get("duration_seconds") or duration
            embedded = (info.get("plain", "") if same_metadata and catalog_revision == info.get("catalogue") else
                        next((tags.get(k) for k in LYRIC_KEYS if tags.get(k)), "") or catalog_text)
            if len(embedded.encode()) > MAX_LRC_BYTES:
                embedded = ""
            side, lyrics, lrc = local_state(path, embedded, duration)
            cache, kara, report = inspect_cache(path, old, token, side)
            if same_metadata and side == old["side"] and catalog_revision == info.get("catalogue"):
                if lyrics == "absent" and old["lyrics"] == "instrumental":
                    lyrics = old["lyrics"]
                if lrc == "local" and old["lrc"] in ("externe", "généré"):
                    lrc = old["lrc"]
                if cache == old["cache"] and kara == old["kara"] and lyrics == old["lyrics"] and lrc == old["lrc"]:
                    unchanged.append(old["id"])
                    counts["scanned"] += 1
                    if len(unchanged) >= 256:
                        flush()
                        progress(counts["scanned"])
                    continue
            title = tags.get("title") or path.stem
            artist = tags.get("artist") or UNKNOWN_ARTIST
            owner = tags.get("albumartist") or artist
            album = tags.get("album") or ""
            release_id = tags.get("albumid") or tags.get("musicbrainzalbumid") or ""
            year = integer(str(tags.get("year") or tags.get("date") or "")[:4])
            edition = encoded_key(owner, album, release_id or year, str(path.parent))
            info.update(tags={k: v for k, v in tags.items() if k not in LYRIC_KEYS},
                        plain=embedded, catalogue=catalog_revision, report=report)
            pending.append(dict(id=old["id"] if old else uuid4().hex, path=str(path), token=token,
                                dev=st.st_dev, inode=st.st_ino, bytes=st.st_size, side=side, cache=cache,
                                previous=(old["path"] if old["path"] != str(path) else old["previous"]) if old else None,
                                title=title, artist=artist, owner=owner, album=album, edition=edition, duration=duration,
                                disc=integer(tags.get("disc") or tags.get("discnumber")), track=integer(tags.get("track") or tags.get("tracknumber")),
                                year=year, artist_id=tags.get("artistid") or tags.get("musicbrainzalbumartistid") or "",
                                release_id=release_id, plays=integer(strawberry["playcount"]) if strawberry else 0,
                                lyrics=lyrics, lrc=lrc, kara=kara, search=key(" ".join((title, artist, owner, album))),
                                seen=generation, added=old["added"] if old else now, checked=now, info=document(info),
                                audio=old["audio"] if same_audio else ""))
            counts["scanned"] += 1
            counts["changed"] += 1
            if len(pending) >= 128:
                flush()
                progress(counts["scanned"])
        except (OSError, ValueError, UnicodeError):
            if old := previous.get(str(path)):
                unchanged.append(old["id"])
            counts["errors"] += 1
    flush()
    # A partial or interrupted scan is never proof of disappearance.
    if paths is None and limit is None and not stop() and not walk_failed:
        with index.db() as db:
            counts["missing"] = db.execute("DELETE FROM songs WHERE seen!=?", (generation,)).rowcount
    return counts
