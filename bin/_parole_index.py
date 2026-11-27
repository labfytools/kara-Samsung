"""Bibliothèque et tâches de Parole. La collection Strawberry reste en lecture seule."""
from collections import defaultdict
from contextlib import closing, contextmanager, nullcontext
from hashlib import sha256
from functools import lru_cache
import json
from itertools import islice
import os
from pathlib import Path
import sqlite3
import subprocess
from datetime import date, datetime, timedelta
import stat
from time import perf_counter, time
from urllib.parse import quote, unquote, urlsplit
from uuid import uuid4

from _kara_paths import STATE
from _kara_files import file_token
from _musique_oz import AUDIO, INBOX, UNKNOWN_ARTIST, album_groups, manual_album_artist, probe
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc
from _parole_taglib import helper as taglib_helper, read as taglib_read

MEDIA, SIDECAR, PREPARATION, REMOVE, ARTWORK = (1 << i for i in range(5))
LEXICON = "lexicon"
EVENT_BATCH = 64
EVENT_DELAY = .25
EVENT_MAX_DELAY = 1.5
REMOVAL_DELAY = .75
COVER_NAMES = {"cover", "folder", "album", "albumart", "front"}
COVER_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".tiff"}

DEFAULTS = {
    "startup": True, "paused": False, "lyrics": True, "lrc": True, "generate": True,
    "albums": True, "discography": True, "cover": True, "metadata": True, "import_tracks": False, "import_albums": False,
    "codex": False, "codex_auto": False, "codex_daily": 2, "codex_timeout": 120,
    "codex_model": "", "codex_effort": "low",
    "codex_categories": ["lyrics", "lrc", "album", "discography", "track", "cover", "metadata", "identify", "import_failure"],
    "idle": 300, "battery": False, "cpu": 25, "memory_mib": 4096, "running_memory_mib": 1536, "temperature": 78,
    "import_strategy": "speed", "import_preferred": "",
    "import_lyrics_workers": 4,
    "gpu": 15, "disk_busy": 25, "network_timeout": 145, "network_workers": 4, "heavy_daily": 3,
    "reserve_gib": 20, "scan_minutes": 15, "review_days": 30,
    "night_enabled": False, "night_screen_off": True, "night_lid": False, "night_lid_qualified": False,
    "night_transcribe": True,
    "night_idle": 420, "night_cpu_temperature": 78, "night_gpu_temperature": 75,
    "night_closed_temperature": 70, "night_minutes": 480,
    "releases": ["Album", "EP", "Mixtape/Street"],
}
LYRIC_KEYS = ("lyrics", "unsyncedlyrics", "lyricseng", "lyricsxxx")
SKIP_TOP = {INBOX, "YouTube"}
LOCAL_REFERENCE_MISS = "aucune preuve locale"

# Start with indexed PCM identities and embedded LRCs. A local turn must
# stay cheap even when thousands of network tasks are waiting for quotas.
# The worker still verifies the waveform identity receipt and current files.
LOCAL_REFERENCE_SCOPE = """
    FROM (
        SELECT id,audio,lyrics,lrc,checked FROM songs INDEXED BY songs_audio
        WHERE audio>'' AND length(audio)=64
        UNION ALL
        SELECT id,audio,lyrics,lrc,checked FROM songs WHERE lrc='intégré'
        AND NOT (audio>'' AND length(audio)=64)
    ) AS target
    CROSS JOIN (SELECT 'lyrics' AS kind UNION ALL SELECT 'lrc') AS category
    CROSS JOIN tasks AS candidate
    WHERE candidate.kind=category.kind AND candidate.subject=target.id
      AND (
          (candidate.kind='lrc' AND target.lrc='intégré' AND (candidate.detail<>? OR target.checked>candidate.updated))
          OR (length(target.audio)=64
              AND ((candidate.kind='lyrics' AND target.lyrics='absent')
                   OR (candidate.kind='lrc' AND target.lrc='absent'))
              AND EXISTS (
                  SELECT 1 FROM songs AS donor WHERE donor.audio=target.audio AND donor.id<>target.id
                    AND (donor.lrc IN ('local','externe','généré')
                         OR (candidate.kind='lyrics' AND donor.lyrics NOT IN ('absent','instrumental')))
                    AND (candidate.detail<>? OR donor.checked>candidate.updated OR target.checked>candidate.updated)
              ))
      )
      AND
"""

# A present sidecar is left for manual review even when its timestamps are
# invalid. Only the absent file token is eligible for a provider publication.
NIGHT_LRC_MISSING = "coalesce(CASE WHEN json_valid(s.side) THEN json_extract(s.side,'$[0]') END,'')=''"
NIGHT_LRC_STATE = """(candidate.state IN ('queued','suspended','temporary')
    OR candidate.state='review' AND candidate.detail IN ('not_found','correspondance ambiguë'))"""
NIGHT_LRC_TRACK = f""" AND candidate.kind='lrc' AND EXISTS(
    SELECT 1 FROM songs s WHERE s.id=candidate.subject AND {NIGHT_LRC_MISSING}
    AND NOT EXISTS(SELECT 1 FROM exclusions e WHERE e.kind='artist' AND e.subject=s.owner))"""


def task_scope(local_reference):
    if local_reference:
        return LOCAL_REFERENCE_SCOPE, (LOCAL_REFERENCE_MISS, LOCAL_REFERENCE_MISS)
    return "FROM tasks AS candidate WHERE", ()


def key(text):
    import unicodedata

    return "".join(c for c in unicodedata.normalize("NFKD", text.casefold().replace("’", "'"))
                   if not unicodedata.combining(c)).strip()


def document(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def encoded_key(*parts):
    return sha256(document(parts).encode()).hexdigest()


def track_plan_revision(song):
    return encoded_key(song["token"], song["side"], song["title"], song["artist"], song["edition"])


def queue_lexicon(db, identity, now):
    # A newer receipt supersedes even a claim already computing forms. The
    # worker can settle only the exact revision it claimed.
    db.execute("""INSERT INTO tasks(kind,subject,state,priority,due,revision,updated,payload)
                  VALUES(?,?,'queued',30,?,?,?,'{}')
                  ON CONFLICT(kind,subject) DO UPDATE SET state='queued',due=excluded.due,
                    revision=excluded.revision,updated=excluded.updated,attempts=0,detail=''
               """, (LEXICON, identity, now, uuid4().hex, now))


def forget_lexicon(db, identity):
    db.execute("DELETE FROM tasks WHERE kind=? AND subject=?", (LEXICON, identity))
    db.execute("DELETE FROM reviews WHERE kind=? AND subject=?", (LEXICON, identity))


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
                -- The visible library page stops after its LIMIT in this
                -- exact order, with id breaking ties between shared credits.
                CREATE INDEX IF NOT EXISTS songs_display ON songs(owner, album, disc, track, title, id);
                CREATE INDEX IF NOT EXISTS songs_release_position ON songs(release_id,track,disc) WHERE release_id!='';
                CREATE INDEX IF NOT EXISTS songs_edition ON songs(edition, disc, track);
                CREATE INDEX IF NOT EXISTS songs_inode ON songs(dev, inode);
                CREATE INDEX IF NOT EXISTS songs_audio ON songs(audio);
                CREATE INDEX IF NOT EXISTS songs_lrc_embedded ON songs(lrc) WHERE lrc='intégré';
                CREATE INDEX IF NOT EXISTS songs_probe_error ON songs(id) WHERE json_extract(info,'$.probe_error')=1;
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
                CREATE TABLE IF NOT EXISTS import_provider_metrics (
                    source TEXT NOT NULL, metric TEXT NOT NULL, mean REAL NOT NULL,
                    samples INTEGER NOT NULL, total REAL NOT NULL, updated REAL NOT NULL,
                    PRIMARY KEY(source, metric)
                );
                CREATE TABLE IF NOT EXISTS import_provider_limits (
                    source TEXT PRIMARY KEY, until REAL NOT NULL, status INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_offer_plans (
                    id TEXT PRIMARY KEY, offers TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_editions (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL, year INTEGER NOT NULL,
                    mb_artist TEXT NOT NULL, mb_group TEXT NOT NULL, mb_release TEXT NOT NULL, gtin TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS canonical_edition_release ON canonical_editions(mb_release)
                    WHERE mb_release!='';
                CREATE INDEX IF NOT EXISTS canonical_edition_gtin ON canonical_editions(gtin) WHERE gtin!='';
                CREATE TABLE IF NOT EXISTS canonical_recordings (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL, artist TEXT NOT NULL,
                    pcm TEXT NOT NULL, mb_recording TEXT NOT NULL, mb_work TEXT NOT NULL, isrc TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS canonical_recording_pcm ON canonical_recordings(pcm);
                CREATE INDEX IF NOT EXISTS canonical_recording_credit ON canonical_recordings(title,artist);
                CREATE TABLE IF NOT EXISTS canonical_fingerprints (
                    pcm TEXT PRIMARY KEY, fingerprint TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS canonical_references (
                    id TEXT PRIMARY KEY, provider TEXT NOT NULL, external_id TEXT NOT NULL,
                    edition TEXT NOT NULL REFERENCES canonical_editions(id),
                    recording TEXT NOT NULL REFERENCES canonical_recordings(id),
                    path TEXT NOT NULL, song TEXT NOT NULL, sha256 TEXT NOT NULL, disc INTEGER NOT NULL,
                    track INTEGER NOT NULL, duration INTEGER NOT NULL, confidence TEXT NOT NULL,
                    tags TEXT NOT NULL, evidence TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS canonical_reference_file ON canonical_references(sha256);
                CREATE INDEX IF NOT EXISTS canonical_reference_recording ON canonical_references(recording);
                CREATE INDEX IF NOT EXISTS canonical_reference_path ON canonical_references(path);
                CREATE INDEX IF NOT EXISTS canonical_reference_song ON canonical_references(song);
                CREATE TABLE IF NOT EXISTS canonical_aliases (
                    album TEXT NOT NULL, first_key TEXT NOT NULL, second_key TEXT NOT NULL,
                    first_name TEXT NOT NULL, second_name TEXT NOT NULL,
                    PRIMARY KEY(album, first_key, second_key)
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
                CREATE TABLE IF NOT EXISTS inventory_root (
                    id INTEGER PRIMARY KEY CHECK(id=1), dev INTEGER NOT NULL, inode INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS index_schema (name TEXT PRIMARY KEY);
                CREATE TABLE IF NOT EXISTS index_events (
                    path TEXT PRIMARY KEY, mask INTEGER NOT NULL, serial TEXT NOT NULL,
                    first REAL NOT NULL, due REAL NOT NULL, attempts INTEGER NOT NULL,
                    combined INTEGER NOT NULL, proof TEXT
                );
                CREATE INDEX IF NOT EXISTS index_events_due ON index_events(due,path);
                CREATE INDEX IF NOT EXISTS index_events_light ON index_events(due,path)
                    WHERE proof IS NOT NULL OR mask & 1=0;
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
            if not db.execute("SELECT 1 FROM index_schema WHERE name='dimensions-v1'").fetchone():
                self.migrate_dimensions(db)

    @staticmethod
    def migrate_dimensions(db):
        # Backfill under the same writer transaction as trigger installation.
        # Every normal publication changes only its old and new dimensions.
        db.executescript("""
            BEGIN IMMEDIATE;
            CREATE TABLE IF NOT EXISTS library_artists (owner TEXT PRIMARY KEY, tracks INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS library_editions (edition TEXT PRIMARY KEY, tracks INTEGER NOT NULL);
            INSERT INTO library_artists SELECT owner,count(*) FROM songs
                WHERE NOT EXISTS(SELECT 1 FROM index_schema WHERE name='dimensions-v1') GROUP BY owner;
            INSERT INTO library_editions SELECT edition,count(*) FROM songs WHERE album!=''
                AND NOT EXISTS(SELECT 1 FROM index_schema WHERE name='dimensions-v1') GROUP BY edition;
            CREATE TRIGGER IF NOT EXISTS library_dimension_added AFTER INSERT ON songs BEGIN
                INSERT INTO library_artists VALUES(new.owner,1) ON CONFLICT(owner) DO UPDATE SET tracks=tracks+1;
                INSERT INTO library_editions SELECT new.edition,1 WHERE new.album!=''
                    ON CONFLICT(edition) DO UPDATE SET tracks=tracks+1;
            END;
            CREATE TRIGGER IF NOT EXISTS library_dimension_removed AFTER DELETE ON songs BEGIN
                UPDATE library_artists SET tracks=tracks-1 WHERE owner=old.owner;
                DELETE FROM library_artists WHERE owner=old.owner AND tracks=0;
                UPDATE library_editions SET tracks=tracks-1 WHERE edition=old.edition AND old.album!='';
                DELETE FROM library_editions WHERE edition=old.edition AND tracks=0;
            END;
            CREATE TRIGGER IF NOT EXISTS library_artist_changed AFTER UPDATE OF owner ON songs
            WHEN old.owner!=new.owner BEGIN
                UPDATE library_artists SET tracks=tracks-1 WHERE owner=old.owner;
                DELETE FROM library_artists WHERE owner=old.owner AND tracks=0;
                INSERT INTO library_artists VALUES(new.owner,1) ON CONFLICT(owner) DO UPDATE SET tracks=tracks+1;
            END;
            CREATE TRIGGER IF NOT EXISTS library_edition_changed AFTER UPDATE OF edition,album ON songs
            WHEN old.edition!=new.edition OR (old.album='')!=(new.album='') BEGIN
                UPDATE library_editions SET tracks=tracks-1 WHERE edition=old.edition AND old.album!='';
                DELETE FROM library_editions WHERE edition=old.edition AND tracks=0;
                INSERT INTO library_editions SELECT new.edition,1 WHERE new.album!=''
                    ON CONFLICT(edition) DO UPDATE SET tracks=tracks+1;
            END;
            INSERT OR IGNORE INTO index_schema VALUES('dimensions-v1');
            COMMIT;
        """)

    def audit_dimensions(self):
        with self.db() as db:
            db.execute("BEGIN")
            # Compare complete memberships, including counts in each group.
            memberships = all(db.execute(f"""WITH expected AS (
                           SELECT {key},count(*) AS tracks FROM songs {where} GROUP BY {key}
                       ) SELECT
                           NOT EXISTS(SELECT * FROM expected EXCEPT SELECT * FROM {table})
                           AND NOT EXISTS(SELECT * FROM {table} EXCEPT SELECT * FROM expected)""").fetchone()[0]
                       for key, table, where in (("owner", "library_artists", ""),
                                                 ("edition", "library_editions", "WHERE album!=''")))
            expected = db.execute("""SELECT count(*),coalesce(sum(bytes),0),
                coalesce(sum(lyrics NOT IN ('absent','instrumental','inconnu')),0),
                coalesce(sum(lrc IN ('local','intégré','externe','généré')),0),coalesce(sum(kara='prêt'),0)
                FROM songs""").fetchone()
            saved = db.execute("SELECT total,bytes,lyrics,lrc,ready FROM library_stats").fetchone()
            return memberships and tuple(saved) == tuple(expected)

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

    def notify(self, paths, mask=MEDIA, *, proofs=None, now=None, db=None):
        # The producer supplies only final library paths. The consumer checks
        # the exact disk version before using an optional publication receipt.
        paths = [str(path) for value in dict.fromkeys(paths)
                 if index_path(self.root, path := Path(value))]
        if not paths:
            return
        if db is None:
            with self.db(wait=.25) as opened:
                self.notify(paths, mask, proofs=proofs, now=now, db=opened)
            return
        now = time() if now is None else now
        serial = uuid4().hex
        proofs = {} if proofs is None else proofs
        # A newer audio proof replaces the full tag set. Text receipts are
        # merged separately and each one still has to match both disk versions.
        db.executemany("""
            INSERT INTO index_events VALUES(?,?,?,?,?,0,0,?)
            ON CONFLICT(path) DO UPDATE SET mask=index_events.mask | excluded.mask,
                serial=excluded.serial,
                due=min(excluded.due, index_events.first + ?), attempts=0,
                combined=index_events.combined+1,
                proof=CASE WHEN json_type(excluded.proof,'$.token')='text' THEN
                    json_set(excluded.proof,'$.references',json_patch(
                        coalesce(json_extract(index_events.proof,'$.references'),'{}'),
                        coalesce(json_extract(excluded.proof,'$.references'),'{}')))
                    ELSE coalesce(json_patch(index_events.proof,excluded.proof),excluded.proof,index_events.proof) END
        """, ((path, mask, serial, proofs.get(path, {}).get("published", now),
               now + (REMOVAL_DELAY if mask & REMOVE else EVENT_DELAY),
               document(proofs[path]) if path in proofs else None, EVENT_MAX_DELAY) for path in paths))

    def events(self, now, *, size=None, light=False):
        with self.db(wait=.15) as db:
            backlog = db.execute("SELECT count(*) FROM index_events").fetchone()[0]
            if size is None:
                size = 128 if backlog >= 4 * EVENT_BATCH and not light else EVENT_BATCH
            # Select the ready window in index order, then postpone removals
            # within it so that a destination can first inherit a moved ID.
            due = "due<=?"
            if light:
                due += f" AND (proof IS NOT NULL OR mask & {MEDIA}=0)"
            elif backlog >= size:
                due += f" OR (attempts=0 AND mask & {REMOVE}=0)"
            rows = db.execute(f"SELECT * FROM index_events WHERE {due} ORDER BY due,path LIMIT ?",
                              (now, size)).fetchall()
        return backlog, sorted(rows, key=lambda row: bool(row["mask"] & REMOVE))

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

    def work_due(self, kinds, now, *, local_reference=False, manual_only=False):
        if not kinds:
            return False
        placeholders = ",".join("?" for _ in kinds)
        scope, arguments = task_scope(local_reference)
        with self.db(wait=.15) as db:
            return db.execute(f"""SELECT 1 {scope} candidate.kind IN ({placeholders}) AND candidate.due<=?
                                  AND candidate.state IN ('queued','suspended','temporary')
                                  AND (NOT ? OR json_extract(candidate.payload,'$.manual') IS NOT NULL) LIMIT 1""",
                              (*arguments, *kinds, now, manual_only)).fetchone() is not None

    def night_lrc_due(self, db):
        return db.execute(f"""SELECT min(candidate.due) FROM tasks AS candidate
                               WHERE {NIGHT_LRC_STATE} {NIGHT_LRC_TRACK}""").fetchone()[0]

    def night_lrc_status(self, *, now=None):
        from _parole_sources import night_lrc_providers

        now = time() if now is None else now
        current = datetime.fromtimestamp(now)
        noon = current.replace(hour=12, minute=0, second=0, microsecond=0)
        if noon > current:
            noon -= timedelta(days=1)
        with self.db(wait=.15) as db:
            total, missing = db.execute(f"""SELECT count(*), coalesce(sum({NIGHT_LRC_MISSING}),0)
                                          FROM songs s""").fetchone()
            retrieved, = db.execute("""SELECT coalesce(sum(CAST(value AS INTEGER)),0) FROM reviews
                                      WHERE kind='activity_yield' AND state='night_lrc' AND checked>=?""",
                                   (noon.timestamp(),)).fetchone()
            due = self.night_lrc_due(db)
            providers = night_lrc_providers(self, db=db, now=now)
        ready = [row["id"] for row in providers if row["availability"] == "ready"]
        waiting = [(row["id"], row["next_available"]) for row in providers if row["next_available"]]
        source_due = now if ready else min((stamp for _, stamp in waiting), default=None)
        resume = max(due, source_due) if due is not None and source_due is not None else None
        return dict(total=total, missing=missing, present=total - missing, retrieved=retrieved,
                    ready=ready, waiting=waiting, resume=resume)

    @staticmethod
    def read_closed_report(db):
        row = db.execute("SELECT value FROM settings WHERE key='night_lid_report'").fetchone()
        return json.loads(row[0]) if row else None

    @staticmethod
    def write_closed_report(db, report):
        db.execute("INSERT OR REPLACE INTO settings VALUES('night_lid_report',?)", (document(report),))

    def closed_progress(self):
        with self.db(wait=.15) as db:
            return self.read_closed_report(db)

    def start_closed_report(self):
        since = time()
        with self.db(wait=.15) as db:
            self.write_closed_report(db, dict(started=since, ended=None, reason="", completed={}, deferred=0, interrupted=0))
        return since

    def end_closed_report(self, since, reason):
        with self.db(wait=.15) as db:
            db.execute("BEGIN IMMEDIATE")
            report = self.read_closed_report(db)
            if report and report["started"] == since and report["ended"] is None:
                report.update(ended=time(), reason=reason)
                self.write_closed_report(db, report)
                return sum(report["completed"].values())
        return None

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
            closed = self.read_closed_report(db)
            interrupted = False
            if closed and closed["ended"] is None:
                # The closed window only admits LRC searches. Desktop import
                # claims can also be recovered without owning this report.
                interrupted = db.execute("SELECT 1 FROM tasks WHERE kind='lrc' AND state='running' LIMIT 1").fetchone() is not None
                closed.update(ended=time(), reason="redémarrage de la maintenance")
                closed["interrupted"] += int(interrupted)
                self.write_closed_report(db, closed)
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

    def claim(self, kinds, now, *, bucket=None, limit=0, high=False, codex_auto=True, gap=0,
              avoid=(), local_reference=False, manual_only=False, night_lrc=False):
        if not kinds:
            return None
        placeholders = ",".join("?" for _ in kinds)
        excluded = ",".join("?" for _ in avoid)
        scope, arguments = task_scope(local_reference)
        # The import lane and classical network lane share the claim
        # transaction. A pending batch owns its tracks until this pass ends.
        lyrics_guard = """
            AND (candidate.kind!='import_lyrics' OR NOT EXISTS (
                SELECT 1 FROM json_each(candidate.payload,'$.paths') j
                JOIN songs s ON s.path=j.value JOIN tasks a ON a.subject=s.id
                WHERE a.kind IN ('lyrics','lrc') AND a.state='running'))
            AND (candidate.kind NOT IN ('lyrics','lrc') OR NOT EXISTS (
                SELECT 1 FROM songs s JOIN tasks a ON a.kind='import_lyrics'
                JOIN json_each(a.payload,'$.paths') j ON j.value=s.path
                WHERE s.id=candidate.subject AND (a.state='running' {pending_imports})))
        """ if {"lyrics", "lrc", "import_lyrics"}.intersection(kinds) else ""
        pending_imports = ("" if night_lrc else
                           "OR (a.state IN ('queued','suspended','temporary') AND a.due<=candidate.due)")
        lyrics_guard = lyrics_guard.format(pending_imports=pending_imports)
        state_guard = NIGHT_LRC_STATE if night_lrc else "candidate.state IN ('queued','suspended','temporary')"
        track_guard = NIGHT_LRC_TRACK if night_lrc else ""
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            day = date.fromtimestamp(now).isoformat()
            if bucket and not self.can_reserve(db, bucket, day, now, limit, gap):
                return None
            task = db.execute(f"""
                UPDATE tasks SET state='running', updated=?, attempts=attempts+1
                WHERE rowid=(SELECT candidate.rowid {scope} candidate.kind IN ({placeholders})
                    AND candidate.subject NOT IN ({excluded})
                    AND {state_guard} AND candidate.due<=?
                    {track_guard}
                    AND (NOT ? OR candidate.priority<=15)
                    AND (NOT ? OR json_extract(candidate.payload,'$.manual') IS NOT NULL)
                    {lyrics_guard}
                    AND (? OR candidate.kind!='codex' OR json_extract(candidate.payload,'$.manual')=1
                         OR json_extract(candidate.payload,'$.kind') IN ('adapter','import_patch'))
                    ORDER BY max(10,candidate.priority-cast((?-candidate.due)/86400 AS INTEGER)),
                             candidate.due,candidate.subject LIMIT 1)
                RETURNING *
            """, (now, *arguments, *kinds, *avoid, now, high, manual_only, codex_auto, now)).fetchone()
            if bucket and task is not None:
                self.reserve(db, bucket, day, now, gap)
            return task

    def finish(self, task, state, detail="", delay=0, *, closed=None, refund_attempt=False,
               network_found=False, activity=()):
        now = time()
        with self.db() as db:
            changed = db.execute("""UPDATE tasks SET state=?, detail=?, updated=?, due=?, attempts=attempts-?
                          WHERE kind=? AND subject=? AND state='running' AND revision=?""",
                                 (state, detail[:300], now, now + delay, refund_attempt,
                                  task["kind"], task["subject"], task["revision"])).rowcount
            published = state == "done" and network_found and task["kind"] in ("lyrics", "lrc")
            if changed and published:
                # Count an actual external publication once at queue
                # settlement. Provider hits and cached attempts are separate.
                minute = int(now) // 60 * 60
                db.execute("""INSERT INTO reviews VALUES('network_yield',?,?,?, '1')
                              ON CONFLICT(kind,subject) DO UPDATE SET value=CAST(value AS INTEGER)+1""",
                           (f"{minute}:{task['kind']}", task["kind"], minute))
                db.execute("DELETE FROM reviews WHERE kind='network_yield' AND checked<?", (minute - 48 * 3600,))
            if changed and activity:
                self.record_activity(db, now, activity)
            if changed and closed is not None:
                since, outcome = closed
                report = self.read_closed_report(db)
                # Count only a settled claim from this closure, in the same
                # transaction as the queue transition. An interruption can
                # settle after the owner has released the lid inhibitor.
                if report and report["started"] == since:
                    recorded = (*activity, f"network_{task['kind']}") if published else activity
                    if recorded:
                        counts = report.setdefault("activity", {})
                        for name in recorded:
                            counts[name] = counts.get(name, 0) + 1
                    if outcome == "completed" and report["ended"] is None:
                        kind = task["kind"]
                        report["completed"][kind] = report["completed"].get(kind, 0) + 1
                    elif outcome != "completed":
                        report[outcome] += 1
                    self.write_closed_report(db, report)

    def network_yield(self, *, now=None):
        end = int(time() if now is None else now) // 60 * 60
        with self.db(wait=.15) as db:
            return dict(db.execute("""SELECT state,sum(CAST(value AS INTEGER)) FROM reviews
                                      WHERE kind='network_yield' AND checked>=? AND checked<?
                                      GROUP BY state""", (end - 3600, end)))

    @staticmethod
    def record_activity(db, now, activity):
        # Minute buckets are ordered by the existing reviews primary key.
        # The caller owns the same transaction as the queue or validation receipt.
        minute = int(now) // 60 * 60
        db.executemany("""INSERT INTO reviews VALUES('activity_yield',?,?,?,'1')
                          ON CONFLICT(kind,subject) DO UPDATE SET value=CAST(value AS INTEGER)+1""",
                       ((f"{minute:012d}:{name}", name, minute) for name in activity))
        db.execute("DELETE FROM reviews WHERE kind='activity_yield' AND subject<?",
                   (f"{minute - 48 * 3600:012d}:",))

    def activity_progress(self, *, now=None):
        end = int(time() if now is None else now) // 60 * 60
        since_hour, since_day = end - 3600, end - 24 * 3600
        with self.db(wait=.15) as db:
            rows = db.execute("""SELECT state,
                                     sum(CASE WHEN checked>=? THEN CAST(value AS INTEGER) ELSE 0 END),
                                     sum(CAST(value AS INTEGER))
                                 FROM reviews WHERE kind='activity_yield' AND subject>=? AND subject<?
                                 GROUP BY state""", (since_hour, f"{since_day:012d}:", f"{end:012d}:"))
            hour, day = {}, {}
            for label, last_hour, last_day in rows:
                hour[label], day[label] = last_hour, last_day
            # Read the historical external publications without backfilling or
            # counting the same event in both histories.
            rows = db.execute("""SELECT state,
                                     sum(CASE WHEN checked>=? THEN CAST(value AS INTEGER) ELSE 0 END),
                                     sum(CAST(value AS INTEGER))
                                 FROM reviews WHERE kind='network_yield' AND checked>=? AND checked<?
                                 GROUP BY state""", (since_hour, since_day, end))
            for label, last_hour, last_day in rows:
                hour[f"network_{label}"], day[f"network_{label}"] = last_hour, last_day
            drafts = db.execute("""SELECT count(*) FROM tasks AS t JOIN songs AS s ON s.id=t.subject
                                  WHERE t.kind='transcription' AND t.state='review' AND t.detail='brouillon à vérifier'
                                  AND (s.lyrics='absent' OR s.lrc='absent')""").fetchone()[0]
        return hour, day, drafts

    def record_validation(self, identity, suffix, audio, sidecar, *, draft, path=None):
        now = time()
        value = document({"audio": audio, "sidecar": sidecar,
                          "source": "Turbo, validé manuellement" if draft else "Correction manuelle"})
        with self.db() as db:
            changed = db.execute("""INSERT INTO reviews VALUES('transcription_validation',?,'validated',?,?)
                         ON CONFLICT(kind,subject) DO UPDATE SET checked=excluded.checked,value=excluded.value
                         WHERE value<>excluded.value""", (identity + suffix, now, value)).rowcount
            if changed and draft:
                self.record_activity(db, now, ("validated_lrc" if suffix == ".lrc" else "validated_lyrics",))
            if path is not None:
                self.notify([path], SIDECAR, db=db)
                if changed:
                    queue_lexicon(db, identity, now)

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

    def update(self, identity, *, expected=None, lexicon=False, **fields):
        assert set(fields) <= {"lyrics", "lrc", "kara", "info", "cache", "audio", "side", "previous", "checked"}
        with self.db() as db:
            match = " AND token=? AND checked=?" if expected is not None else ""
            args = (expected["token"], expected["checked"]) if expected is not None else ()
            changed = db.execute(f"UPDATE songs SET {','.join(name+'=?' for name in fields)} WHERE id=?{match}",
                                 (*fields.values(), identity, *args)).rowcount
            if changed and lexicon:
                queue_lexicon(db, identity, time())

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
def collection_reader(database, root, *, targeted=False):
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
                   "musicbrainz_album_id musicbrainz_album_artist_id musicbrainz_recording_id genre").split()
        selected = ",".join(c if c in available else f"NULL AS {c}" for c in columns)
        where = " WHERE unavailable=0" if "unavailable" in available else ""
        if targeted:
            def targeted_batch(paths):
                # Strawberry stores either a fully escaped file URL or Qt's
                # form with safe path punctuation left unescaped. idx_url
                # can serve both without walking the entire collection.
                variants = {}
                for path in paths:
                    for value in {path.as_uri()[7:], quote(str(path), safe="/!$&'()*+,;=:@")}:
                        variants["file://" + value] = str(path)
                        variants["file://localhost" + value] = str(path)
                if not variants:
                    return {}
                marks = ",".join("?" for _ in variants)
                condition = (" AND " if where else " WHERE ") + f"url IN ({marks})"
                rows = {}
                # Keep the exact same row layout as the full reader. The
                # stored catalogue revision includes the selected rowid.
                for row in db.execute("SELECT rowid," + selected + " FROM songs" + where + condition + " ORDER BY rowid",
                                      tuple(variants)):
                    rows.setdefault(variants[row["url"]], row)
                return rows
            yield targeted_batch
            return
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


def sidecar(path, suffix, limit=MAX_LRC_BYTES, *, preserve_bom=False):
    candidate = path.with_suffix(suffix)
    try:
        info = candidate.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            return "invalid", ""
        with os.fdopen(os.open(candidate, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as stream:
            before = os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                return "invalid", ""
            raw = stream.read(limit + 1)
            if len(raw) > limit:
                return "invalid", ""
            version = os.fstat(stream.fileno())
            if revision(before) != revision(version):
                return "invalid", ""
            return revision(version), raw.decode("utf-8" if preserve_bom else "utf-8-sig")
    except FileNotFoundError:
        return "", ""
    except (OSError, UnicodeError, ValueError):
        return "invalid", ""


def revision(info):
    return document([info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns])


def sidecar_token(path):
    try:
        info = path.lstat()
        return revision(info) if stat.S_ISREG(info.st_mode) and info.st_size <= MAX_LRC_BYTES else "invalid"
    except FileNotFoundError:
        return ""
    except OSError:
        return "invalid"


def reference_proof(path, suffix, audio, provider, *, external=False):
    # The caller captures the final linked sidecar while its audio is pinned
    # by the mutation lease. The proof contains versions and attribution only.
    return {"references": {suffix: {"audio": document(audio), "sidecar": sidecar_token(path.with_suffix(suffix)),
                                     "provider": provider, "external": external}}}


def local_state(path, embedded, duration, old, info, stable, measure, *, exact_duration=None):
    # Versions are kept separately for the two sidecars. A new LRC can reuse
    # the existing TXT result, and a duration change can reuse parsed timing.
    with measure("sidecar_stat"):
        tokens = [sidecar_token(path.with_suffix(suffix)) for suffix in (".lrc", ".txt")]
    previous = json.loads(old["side"]) if old and old["side"] else [None, None]
    if stable and tokens == previous and "invalid" not in tokens:
        return old["side"], old["lyrics"], old["lrc"]
    saved = info.get("side_state", [None, None])
    evidence = []
    for i, suffix in enumerate((".lrc", ".txt")):
        if not tokens[i]:
            evidence.append(-1 if i == 0 else False)
        elif tokens[i] == previous[i] and tokens[i] != "invalid" and saved[i] is not None:
            evidence.append(saved[i])
        else:
            with measure("sidecar_read"):
                tokens[i], raw = sidecar(path, suffix)
            if i == 0:
                with measure("lrc"):
                    evidence.append(max((stamp for stamp, text in parse_lrc(raw) if text.strip()), default=-1))
            else:
                evidence.append(bool(raw.strip()))
    info["side_state"] = evidence
    end, plain = evidence
    embedded_end = -1
    if not tokens[0] and embedded:
        with measure("lrc"):
            rows = parse_lrc(embedded)
        if rows:
            embedded_end = rows[-1][0]
    # Near the acceptance boundary, encoder padding and presentation timing
    # can change the verdict. Keep FFprobe as the reference for that decision.
    candidate_end = end if tokens[0] else embedded_end
    if exact_duration and duration and abs(candidate_end - duration - 3) <= .15:
        duration = exact_duration()
    if end >= 0 and duration is not None and (not duration or 0 < end <= duration + 3):
        lrc = "local"
    elif tokens[0]:
        lrc = "à vérifier"
    else:
        lrc = ("à vérifier" if duration is None else "intégré"
               if embedded_end >= 0 and (not duration or 0 < embedded_end <= duration + 3) else "absent")
    lyrics = "fichier texte" if plain else "Strawberry" if embedded.strip() else "LRC" if lrc == "local" else "absent"
    # Reference provenance belongs to its exact file and audio versions.
    if old and stable:
        if tokens[0] == previous[0] and lrc == "local" and old["lrc"] in ("externe", "généré"):
            lrc = old["lrc"]
        if tokens == previous and lyrics == "absent" and old["lyrics"] == "instrumental":
            lyrics = old["lyrics"]
    return document(tokens), lyrics, lrc


def audio_paths(root, onerror=None):
    for folder, children, files in os.walk(root, onerror=onerror):
        children[:] = sorted(name for name in children if not name.startswith(".")
                             and (Path(folder) != root or name not in SKIP_TOP))
        for name in sorted(files):
            if not name.startswith(".") and Path(name).suffix.lower() in AUDIO:
                yield Path(folder) / name


def probe_track(path):
    return probe(path, include_duration=True, include_codec=True)


def index_path(root, path):
    if not path.is_relative_to(root) or path.suffix.lower() not in AUDIO:
        return False
    try:
        str(path).encode("utf-8")
    except UnicodeError:
        return False
    parts = path.relative_to(root).parts
    return bool(parts and parts[0] not in SKIP_TOP and not any(p.startswith(".") for p in parts))


def path_rows(db, paths):
    values = tuple(map(str, paths))
    if not values:
        return {}
    marks = ",".join("?" for _ in values)
    return {row["path"]: row for row in db.execute(f"SELECT * FROM songs WHERE path IN ({marks})", values)}


def missing_on_device(path, device):
    # A vanished submount exposes a different device on the nearest existing
    # ancestor. Errors other than absence must leave the historical row intact.
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        return candidate != path and stat.S_ISDIR(info.st_mode) and info.st_dev == device
    return False


def cover_path(path):
    return path.stem.casefold() in COVER_NAMES and path.suffix.casefold() in COVER_TYPES


def affected_tracks(index, folder):
    # A lexical range on the unique path index keeps an album cover
    # update independent of the total size of the library.
    prefix = str(folder) + "/"
    with index.db(wait=.15) as db:
        return [path for row in db.execute("SELECT path FROM songs WHERE path>=? AND path<?",
                                          (prefix, prefix[:-1] + "0")) if (path := Path(row[0])).parent == folder]


def inventory(index, collection, inspect_cache, stop, progress, *, limit=None, paths=None,
              events=None, reader=None, plan_events=None, allow_probe=True):
    root = index.root
    counts = dict(scanned=0, changed=0, missing=0, errors=0, unchanged=0, probes=0,
                  taglib=0, reused=0, strawberry=0, transactions=0, receipts=0, delivered=0, combined=0, lag=0.)
    fast_reader = taglib_helper() if reader is None else None
    reader = reader or probe_track
    events = {row["path"]: row for row in events} if events is not None else {}
    if events:
        paths = tuple(map(Path, events))
    phases = defaultdict(float)
    calls = defaultdict(int)

    @lru_cache(maxsize=64)
    def cover_revision(folder):
        entries = []
        for path in folder.iterdir():
            if cover_path(path):
                info = path.lstat()
                if stat.S_ISREG(info.st_mode):
                    entries.append([path.name, revision(info)])
        return sorted(entries)

    def lookup_media(path):
        event = events.get(str(path))
        return event is None or event["mask"] & MEDIA and not event["proof"]

    @contextmanager
    def measure(name):
        started = perf_counter()
        try:
            yield
        finally:
            phases[name] += perf_counter() - started
            calls[name] += 1

    def root_identity():
        st = root.lstat()
        if not stat.S_ISDIR(st.st_mode):
            raise OSError("Bibliothèque indisponible")
        return st.st_dev, st.st_ino

    identity = root_identity()
    # Keep a narrow snapshot for absence detection. Unchanged songs do not
    # need a generation write. The conditional delete protects newer rows.
    with measure("sqlite_read"), index.db() as db:
        saved_root = db.execute("SELECT dev,inode FROM inventory_root WHERE id=1").fetchone()
        if saved_root is not None and tuple(saved_root) != identity:
            raise OSError("Stockage de la bibliothèque remplacé ou indisponible")
        missing = ({r["path"]: (r["id"], r["token"], r["checked"])
                    for r in db.execute("SELECT id,path,token,checked FROM songs")}
                   if paths is None and limit is None else {})
    had_rows = bool(missing)
    generation = uuid4().hex
    group_rules = album_groups(index.path)
    pending = []
    removed = []
    settled, retry = set(), set()
    now = time()
    walk_failed = False

    def error(_):
        nonlocal walk_failed
        walk_failed = True
        counts["errors"] += 1

    def flush():
        if not (pending or removed or settled or retry):
            return
        if root_identity() != identity:
            raise OSError("Stockage de la bibliothèque changé pendant l’inventaire")
        with measure("sqlite_transaction"), index.db() as db:
            with measure("sqlite_lock"):
                db.execute("BEGIN IMMEDIATE")
            counts["transactions"] += 1
            planned = []
            for song in pending:
                written = db.execute("""
                    INSERT INTO songs VALUES(
                      :id,:path,:token,:dev,:inode,:bytes,:side,:cache,:previous,:title,:artist,:owner,
                      :album,:edition,:duration,:disc,:track,:year,:artist_id,:release_id,:plays,
                      :lyrics,:lrc,:kara,:search,:seen,:added,:checked,:info,:audio
                    ) ON CONFLICT(id) DO UPDATE SET
                      path=excluded.path,token=excluded.token,dev=excluded.dev,inode=excluded.inode,
                      bytes=excluded.bytes,side=excluded.side,cache=excluded.cache,previous=excluded.previous,
                      title=excluded.title,artist=excluded.artist,owner=excluded.owner,album=excluded.album,
                      edition=excluded.edition,duration=excluded.duration,disc=excluded.disc,track=excluded.track,
                      year=excluded.year,artist_id=excluded.artist_id,release_id=excluded.release_id,plays=excluded.plays,
                      lyrics=excluded.lyrics,lrc=excluded.lrc,kara=excluded.kara,search=excluded.search,
                      seen=excluded.seen,checked=excluded.checked,info=excluded.info,audio=excluded.audio
                    WHERE songs.token=:old_token AND songs.checked=:old_checked
                """, song).rowcount
                counts["changed"] += written
                if written:
                    planned.append(song["path"])
                    if song["lexicon"] or song["lexicon_dirty"] and db.execute(
                            "SELECT 1 FROM tasks WHERE kind=? AND subject=?", (LEXICON, song["id"])).fetchone():
                        queue_lexicon(db, song["id"], now)
                elif song["path"] in events:
                    retry.add(song["path"])
                # Keep the canonical edition and recording IDs when a known
                # inode moves. The path is changed in the song transaction.
                if written and (song["old_token"] is None or song["canonical_previous"] is not None):
                    db.execute("""
                        UPDATE canonical_references SET path=:path,song=:id
                        WHERE (path=:path AND song='') OR (path=:canonical_previous AND song IN ('',:id))
                    """, song)
            for song in removed:
                deleted = db.execute("DELETE FROM songs WHERE id=? AND path=? AND token=? AND checked=?",
                                     (song["id"], song["path"], song["token"], song["checked"])).rowcount
                counts["missing"] += deleted
                if deleted:
                    db.execute("UPDATE tasks SET state='excluded' WHERE subject=? AND state!='running'", (song["id"],))
                    forget_lexicon(db, song["id"])
                elif db.execute("SELECT 1 FROM songs WHERE path=?", (song["path"],)).fetchone():
                    retry.add(song["path"])
            if planned and plan_events:
                with measure("plan"):
                    plan_events(db, planned)
            # Acknowledgement and visible changes share one commit. An event
            # merged during inspection has a different serial and survives.
            for pathname in sorted(settled - retry):
                event = events[pathname]
                if db.execute("DELETE FROM index_events WHERE path=? AND serial=?",
                              (pathname, event["serial"])).rowcount:
                    counts["delivered"] += 1
                    counts["combined"] += event["combined"]
                    counts["lag"] += max(0., time() - event["first"])
            db.executemany("UPDATE index_events SET attempts=attempts+1,due=? WHERE path=? AND serial=?",
                           ((time() + min(1800, 2 ** min(10, events[name]["attempts"] + 2)), name,
                             events[name]["serial"]) for name in sorted(retry)))
        pending.clear()
        removed.clear()
        settled.clear()
        retry.clear()

    reader_context = (collection_reader(collection, root, targeted=paths is not None)
                      if not events or any(lookup_media(path) for path in paths) else nullcontext(lambda paths: {}))
    started = perf_counter()
    with reader_context as read_batch, index.db() as db:
        phases["strawberry"] += perf_counter() - started
        candidates = iter(audio_paths(root, error) if paths is None else dict.fromkeys(paths))
        while not stop() and (limit is None or counts["scanned"] < limit):
            size = 128 if limit is None else min(128, limit - counts["scanned"])
            with measure("walk"):
                window = tuple(islice(candidates, size))
                batch = tuple(path for path in window if index_path(root, path))
            if not window:
                break
            if not batch:
                continue
            with measure("sqlite_read"):
                previous = path_rows(db, batch)
            lookup = {path for path in batch if lookup_media(path)}
            with measure("strawberry"):
                catalogue = read_batch(lookup) if lookup else {}
            for path in batch:
                if stop():
                    break
                pathname = str(path)
                missing.pop(pathname, None)
                old = previous.get(pathname)
                event = events.get(pathname)
                mask = event["mask"] if event else 0
                try:
                    with measure("file_stat"):
                        st = path.lstat()
                        if not stat.S_ISREG(st.st_mode) or paths is not None and path.resolve() != path:
                            continue
                        token = revision(st)
                    canonical_previous = None
                    if old is None:
                        with measure("sqlite_read"):
                            matches = db.execute("SELECT * FROM songs WHERE dev=? AND inode=? LIMIT 2",
                                                 (st.st_dev, st.st_ino)).fetchall()
                        if len(matches) == 1:
                            known = matches[0]
                            if (json.loads(known["token"])[2:4] == [st.st_size, st.st_mtime_ns]
                                    and not Path(known["path"]).exists()):
                                old = known
                                canonical_previous = old["path"]
                    proof = json.loads(event["proof"]) if event and event["proof"] else {}
                    proof_tags = proof.get("tags") if proof.get("token") == token else None
                    strawberry = catalogue.get(pathname)
                    fresh = bool(strawberry and (st.st_size, int(st.st_mtime)) ==
                                 (strawberry["filesize"], strawberry["mtime"]))
                    values = tuple(strawberry) if strawberry else ()
                    if strawberry and not strawberry["genre"]:
                        values = values[:-1]
                    catalogue_revision = encoded_key(values)
                    info = json.loads(old["info"]) if old else {}
                    if event and path not in lookup and old:
                        catalogue_revision = info.get("catalogue", catalogue_revision)
                    same_audio = old is not None and old["token"] == token
                    same_path = old is not None and old["path"] == str(path)
                    same_catalogue = catalogue_revision == info.get("catalogue")
                    reuse = same_audio and not info.get("probe_error") and (
                        same_catalogue or not fresh or info.get("file_tags") or info.get("tag_reader") == "taglib")
                    with measure("metadata"):
                        if reuse:
                            tags = info["tags"]
                            embedded = info.get("plain", "")
                            duration = old["duration"]
                            counts["reused"] += 1
                        else:
                            old_stamp = json.loads(old["token"]) if old is not None else None
                            same_coarse_stamp = old_stamp is not None and (old_stamp[2], old_stamp[3] // 10**9) == (
                                st.st_size, st.st_mtime_ns // 10**9)
                            if proof_tags is not None:
                                tags = proof_tags
                                counts["receipts"] += 1
                                info["file_tags"] = True
                                info.pop("tag_reader", None)
                                info.pop("probe_error", None)
                            elif fresh and (old is None or same_audio or not same_coarse_stamp):
                                tags = dict(strawberry)
                                for dest, source in (("albumid", "album"), ("artistid", "album_artist"),
                                                     ("recordingid", "recording")):
                                    tags[dest] = strawberry[f"musicbrainz_{source}_id"]
                                counts["strawberry"] += 1
                                info.pop("file_tags", None)
                                info.pop("tag_reader", None)
                                info.pop("probe_error", None)
                            else:
                                if not allow_probe:
                                    retry.add(pathname)
                                    continue
                                attempt = None
                                if fast_reader is not None:
                                    with measure("taglib"):
                                        attempt = taglib_read(path, fast_reader)
                                try:
                                    if attempt is not None:
                                        tags = attempt.tags
                                        counts["taglib"] += 1
                                        info["tag_reader"] = "taglib"
                                        # The enrichment writer needs the exact FFprobe
                                        # spelling of all tags before comparing a rewrite.
                                        info.pop("file_tags", None)
                                    else:
                                        with measure("ffprobe"):
                                            counts["probes"] += 1
                                            tags, _ = reader(path)
                                        info["file_tags"] = True
                                        info.pop("tag_reader", None)
                                except (OSError, ValueError, subprocess.SubprocessError):
                                    tags = info.get("tags", {}) if old else {}
                                    info["probe_error"] = True
                                    counts["errors"] += 1
                                    if event:
                                        retry.add(pathname)
                                else:
                                    info.pop("probe_error", None)
                            duration = tags.get("duration_seconds") or (
                                strawberry["length"] / 1e9 if fresh and (strawberry["length"] or 0) > 0 else 0)
                            embedded = next((tags.get(k) for k in LYRIC_KEYS if tags.get(k)), "") or (
                                (strawberry["lyrics"] or "") if fresh else "")
                            if len(embedded.encode()) > MAX_LRC_BYTES:
                                embedded = ""
                    stable = same_audio and embedded == info.get("plain", "") and duration == old["duration"]
                    timing_pending = bool(info.get("timing_pending"))
                    if stable and event and not mask & (MEDIA | SIDECAR):
                        side, lyrics, lrc = old["side"], old["lyrics"], old["lrc"]
                    else:
                        def exact_duration():
                            if allow_probe:
                                try:
                                    with measure("ffprobe"):
                                        counts["probes"] += 1
                                        if exact := reader(path)[0].get("duration_seconds"):
                                            return exact
                                except (OSError, ValueError, subprocess.SubprocessError):
                                    counts["errors"] += 1
                            if event:
                                retry.add(pathname)
                            info["timing_pending"] = True
                            return None

                        verify = exact_duration if info.get("tag_reader") == "taglib" else None
                        side, lyrics, lrc = local_state(path, embedded, duration, old, info,
                                                       stable and not info.pop("timing_pending", False), measure,
                                                       exact_duration=verify)
                    if stable and event and side == old["side"] and not mask & PREPARATION:
                        cache, kara, report = old["cache"], old["kara"], info.get("report", {})
                    else:
                        with measure("cache"):
                            cache, kara, report = inspect_cache(path, old, token, side)
                    side_versions = json.loads(side) if side else [None, None]
                    references = proof.get("references", {})
                    provenance = {}
                    for offset, (suffix, field) in enumerate(((".lrc", "lrc_provider"), (".txt", "text_provider"))):
                        fact = references.get(suffix, {})
                        if (side_versions[offset] not in ("", "invalid", None)
                                and fact.get("audio") == token and fact.get("sidecar") == side_versions[offset]
                                and isinstance(provider := fact.get("provider"), str) and provider):
                            provenance[field] = provider
                            if suffix == ".lrc" and lrc == "local" and fact.get("external") is True:
                                lrc = "externe"
                    if proof_tags is not None and isinstance(provider := proof.get("lyrics_provider"), str) and provider:
                        provenance["lyrics_provider"] = provider
                    same_streams = same_audio or (proof_tags is not None and old is not None and
                                                 proof.get("same_streams_as") == old["token"])
                    audio = old["audio"] if same_streams else ""
                    pcm = proof.get("pcm") if proof_tags is not None else None
                    if isinstance(pcm, str) and len(pcm) == 64 and all(c in "0123456789abcdef" for c in pcm):
                        audio = pcm
                    artwork_changed = False
                    if (event and mask & ARTWORK) or (not event and "artwork" in info):
                        with measure("artwork"):
                            artwork = cover_revision(path.parent)
                        artwork_changed = artwork != info.get("artwork")
                        info["artwork"] = artwork
                    counts["scanned"] += 1
                    plays = integer(strawberry["playcount"]) if strawberry else old["plays"] if old else 0
                    if (same_path and stable and same_catalogue and side == old["side"] and cache == old["cache"]
                            and kara == old["kara"] and lyrics == old["lyrics"] and lrc == old["lrc"]
                                and plays == old["plays"] and not artwork_changed and not info.get("probe_error")
                                and timing_pending == bool(info.get("timing_pending")) and audio == old["audio"]
                                and all(info.get(field) == value for field, value in provenance.items())):
                        counts["unchanged"] += 1
                        if event:
                            settled.add(pathname)
                        continue
                    if revision(path.lstat()) != token:
                        error("version audio modifiée pendant l’inventaire")
                        if event:
                            retry.add(pathname)
                        continue
                    if canonical_previous is not None:
                        missing.pop(canonical_previous, None)
                    title = tags.get("title") or path.stem
                    artist = tags.get("artist") or UNKNOWN_ARTIST
                    owner = tags.get("albumartist") or artist
                    album = tags.get("album") or ""
                    artist_id = tags.get("artistid") or tags.get("musicbrainzalbumartistid") or ""
                    owner = manual_album_artist(group_rules, tags) or owner
                    release_id = tags.get("albumid") or tags.get("musicbrainzalbumid") or ""
                    year = integer(str(tags.get("year") or tags.get("date") or "")[:4])
                    edition = encoded_key(owner, album, release_id or year, str(path.parent))
                    if not same_streams or embedded != info.get("plain", ""):
                        info.pop("lyrics_provider", None)
                    old_side = json.loads(old["side"]) if old and old["side"] else [None, None]
                    for offset, field in enumerate(("lrc_provider", "text_provider")):
                        if not same_streams or side_versions[offset] != old_side[offset]:
                            info.pop(field, None)
                    info.update(provenance)
                    info.update(tags={k: v for k, v in tags.items() if k not in LYRIC_KEYS},
                                plain=embedded, catalogue=catalogue_revision, report=report)
                    pending.append(dict(id=old["id"] if old else uuid4().hex, path=str(path), token=token,
                                        dev=st.st_dev, inode=st.st_ino, bytes=st.st_size, side=side, cache=cache,
                                        previous=(old["path"] if not same_path else old["previous"]) if old else None,
                                        title=title, artist=artist, owner=owner, album=album, edition=edition, duration=duration,
                                        disc=integer(tags.get("disc") or tags.get("discnumber")),
                                        track=integer(tags.get("track") or tags.get("tracknumber")), year=year,
                                        artist_id=artist_id, release_id=release_id, plays=plays, lyrics=lyrics, lrc=lrc,
                                        kara=kara, search=key(" ".join((title, artist, owner, album))), seen=generation,
                                        added=old["added"] if old else now, checked=now, info=document(info),
                                        audio=audio, canonical_previous=canonical_previous,
                                        lexicon=bool(event and mask & (MEDIA | SIDECAR) and (embedded or provenance)),
                                        lexicon_dirty=not event or bool(mask & (MEDIA | SIDECAR)),
                                        old_token=old["token"] if old else None, old_checked=old["checked"] if old else None))
                    if event:
                        settled.add(pathname)
                except FileNotFoundError:
                    if event and mask & REMOVE:
                        if old and missing_on_device(path, old["dev"]):
                            removed.append(old)
                            settled.add(pathname)
                        elif old is None:
                            settled.add(pathname)
                        else:
                            retry.add(pathname)
                    elif event:
                        retry.add(pathname)
                except (OSError, ValueError, UnicodeError) as exc:
                    error(exc)
                    if event:
                        retry.add(pathname)
            flush()
            progress(counts["scanned"])

    # A missing or replaced mount, interrupted walk or unreadable directory
    # is not proof of absence. A legacy index first needs a readable witness.
    if (paths is None and limit is None and not stop() and not walk_failed
            and root_identity() == identity and (saved_root or counts["scanned"] or not had_rows)):
        if saved_root is None:
            with measure("sqlite_transaction"), index.db() as db:
                db.execute("INSERT OR IGNORE INTO inventory_root VALUES(1,?,?)", identity)
                counts["transactions"] += 1
        remaining = iter(missing.items())
        while not stop() and (batch := tuple(islice(remaining, 128))):
            absent = []
            for pathname, (song_id, token, checked) in batch:
                try:
                    if missing_on_device(Path(pathname), json.loads(token)[0]):
                        absent.append((song_id, pathname, token, checked))
                except OSError as exc:
                    error(exc)
            if absent and root_identity() == identity:
                with measure("sqlite_transaction"), index.db() as db:
                    with measure("sqlite_lock"):
                        db.execute("BEGIN IMMEDIATE")
                    counts["transactions"] += 1
                    for row in absent:
                        if db.execute("DELETE FROM songs WHERE id=? AND path=? AND token=? AND checked=?", row).rowcount:
                            forget_lexicon(db, row[0])
                            counts["missing"] += 1
    return dict(counts, phase_seconds=dict(phases), phase_calls=dict(calls))
