"""Durable Parole queue, scoped to the canonical music library."""
from contextlib import contextmanager
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

from _kara_import_local import local_selection, local_url
from _kara_paths import STATE
from _musique_oz import SHARED_LIBRARY
from _parole_import_providers import archive_identifier, jamendo_identifier

DEFAULT_BATCHES = 4
MAX_BATCHES = 8
COMPLETE = {"success", "present", "duplicate"}
BUSY = ("queued", "running")


def import_key(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ("url", url)
    if value := archive_identifier(parsed):
        return ("archive", value)
    if match := jamendo_identifier(parsed):
        return ("jamendo_" + match[0], match[1])
    if parsed.hostname not in {"music.youtube.com", "www.youtube.com", "youtube.com", "youtu.be"}:
        return ("url", url)
    query = parse_qs(parsed.query)
    if query.get("list"):
        return ("playlist", query["list"][0])
    if parsed.path.startswith("/browse/"):
        return ("album", parsed.path.rstrip("/").split("/")[-1])
    if query.get("v"):
        return ("video", query["v"][0])
    if parsed.hostname == "youtu.be":
        return ("video", parsed.path.strip("/"))
    if parsed.path.startswith(("/shorts/", "/live/")):
        return ("video", parsed.path.split("/")[2])
    return ("url", url)


def digest(text):
    return sha256(text.encode()).hexdigest()


class ImportQueue:
    def __init__(self, state=STATE, library=SHARED_LIBRARY):
        self.directory = Path(state) / "kara" / "import-queue" / digest(str(library.resolve()))
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.database = self.directory / "queue.sqlite3"
        self.database.touch(mode=0o600, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS settings (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    paused INTEGER NOT NULL, batches INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS jobs (
                    seq INTEGER PRIMARY KEY, id TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL, option TEXT, targets TEXT NOT NULL, keys TEXT NOT NULL,
                    message TEXT NOT NULL, journal TEXT, attempt INTEGER NOT NULL DEFAULT 0,
                    refresh INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS jobs_state ON jobs(state, seq);
                CREATE TABLE IF NOT EXISTS outcomes (
                    job TEXT NOT NULL, url TEXT NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY (job, url), FOREIGN KEY (job) REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS import_terminal (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), attempt INTEGER NOT NULL, value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS import_recovery (
                    job TEXT PRIMARY KEY REFERENCES jobs(id), attempt INTEGER NOT NULL,
                    signature TEXT NOT NULL, phase TEXT NOT NULL, due REAL NOT NULL,
                    classic INTEGER NOT NULL DEFAULT 0, remedial INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS recovery_signature ON import_recovery(signature, phase, due);
                CREATE TABLE IF NOT EXISTS import_recovery_history (
                    job TEXT NOT NULL REFERENCES jobs(id), attempt INTEGER NOT NULL,
                    stamp REAL NOT NULL DEFAULT (unixepoch('subsec')), signature TEXT NOT NULL,
                    phase TEXT NOT NULL, message TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS maintenance_receipts (
                    id TEXT PRIMARY KEY, job TEXT NOT NULL REFERENCES jobs(id)
                );
                CREATE TABLE IF NOT EXISTS requests (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL
                );
            """)
            db.execute("INSERT OR IGNORE INTO settings VALUES (1, 0, ?)", (DEFAULT_BATCHES,))

    @contextmanager
    def connect(self, write=False):
        # SQLite's rollback journal and FULL sync make an acknowledged request
        # durable. Independent connections keep GTK, HTTP and workers thread-safe.
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA synchronous = FULL")
        try:
            with db:
                if write:
                    db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def settings(self):
        with self.connect() as db:
            row = db.execute("SELECT paused, batches FROM settings WHERE id=1").fetchone()
            return bool(row["paused"]), row["batches"]

    def configure(self, *, paused=None, batches=None):
        if paused is not None and type(paused) is not bool:
            raise ValueError("État de pause invalide.")
        if batches is not None and (type(batches) is not int or not 1 <= batches <= MAX_BATCHES):
            raise ValueError(f"Choisis entre 1 et {MAX_BATCHES} lots simultanés.")
        with self.connect(write=True) as db:
            db.execute("UPDATE settings SET paused=coalesce(?,paused), batches=coalesce(?,batches) WHERE id=1",
                       (paused, batches))

    def enqueue(self, targets, *, option=None, journal=None, request=None, db=None):
        if db is None:
            with self.connect(write=True) as transaction:
                return self.enqueue(targets, option=option, journal=journal, request=request, db=transaction)
        unique = {}
        for title, url in targets:
            if local_url(url):
                # Reject private edition links before persisting the selection.
                local_selection(url)
            unique.setdefault(import_key(url), (title, url))
        if not unique:
            raise ValueError("Aucun album à importer.")
        keys = set(unique) if option is None else set()
        current = db.execute("SELECT id, option, keys FROM jobs WHERE state IN ('queued', 'running')")
        identity = next((row["id"] for row in current
                         if (option is not None and row["option"] == option) or
                         (keys and keys <= set(map(tuple, json.loads(row["keys"]))))), None)
        if identity is None:
            identity = uuid4().hex
            db.execute("INSERT INTO jobs (id,state,option,targets,keys,message,journal) VALUES (?, 'queued', ?, ?, ?, ?, ?)",
                       (identity, option, json.dumps(list(unique.values())), json.dumps(sorted(keys)),
                        "En attente…", journal))
        if request is not None:
            request["task_id"] = identity
            self.save_request(request, db=db)
        return identity

    def enqueue_maintenance(self, receipt, title, url):
        # Deduplication against desktop/mobile uses the existing queue. A
        # committed receipt also survives completion, cancellation and restart.
        with self.connect(write=True) as db:
            row = db.execute("SELECT job FROM maintenance_receipts WHERE id=?", (receipt,)).fetchone()
            if row:
                return row[0]
            identity = self.enqueue([(title, url)], db=db)
            db.execute("INSERT INTO maintenance_receipts VALUES(?,?)", (receipt, identity))
            return identity

    @staticmethod
    def decode(row):
        return {**row, "targets": json.loads(row["targets"])}

    def job(self, identity):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (identity,)).fetchone()
            return self.decode(row) if row else None

    def rows(self):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM jobs WHERE state NOT IN ('done', 'cancelled') OR refresh>0 ORDER BY seq")
            return [self.decode(row) for row in rows]

    def history(self, before=None, limit=25):
        with self.connect() as db:
            rows = db.execute("""
                SELECT * FROM jobs WHERE state IN ('done', 'cancelled') AND refresh=0
                    AND (? IS NULL OR seq < ?) ORDER BY seq DESC LIMIT ?
            """, (before, before, limit))
            return [self.decode(row) for row in rows]

    def count(self):
        with self.connect() as db:
            return {row[0]: row[1] for row in db.execute("SELECT state,count(*) FROM jobs GROUP BY state")}

    def pending(self):
        with self.connect() as db:
            return bool(db.execute("SELECT 1 FROM jobs WHERE state IN ('queued','running') AND option IS NULL LIMIT 1")
                        .fetchone())

    def enrichment_groups(self):
        with self.connect() as db:
            # Historical outcomes predate enrichment receipts. Count only
            # receipts written by the asynchronous pipeline.
            return [json.loads(value) for value, in db.execute("""
                SELECT json_extract(payload,'$.enrichment') FROM outcomes
                WHERE json_type(payload,'$.enrichment')='object'
                AND json_extract(payload,'$.audio_complete')=1
            """)]

    def totals(self):
        with self.connect() as db:
            albums, tracks = set(), set()
            settled = 0
            for url, payload, state in db.execute(
                    "SELECT url,payload,state FROM outcomes JOIN jobs ON jobs.id=outcomes.job"):
                outcome = json.loads(payload)
                status = outcome["status"]
                if state != "cancelled" and (status in COMPLETE or
                        state not in BUSY and status in ("failure", "partial")):
                    settled += 1
                if outcome["status"] in ("success", "present"):
                    albums.add(outcome.get("identity") or import_key(url))
                tracks.update(outcome.get("tracks", ()))
            planned = db.execute("""
                SELECT coalesce(sum(json_array_length(targets)),0)
                FROM jobs WHERE option IS NULL AND state != 'cancelled'
            """).fetchone()[0]
        return len(albums), len(tracks), settled, planned

    def outcomes(self, identity):
        with self.connect() as db:
            return {url: json.loads(payload) for url, payload in
                    db.execute("SELECT url,payload FROM outcomes WHERE job=?", (identity,))}

    def set_journal(self, identity, path):
        with self.connect() as db:
            db.execute("UPDATE jobs SET journal=? WHERE id=?", (str(path), identity))

    def album(self, identity, outcome):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO outcomes VALUES (?, ?, ?)",
                       (identity, outcome["url"], json.dumps(outcome)))

    def transition(self, identity, previous, state, message, *, refresh=False):
        with self.connect() as db:
            return db.execute("""
                UPDATE jobs SET state=?,message=?,refresh=CASE WHEN ? THEN attempt+1 ELSE refresh END
                WHERE id=? AND state=?
            """, (state, message, refresh, identity, previous)).rowcount == 1

    def claim(self, identity):
        with self.connect(write=True) as db:
            paused, capacity = db.execute("SELECT paused,batches FROM settings WHERE id=1").fetchone()
            active = db.execute("SELECT count(*) FROM jobs WHERE state='running'").fetchone()[0]
            if paused or active >= capacity:
                return False
            return db.execute("""
                UPDATE jobs SET state='running',message='Import en cours',attempt=attempt+1
                WHERE id=? AND state='queued'
            """, (identity,)).rowcount == 1

    def finish(self, identity, event, message, terminal=None):
        with self.connect(write=True) as db:
            has_tracks = db.execute("""SELECT 1 FROM outcomes WHERE job=?
                                    AND json_array_length(json_extract(payload,'$.tracks'))>0 LIMIT 1""",
                                    (identity,)).fetchone()
            state = {"success": "done", "skip": "done", "cancelled": "cancelled",
                     "failure": "partial" if has_tracks else "failed"}[event]
            row = db.execute("""UPDATE jobs SET state=?,message=?,refresh=attempt+1
                                WHERE id=? AND state='running' RETURNING attempt""",
                             (state, message, identity)).fetchone()
            if row is not None and terminal is not None:
                db.execute("INSERT OR REPLACE INTO import_terminal VALUES(?,?,?)",
                           (identity, row[0], json.dumps(terminal, ensure_ascii=False)))

    def diagnosis_candidates(self, now, limit=20):
        with self.connect() as db:
            rows = db.execute("""
                SELECT j.*, t.value AS terminal, r.phase AS recovery_phase, r.signature AS signature,
                       coalesce(r.classic,0) AS classic, coalesce(r.remedial,0) AS remedial
                FROM jobs j LEFT JOIN import_terminal t ON j.id=t.job AND j.attempt=t.attempt
                     LEFT JOIN import_recovery r ON j.id=r.job
                WHERE j.option IS NULL AND j.refresh=0 AND (
                    (j.state IN ('failed','partial','review') AND (r.attempt IS NULL OR j.attempt!=r.attempt
                          OR (r.phase IN ('retry_wait','codex_wait') AND r.due<=?)))
                    OR (j.state IN ('done','cancelled') AND r.phase NOT IN ('done','cancelled')))
                ORDER BY j.seq LIMIT ?
            """, (now, limit))
            return [self.decode(r) for r in rows]

    def diagnosis_outcomes(self, identity):
        # Stream failed outcomes independently of the size of the import lot.
        with self.connect() as db:
            cursor = db.execute("""SELECT payload FROM outcomes WHERE job=?
                                   AND json_extract(payload,'$.status') IN ('failure','partial') ORDER BY rowid""",
                                (identity,))
            for value, in cursor:
                yield json.loads(value)

    def recover_import(self, row, signature, phase, message, due=0., *, replacement=None):
        # The attempt generation guards against manual retry, integration
        # acknowledgement and worker completion racing an automatic decision.
        with self.connect(write=True) as db:
            active = db.execute("SELECT state,attempt,refresh,targets,keys FROM jobs WHERE id=?",
                                (row["id"],)).fetchone()
            if (active is None or active["state"] != row["state"] or active["attempt"] != row["attempt"]
                    or active["refresh"]):
                return False
            existing = db.execute("SELECT classic,remedial FROM import_recovery WHERE job=?", (row["id"],)).fetchone()
            classic, remedial = existing if existing else (0, 0)
            if phase == "classic" and classic >= 2 or phase == "verified" and remedial:
                return False
            if phase in ("classic", "verified"):
                if replacement:
                    old, new = replacement
                    targets = json.loads(active["targets"])
                    if not any(url == old for _, url in targets) or any(url == new for _, url in targets):
                        return False
                    targets = [(title, new if url == old else url) for title, url in targets]
                    keys = sorted({import_key(url) for _, url in targets})
                    db.execute("UPDATE jobs SET targets=?,keys=? WHERE id=?", (json.dumps(targets), json.dumps(keys), row["id"]))
                    prior = db.execute("SELECT payload FROM outcomes WHERE job=? AND url=?", (row["id"], old)).fetchone()
                    if prior is not None:
                        saved = json.loads(prior[0])
                        saved["url"] = new
                        db.execute("UPDATE outcomes SET url=?,payload=? WHERE job=? AND url=?",
                                   (new, json.dumps(saved), row["id"], old))
                db.execute("UPDATE jobs SET state='queued',message=? WHERE id=?", (message, row["id"]))
            elif phase == "review":
                db.execute("UPDATE jobs SET state='review',message=? WHERE id=?", (message, row["id"]))
            elif phase in ("retry_wait", "codex_wait"):
                db.execute("UPDATE jobs SET message=? WHERE id=?", (message, row["id"]))
            db.execute("INSERT OR REPLACE INTO import_recovery VALUES(?,?,?,?,?,?,?)",
                       (row["id"], row["attempt"], signature, phase, due,
                        classic + (phase == "classic"), remedial + (phase == "verified")))
            db.execute("INSERT INTO import_recovery_history(job,attempt,signature,phase,message) VALUES(?,?,?,?,?)",
                       (row["id"], row["attempt"], signature, phase, message))
            return True

    def reconsider_import(self, signature):
        with self.connect() as db:
            db.execute("""UPDATE import_recovery SET phase='codex_wait',due=0
                          WHERE signature=? AND phase IN ('codex_wait','review')""", (signature,))

    def refreshed(self, generations):
        # A refresh running in Strawberry may finish after a retry. Its
        # acknowledgement must apply only to the results that it refreshed.
        with self.connect() as db:
            db.executemany("UPDATE jobs SET refresh=0 WHERE id=? AND refresh=?", generations)

    def integration_failed(self, identity, generation, missing_urls):
        # Keep the publication receipts for selective retry. A stale catalogue
        # refresh must never overwrite the result of a newer attempt.
        with self.connect(write=True) as db:
            if not db.execute("""
                UPDATE jobs SET state='partial',refresh=0,message='Indexation incomplète · relance disponible'
                WHERE id=? AND refresh=? AND state NOT IN ('queued','running','cancelled')
            """, (identity, generation)).rowcount:
                return
            for url in missing_urls:
                row = db.execute("SELECT payload FROM outcomes WHERE job=? AND url=?", (identity, url)).fetchone()
                if row:
                    outcome = json.loads(row[0])
                    outcome.update(status="partial", message="Pistes manquantes dans la bibliothèque")
                    outcome.setdefault("diagnostics", []).append(
                        dict(category="processing", stage="integration", detail="Pistes absentes du catalogue local."))
                    db.execute("UPDATE outcomes SET payload=? WHERE job=? AND url=?",
                               (json.dumps(outcome), identity, url))

    def requests(self):
        with self.connect() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT payload FROM requests ORDER BY rowid")]

    def save_request(self, request, *, db=None):
        if db is None:
            with self.connect() as connection:
                return self.save_request(request, db=connection)
        db.execute("INSERT INTO requests VALUES (?, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                   (request["id"], json.dumps(request)))
