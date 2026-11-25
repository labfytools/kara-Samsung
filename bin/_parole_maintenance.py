"""Ordonnanceur de maintenance rattaché au propriétaire GTK du service Kara."""
from collections import deque
from logging import Logger
from logging.handlers import RotatingFileHandler
from datetime import datetime
import json
from math import isfinite
import os
from pathlib import Path
import re
from shutil import disk_usage
import sqlite3
import subprocess
import sys
import tempfile
from threading import Event, Lock, Thread
from time import monotonic, time

from _kara_import_resources import lease
from _kara_paths import BIN
from _kara_prepare import PreparedCache, file_token, lrc_token
from _kara_refine import VERSION as REFINEMENT_VERSION
from _kara_stems import ROOT as STEM_ROOT, FILES, cached_stems, stem_directory
from _parole_codex import KINDS, research
from _parole_artist_groups import recover as recover_artist_groups
from _parole_index import Index, document, encoded_key, inventory, sidecar
from _parole_import_diagnostics import ImportDiagnostics
from _parole_import_enrichment import COVER as IMPORT_COVER, KINDS as IMPORT_ENRICHMENT, LYRICS as IMPORT_LYRICS
from _parole_import_enrichment import paths_for as enrichment_paths, pending as enrichment_pending
from _parole_night import Night
from _parole_process import Interrupted, run
from _parole_resources import Resources
from _parole_sources import discover, seed, controls
from _paroles_lrc import MAX_LRC_BYTES

CATEGORIES = ("SYSTEM", "SCAN", "LYRICS", "LRC", "ALBUM", "DISCOGRAPHIE", "DOWNLOAD", "METADATA",
              "COVER", "CODEX", "SOURCE", "DEMUCS", "TURBO", "KARA", "TV", "IDLE", "CACHE",
              "SUCCESS", "WARNING", "ERROR")
NETWORK = ("lyrics", "lrc", "album", "discography", "cover", "metadata", "codex", "source", "source_probe")
HOUR = 3600
DAY = 24 * HOUR
PHASES = {"cache": "Vérification du cache", "lyrics": "Paroles de référence",
          "model": "Chargement du modèle", "transcription": "Transcription acoustique",
          "cached_transcription": "Première transcription réutilisée",
          "comparison": "Comparaison des mots", "correction": "Vérification des repères",
          "words": "Repères mot par mot", "refinement": "Reprises localisées",
          "vocal_refinement": "Vérification sur la voix séparée", "saving": "Enregistrement",
          "ready": "Référence validée", "cached": "Référence réutilisée",
          "cached_insufficient": "Couverture insuffisante"}
NIGHT_RESTART_SETTINGS = (
    "night_enabled", "night_lid", "night_lid_qualified", "night_screen_off", "night_idle",
    "night_minutes", "night_cpu_temperature", "night_gpu_temperature", "night_closed_temperature",
    "cache_gib", "reserve_gib", "generate", "lyrics", "lrc", "paused",
)


LOG_PRIVATE = re.compile(r"https?://[^\s]+|(?i:\b(?:authorization|password|cookie|token|secret|"
                         r"api[-_ ]?key)\b)\s*[:=][^\n]*|(?i:\bbearer\s+)[A-Za-z0-9._-]+|eyJ[A-Za-z0-9_-]{12,}\.[A-Za-z0-9_-]+")


def event_text(message):
    return LOG_PRIVATE.sub("[expurgé]", "".join(ch if ch.isprintable() else " " for ch in message))[:320]


class Journal:
    def __init__(self, folder):
        self.events = deque(maxlen=600)
        self.path = folder / "maintenance.jsonl"
        self.serial = 0
        self.mutex = Lock()
        for path in (self.path.with_suffix(".jsonl.1"), self.path):
            try:
                with path.open("rb") as reader:
                    for line in reader:
                        try:
                            stamp, category, level, text = json.loads(line)
                            if category in CATEGORIES and all(isinstance(s, str) for s in (stamp, level, text)):
                                self.events.append((stamp, category, level, event_text(text)))
                        except (TypeError, ValueError, UnicodeError):
                            continue
            except FileNotFoundError:
                continue
        self.path.touch(mode=0o600, exist_ok=True)
        self.logger = Logger("parole-maintenance")
        self.handler = RotatingFileHandler(self.path, maxBytes=512 * 1024, backupCount=4, encoding="utf-8")
        self.logger.addHandler(self.handler)

    def emit(self, category, message, level="INFO"):
        # Callers publish observed phases and local titles only. Never send
        # subprocess stderr, server URLs, or agent reasoning here.
        clean = event_text(message)
        entry = datetime.now().strftime("%Y-%m-%d %H:%M:%S"), category, level, clean
        with self.mutex:
            self.logger.info(document(entry))
            self.events.append(entry)
            self.serial += 1

    def snapshot(self):
        with self.mutex:
            return self.serial, tuple(self.events)

    def history(self):
        entries = deque(maxlen=4000)
        for path in [*(self.path.with_suffix(f".jsonl.{i}") for i in range(4, 0, -1)), self.path]:
            try:
                with path.open(encoding="utf-8") as log:
                    for line in log:
                        try:
                            entry = json.loads(line)
                            if isinstance(entry, list) and len(entry) == 4 and all(isinstance(part, str) for part in entry):
                                entries.append((*entry[:3], event_text(entry[3])))
                        except ValueError:
                            continue
            except OSError:
                continue
        return tuple(entries)

    def close(self):
        with self.mutex:
            self.handler.close()


class Maintenance:
    def __init__(self, root, collection, *, state=None, import_queue=None, cache=None, background=False):
        self.index = Index(root) if state is None else Index(root, state)
        self.state_root = state
        self.collection = collection
        self.import_queue = import_queue
        self.import_diagnostics = ImportDiagnostics(self, import_queue) if import_queue is not None else None
        self.cache = cache or PreparedCache()
        self.resources = Resources(self.index.root)
        self.journal = Journal(self.index.folder)
        self.night = Night(self.index, self.journal)
        self.night_running = False
        self.night_settings_at = 0.
        self.night_handover = 0.
        self.night_policy = None
        self.night_lrc = (False, False)
        self.night_measures = {}
        self.owner = None
        self.foreground = Event()
        if not background:
            self.foreground.set()
        self.shutdown = Event()
        self.wake = Event()
        self.interrupt = Event()
        self.scan_now = Event()
        self.priority = False
        self.thread = None
        self.active = None
        self.progress = None
        self.block = ""
        self.cancelled = None
        self.priority_until = 0.
        self.next_scan = 0
        self.next_network = 0
        self.network_cursor = 0
        self.import_enrichment_turn = 0
        self.import_activity = (0, 0)
        self.import_lyrics_stats = None
        self.next_import_probe = 0.
        self.revision = 0
        self.sampling = 0
        self.lock = Lock()
        self.metrics = {}
        seed(self.index)

    def start(self):
        if self.thread is None:
            self.thread = Thread(target=self.loop, name="parole-maintenance", daemon=False)
            self.thread.start()

    def close(self):
        self.shutdown.set()
        self.wake.set()
        if self.thread is None:
            self.journal.close()

    def set_priority(self, busy):
        with self.lock:
            self.priority = busy
            if busy and self.active and self.active[0] in ("prepare", *IMPORT_ENRICHMENT):
                self.interrupt.set()
        if busy and self.active:
            self.wake.set()

    def yield_heavy(self):
        # Mobile/organizer callbacks may retry as soon as our process group
        # exits. The GTK thread does not wait on a decoder or GPU driver.
        with self.lock:
            self.priority_until = monotonic() + 5
            if self.active and self.active[0] == "prepare":
                self.interrupt.set()
                return False
        return True

    def rescan(self):
        self.scan_now.set()
        self.wake.set()

    def pause(self, paused):
        self.index.configure(paused=paused)
        if paused:
            self.interrupt.set()
        self.wake.set()
        self.journal.emit("SYSTEM", "Maintenance suspendue" if paused else "Maintenance reprise")

    def show(self):
        self.foreground.set()
        self.wake.set()

    def cancel(self, kind, subject):
        self.index.cancel(kind, subject)
        with self.lock:
            if self.active == (kind, subject):
                self.cancelled = self.active
                self.interrupt.set()
        self.wake.set()

    def inspect_cache(self, path, old, token, side):
        try:
            reference = self.cache.folder(path) / "prepared.json"
            rt = file_token(reference) if reference.is_file() else None
            model = file_token(self.cache.model) if self.cache.model.is_file() else None
            stem = stem_directory(path)
            stem_tokens = [file_token(stem / name) if (stem / name).is_file() else None
                           for name in ("meta.json", *FILES)]
            # A previous inventory contains coverage but cannot distinguish a
            # rejected reference from a validated reference awaiting stems.
            saved = document([rt, model, stem_tokens, REFINEMENT_VERSION, "reference-status"])
            if old and old["cache"] == saved and old["token"] == token and old["side"] == side:
                return saved, old["kara"], json.loads(old["info"]).get("report", {})
            item = self.cache.cached(path, verify=False) if rt and model else None
            stems = cached_stems(path) if all(stem_tokens) else None
            if item and item.get("refinement", {}).get("version") != REFINEMENT_VERSION:
                item = None
            report = {"instrumental": bool(stems), "voice": bool(stems), "reference_validated": bool(item)}
            if item:
                report.update(coverage=item["report"]["word_coverage"], line_coverage=item["report"]["line_coverage"],
                              source=item["source"].get("provider", "bibliothèque"),
                              corrected=item["report"].get("changed", 0))
            negative = self.cache.cached(path, verify=False, insufficient=True) if rt and model and not item else None
            if negative:
                report.update(coverage=negative["report"]["word_coverage"],
                              line_coverage=negative["report"]["line_coverage"])
            state = ("prêt" if item and stems else "partiel" if item or stems or negative else
                     "obsolète" if rt or old and old["kara"] == "prêt" else "absent")
            return saved, state, report
        except (OSError, ValueError):
            return "", "absent", {}

    def stopped(self):
        if self.shutdown.is_set() or self.interrupt.is_set():
            return True
        if self.active and self.active[0] == "prepare":
            now = monotonic()
            if self.import_queue and now >= self.next_import_probe:
                self.next_import_probe = now + 1.
                if self.import_queue.pending():
                    self.interrupt.set()
                    return True
            if self.night_running:
                return not self.night_allowed(self.current_settings, running=True)
            return not self.resources.check(self.current_settings,
                                            busy=self.priority or monotonic() < self.priority_until, running=True)
        return False

    def night_allowed(self, settings, *, running):
        now = monotonic()
        if running and now >= self.night_settings_at:
            self.night_settings_at = now + .5
            try:
                fresh = self.index.settings(wait=.15)
            except (OSError, sqlite3.Error, ValueError):
                return self.night.report("Préparation nocturne suspendue · réglages indisponibles")
            if any(settings[name] != fresh[name] for name in NIGHT_RESTART_SETTINGS):
                return self.night.report("Préparation nocturne suspendue · réglages modifiés")
        idle = self.resources.session_idle(settings["night_idle"])
        if self.resources.resumed:
            self.resources.resumed = False
            self.night.activity()
            if running:
                return self.night.report("Reprise d’activité · préparation nocturne suspendue")
        if not self.night.check(settings, busy=self.priority or now < self.priority_until, fast=running):
            return False
        if not idle:
            return self.night.report("Préparation nocturne en attente d’inactivité")
        if self.night.expired(settings):
            return False
        if not self.resources.check(settings, running=running, nocturnal=True, lid=self.night.desktop.lid):
            return self.night.report(self.resources.reason)
        self.night.reason = ""
        return True

    def scan(self, settings, *, limit=None):
        started = monotonic()
        self.active = ("scan", "")
        self.progress = None
        self.journal.emit("SCAN", "Analyse incrémentale de la bibliothèque")
        try:
            args = () if self.state_root is None else (self.state_root,)
            associations = self.index.associations(*args)
            result = inventory(self.index, self.collection, self.inspect_cache, self.stopped,
                               lambda scanned: setattr(self, "progress", ("SCAN", f"{scanned} chansons vérifiées", None)),
                               limit=limit)
            self.metrics.update(result, scan_seconds=monotonic() - started, associations=associations)
            self.journal.emit("SCAN", "{scanned} vérifiées · {changed} mises à jour · {missing} disparues · {errors} erreurs".format(**result))
            self.revision += 1
            if not self.stopped():
                self.plan(settings)
        finally:
            self.active = None
            self.progress = None

    def plan(self, settings, *, paths=None):
        now = time()
        with self.index.db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Queue cursors are stable song/edition identities, independent
            # from filter, pagination and GTK selection.
            blocked = {row[0] for row in db.execute("SELECT subject FROM exclusions WHERE kind='artist'")}
            songs = (db.execute("SELECT * FROM songs") if paths is None else
                     (row for path in paths for row in db.execute("SELECT * FROM songs WHERE path=?", (str(path),))))
            for song in songs:
                revision = encoded_key(song["token"], song["side"], song["title"], song["artist"], song["edition"])
                subject = song["id"]
                if song["owner"] in blocked:
                    db.execute("UPDATE tasks SET state='excluded' WHERE subject=? AND state!='running'", (subject,))
                    continue
                tags = json.loads(song["info"])["tags"]
                missing_tags = any(not tags.get(field) for field in ("title", "artist", "album"))
                for kind, missing in (("lyrics", song["lyrics"] == "absent"), ("lrc", song["lrc"] in ("absent", "intégré")),
                                      ("metadata", missing_tags),
                                      ("prepare", song["kara"] != "prêt" and song["lyrics"] != "instrumental")):
                    if not missing:
                        db.execute("UPDATE tasks SET state='done' WHERE kind=? AND subject=? "
                                   "AND state IN ('queued','temporary','suspended')", (kind, subject))
                        continue
                    priority = 25 if song["plays"] else 35
                    if now - song["added"] <= DAY and song["previous"] is None:
                        priority = 20
                    if kind == "lrc":
                        priority -= 1
                    if kind == "prepare":
                        priority += 30 if song["lrc"] == "absent" else 20
                    if kind == "prepare" and settings["night_enabled"]:
                        night_revision = encoded_key(revision, "night", settings["generate"], settings["lrc"], settings["lyrics"])
                        self.index.enqueue(kind, subject, night_revision, priority, db=db)
                    elif settings.get("autoprepare" if kind == "prepare" else kind):
                        self.index.enqueue(kind, subject, revision, priority, db=db)
            if paths is not None:
                return
            for kind, column in (("album", "edition"), ("discography", "owner"), ("cover", "edition")):
                enabled = settings["albums" if kind == "album" else kind]
                if not enabled:
                    continue
                entries = db.execute(f"""SELECT {column} AS subject, max(added) AS revision, count(*) AS tracks
                                         FROM songs WHERE album!='' GROUP BY {column}""")
                for row in entries:
                    subject = row["subject"]
                    skipped = db.execute(f"""SELECT 1 FROM songs WHERE {column}=? AND owner IN
                                               (SELECT subject FROM exclusions WHERE kind='artist') LIMIT 1""", (subject,)).fetchone()
                    if skipped or db.execute("SELECT 1 FROM exclusions WHERE kind=? AND subject=?", (kind, subject)).fetchone():
                        db.execute("UPDATE tasks SET state='excluded' WHERE kind=? AND subject=? AND state!='running'",
                                   (kind, subject))
                        continue
                    revision = encoded_key(row["revision"], row["tracks"], settings["releases"])
                    self.index.enqueue(kind, subject, revision, 42 if kind == "album" else 46 if kind == "cover" else 45, db=db)
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind IN ('lyrics','lrc','prepare','metadata')
                         AND NOT EXISTS(SELECT 1 FROM songs WHERE id=tasks.subject)""")
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind IN ('album','cover')
                         AND NOT EXISTS(SELECT 1 FROM songs WHERE edition=tasks.subject)""")
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind='discography'
                         AND NOT EXISTS(SELECT 1 FROM songs WHERE owner=tasks.subject)""")

    def query(self, kind, subject):
        with self.index.db() as db:
            if kind in ("album", "cover", "discography", "track"):
                prop = "owner" if kind == "discography" else "edition"
                row = db.execute(f"SELECT * FROM songs WHERE {prop}=? LIMIT 1", (subject,)).fetchone()
            else:
                row = db.execute("SELECT * FROM songs WHERE id=?", (subject,)).fetchone()
            review = (db.execute("SELECT value FROM reviews WHERE kind='album' AND subject=?", (subject,)).fetchone()
                      if kind == "track" else None)
        if row is None:
            return None
        revision = encoded_key(kind, subject, row["token"] if kind not in ("discography", "album", "cover") else "")
        return {"research_id": revision, "kind": kind, "artist": (row["owner"] if kind in ("discography", "album", "cover") else row["artist"])[:180],
                "title": event_text(row["title"])[:180] if kind not in ("discography", "album", "cover") else "",
                "album": event_text(row["album"])[:180] if kind != "discography" else "", "year": row["year"],
                "duration": round(row["duration"]), "disc": row["disc"], "track": row["track"],
                "release_id": (release.get("id") or row["release_id"])[:100], "artist_id": row["artist_id"][:100],
                **({"missing_tracks": [[disc, track, event_text(title)[:180]] for disc, track, title in release.get("missing", [])[:15]]}
                   if kind == "track" else {})}

    def queue_codex(self, kind, subject, failure="manuel", *, manual=False):
        query = self.query(kind, subject)
        if query is None or kind not in KINDS:
            return
        settings = self.index.settings()
        if not settings["codex"] or kind not in settings["codex_categories"] or not (manual or settings["codex_auto"]):
            return False
        if manual:
            query["manual"] = True
        query.update(failure=failure, providers=self.active_sources(kind))
        identity = query["research_id"]
        if self.index.enqueue("codex", identity, identity, 15 if manual else 48, query, force=manual):
            self.journal.emit("CODEX", f"Recherche mise en file · {query['artist']} {query['title'] or query['album']}")
            self.wake.set()
        return True

    def queue_provider_discovery(self, query):
        query = " ".join(query.split())
        if not 2 <= len(query) <= 180:
            raise ValueError("Indique un artiste, un titre ou un album à rechercher.")
        if not self.index.settings()["codex"]:
            return False
        identity = encoded_key("provider_search", query.casefold())
        payload = {"research_id": identity, "kind": "provider_search", "manual": True,
                   "artist": "", "title": "", "album": query, "providers": self.active_sources("import")}
        if self.index.enqueue("codex", identity, identity, 15, payload, force=True):
            self.journal.emit("CODEX", "Recherche de téléchargements autorisés mise en file")
            self.wake.set()
        return True

    def queue_adapter(self, source):
        if source["adapter"] != "custom" or source["state"] == "excluded" or not self.index.settings()["codex"]:
            return False
        config = json.loads(source["config"])
        identity = encoded_key("adapter", source["id"], config)
        payload = {"research_id": identity, "kind": "adapter", "artist": source["name"], "title": "",
                   "album": "", "url": config["url"], "documentation": config.get("docs", ""),
                   "terms": config.get("terms", ""), "category": source["category"]}
        if self.index.enqueue("codex", identity, identity, 12, payload):
            self.journal.emit("CODEX", "Proposition d’adaptateur mise en file")
            self.wake.set()
        return True

    def queue_import_patch(self, task):
        query = json.loads(task["payload"])
        result = None
        with self.index.db() as db:
            row = db.execute("SELECT value FROM reviews WHERE kind='codex' AND subject=?", (task["subject"],)).fetchone()
            if row:
                result = json.loads(row[0])
        if query.get("kind") != "import_failure" or not result or result.get("resolution", {}).get("action") != "code":
            return False
        subject = encoded_key("import_patch", task["subject"])
        query.update(kind="import_patch", research_id=subject, diagnosis=result["proposal"],
                     contract="Kara reprend la file SQLite par lot. Il conserve les reçus des pistes publiées et le verrou de bibliothèque.")
        if self.index.enqueue("codex", subject, subject, 12, query):
            self.journal.emit("CODEX", "Ébauche de correctif mise en file pour revue")
            self.wake.set()
        return True

    def active_sources(self, kind):
        with self.index.db() as db:
            return [r["name"] for r in db.execute("SELECT name FROM sources WHERE state='active' AND category LIKE ? LIMIT 20",
                                                  (f"%{kind}%",))]

    def refresh_song(self, song, **changes):
        if not song:
            return
        inventory(self.index, self.collection, self.inspect_cache, self.shutdown.is_set,
                  lambda *_: None, paths=[Path(song["path"])])
        fresh = self.index.song(song["id"])
        if fresh:
            details = json.loads(fresh["info"])
            details.update(changes.pop("provenance", {}))
            self.index.update(song["id"], checked=time(), info=document(details), **changes)
        self.revision += 1

    def do_song(self, task, result):
        identity, kind = task["subject"], task["kind"]
        song = self.index.song(identity)
        if song is None:
            return "excluded", "disparu", 0
        outcome = result["state"]
        if outcome == "rescan":
            self.refresh_song(song)
            return "temporary", outcome, 10
        if outcome == "found":
            provider = result["source"]
            if kind == "lrc":
                self.refresh_song(song, lrc="intégré" if provider == "Strawberry" else "externe",
                                  provenance={"lrc_provider": provider})
            else:
                self.refresh_song(song, lyrics="Strawberry", audio=result["audio"],
                                  provenance={"lyrics_provider": provider})
            self.journal.emit("SUCCESS", f"{song['title']} · {'LRC' if kind == 'lrc' else 'Paroles'} vérifiés")
        elif outcome == "instrumental":
            self.index.update(identity, lyrics="instrumental", checked=time())
            self.revision += 1
        elif outcome == "present":
            self.refresh_song(song)
        elif outcome in ("temporary", "network", "unavailable", "limited"):
            msg = {"temporary": "Publication ou moteur temporairement indisponible",
                   "network": "Erreur réseau", "unavailable": "Fournisseur indisponible",
                   "limited": "Limitation du fournisseur"}[outcome]
            self.journal.emit(kind.upper(), msg, "WARNING")
            return "temporary", msg, min(DAY, 120 * 2**min(task["attempts"], 9))
        elif outcome == "ambiguous":
            self.queue_codex(kind, identity, "correspondances classiques ambiguës")
            return "review", "correspondance ambiguë", 0
        elif outcome == "not_found":
            self.journal.emit(kind.upper(), f"Aucune correspondance vérifiée · {song['title']}")
            self.queue_codex(kind, identity, "sources classiques épuisées")
            return "review", outcome, 7 * DAY
        elif outcome == "invalid":
            return "review", "à vérifier", 7 * DAY
        return "done", outcome, 0

    def do_review(self, task, result):
        kind, subject = task["kind"], task["subject"]
        state = result["state"]
        if state == "temporary":
            return "temporary", state, min(DAY, 600 * 2**min(task["attempts"], 7))
        self.index.review(kind, subject, state, result.get("value", {}))
        self.revision += 1
        if state in ("not_found", "ambiguous", "partial"):
            self.queue_codex(kind, subject, state)
        if kind == "metadata" and state == "identified":
            self.journal.emit("METADATA", "Identification documentée dans les détails du morceau")
        if state == "incomplete":
            with self.index.db() as db:
                permitted = db.execute("SELECT 1 FROM import_permissions WHERE kind='album' AND subject=?",
                                       (subject,)).fetchone()
            if not permitted and result["value"]["missing"]:
                self.queue_codex("track", subject, state)
            self.journal.emit("ALBUM", f"{len(result['value']['missing'])} pistes manquantes · {result['value']['title']}")
        if kind == "discography" and "missing" in result.get("value", {}):
            self.journal.emit("DISCOGRAPHIE", f"{len(result['value']['missing'])} publications absentes · {subject}")
        return ("temporary" if state not in ("excluded",) else "excluded",
                state, self.current_settings["review_days"] * DAY)

    def output(self, command, timeout=140, observer=None, maximum=262_144, *, extra_env=None):
        env = dict(os.environ, KARA_MUSIC_LIBRARY=str(self.index.root))
        if extra_env:
            env.update(extra_env)
        if self.state_root is not None:
            env["XDG_STATE_HOME"] = str(self.state_root)
        if self.night_running:
            env["KARA_DEMUCS_THREADS"] = "2"
        # All input paths are program arguments; no shell evaluates titles or paths.
        retain = (self.owner,) if self.owner is not None else ()
        return run(command, stop=self.stopped, timeout=timeout, event=observer, env=env, maximum=maximum,
                   retain=retain)

    def enrich_import(self, task):
        kind, subject = task["kind"], task["subject"]
        paths = enrichment_paths(self.index, kind, subject)
        def update(event):
            if event.get("event") == "activity":
                self.import_activity = event["lyrics"], event["lrc"]
        try:
            code, data = self.output([sys.executable, "-B", str(BIN / "_parole_work.py"), kind, subject],
                                     timeout=180, observer=update, extra_env={"KARA_ENRICHMENT_EVENTS": "1"})
        finally:
            self.import_activity = (0, 0)
        value = json.loads(data.splitlines()[-1]) if not code else {"state": "temporary"}
        if kind == IMPORT_LYRICS and paths:
            if "tracks_seconds" in value:
                self.import_lyrics_stats = value
                self.index.review("import_lyrics_metrics", subject, "measured", value)
                self.journal.emit("LYRICS", f"{value['lyrics'].get('found', 0)} textes · "
                                  f"{value['lrc'].get('found', 0)} LRC · {value['seconds']:.2f} s · "
                                  f"{value['requests']} requêtes · {value['cache_hits']} recherches évitées")
            inventory(self.index, self.collection, self.inspect_cache, self.stopped, lambda *_: None, paths=paths)
            self.plan(self.current_settings, paths=paths)
            self.revision += 1
        if self.stopped():
            raise Interrupted
        if value["state"] == "complete":
            return "done", "enrichissement vérifié", 0
        if value["state"] == "temporary" and task["attempts"] < 2:
            return "temporary", "recherche différée", 300
        return "review", "enrichissement partiel · maintenance complémentaire", 0

    def tick_import_enrichment(self, settings, now):
        # Enrichment can run without a nocturnal resource sample. It needs
        # the library's free-space reserve and consumes no preparation cache.
        if disk_usage(self.index.root).free <= settings["reserve_gib"] * 1024**3:
            return False
        # One owner alternates stages and keeps their claims separate from the
        # classic maintenance budget. Audio imports own their existing slots.
        for _ in IMPORT_ENRICHMENT:
            kind = IMPORT_ENRICHMENT[self.import_enrichment_turn]
            self.import_enrichment_turn = (self.import_enrichment_turn + 1) % len(IMPORT_ENRICHMENT)
            if not (settings["lyrics"] or settings["lrc"] if kind == IMPORT_LYRICS else settings["cover"]):
                continue
            task = self.index.claim((kind,), now)
            if task is not None:
                self.execute(task)
                # Recheck available enrichment immediately. Supplier gates
                # still control traffic, including after errors and retries.
                self.wake.set()
                return True
        return False

    def night_reference(self, song, path):
        settings = self.current_settings
        # Retain a verified provider response across Demucs/Turbo cancellation.
        # Audio/sidecar edits and changed enabled sources invalidate that reply.
        local_text = sidecar(path, ".txt")[1] or json.loads(song["info"]).get("plain", "")
        with self.index.db() as db:
            sources = [tuple(row) for row in db.execute(
                "SELECT id,config FROM sources WHERE state='active' ORDER BY id")]
            revision = encoded_key(song["token"], song["side"], local_text,
                                   settings["lrc"], settings["lyrics"], settings["generate"], sources)
            receipt = db.execute("SELECT checked,value FROM reviews WHERE kind='prepare_reference' AND subject=?",
                                 (song["id"],)).fetchone()
        if receipt and time() - receipt["checked"] < DAY:
            saved = json.loads(receipt["value"])
            if saved.pop("revision", None) == revision:
                return saved

        if ((settings["lrc"] or settings["generate"] and settings["lyrics"] and not local_text.strip())
                and not self.index.reserve_daily("network", settings["network_daily"], gap=settings["network_interval"])):
            return {"state": "limited"}

        def lookup(kind):
            try:
                _, data = self.output([sys.executable, "-B", str(BIN / "_parole_work.py"), kind, song["id"]],
                                      settings["network_timeout"], maximum=6 * MAX_LRC_BYTES + 1024)
            except TimeoutError:
                return {"state": "unavailable"}
            return json.loads(data)

        result = {"state": "not_found"}
        if settings["lrc"]:
            self.journal.emit("LRC", f"Dernière recherche de minutage · {song['title']}")
            result = lookup("prepare_lrc")
            if result["state"] == "found":
                result["mode"] = "timed"
        if result["state"] not in ("found", "instrumental", "rescan") and settings["generate"]:
            # Provider timeouts and HTTP limits do not invalidate a known
            # lyric text. The completed lookup attempt may therefore fall
            # back to acoustically observed timestamps from that text.
            if local_text.strip() and len(local_text.encode()) <= MAX_LRC_BYTES:
                result = {"state": "found", "lyrics": local_text, "source": "bibliothèque"}
            elif settings["lyrics"]:
                result = lookup("prepare_plain")
            if result["state"] == "found":
                result["mode"] = "plain"
        if result["state"] in ("found", "not_found", "ambiguous", "invalid", "instrumental"):
            self.index.review("prepare_reference", song["id"], result["state"], dict(result, revision=revision))
        return result

    def prepare(self, task, *, nocturnal=False):
        identity = task["subject"]
        song = self.index.song(identity)
        if song is None:
            return "excluded", "disparu", 0
        path = Path(song["path"])
        if path.resolve(strict=True) != path or not path.is_relative_to(self.index.root):
            return "excluded", "chemin hors bibliothèque", 0
        if document(file_token(path)) != song["token"]:
            self.refresh_song(song)
            return "temporary", "modification détectée", 10
        info = json.loads(song["info"])
        plain = sidecar(path, ".txt")[1] or info.get("plain")
        source_lrc = path.with_suffix(".lrc")
        try:
            rejected = lrc_token(source_lrc)
        except ValueError:
            return "review", "LRC original non ordinaire", 0
        local = rejected is not None and song["lrc"] in ("local", "externe", "généré")
        saved = self.cache.cached(path, verify=False)
        if nocturnal and local and not saved:
            insufficient = self.cache.cached(path, verify=False, insufficient=True)
            if insufficient:
                # A structurally valid LRC can fail the acoustic check. Its
                # negative receipt survives the handoff to a verified alternate.
                local = False
                if insufficient["source"]["kind"] == "fallback":
                    return "review", "référence Kara insuffisante", 0
        reference_option, provider = None, "Strawberry"
        if not saved and not local and nocturnal:
            if json.loads(song["side"]) != [sidecar(path, suffix)[0] for suffix in (".lrc", ".txt")]:
                self.refresh_song(song)
                return "temporary", "paroles modifiées", 10
            result = self.night_reference(song, path)
            if result["state"] == "instrumental":
                self.index.update(identity, lyrics="instrumental", checked=time())
                return "excluded", "instrumental", 0
            if result["state"] in ("temporary", "network", "unavailable", "limited", "rescan"):
                return "temporary", "dernière recherche indisponible", 300
            if result["state"] != "found":
                return "review", "référence fiable absente", 0
            reference_option = "--timed-reference" if result["mode"] == "timed" else "--plain-reference"
            plain, provider = result["lyrics"], result["source"]
        elif not saved and not local:
            with self.index.db() as db:
                pending = db.execute("""SELECT state,detail,updated FROM tasks
                                        WHERE kind='lrc' AND subject=?""", (identity,)).fetchone()
            if source_lrc.is_symlink() or source_lrc.exists():
                return "review", "LRC existant à vérifier", 0
            if not self.current_settings["generate"] or not plain or not self.current_settings["lrc"]:
                return "review", "référence absente", 0
            if not pending or pending["state"] != "review" or pending["detail"] != "not_found":
                return "temporary", "dernière recherche LRC en attente", 1800
            if time() - pending["updated"] > DAY:
                self.index.enqueue("lrc", identity, task["revision"], priority=13, force=True)
                return "temporary", "dernière recherche LRC replanifiée", 300
            if self.current_settings["codex"]:
                research = self.query("lrc", identity)
                with self.index.db() as db:
                    awaiting = db.execute("SELECT state,due FROM tasks WHERE kind='codex' AND subject=?",
                                          (research["research_id"],)).fetchone() if research else None
                if awaiting and awaiting["state"] in ("queued", "running") and time() - task["due"] < 300:
                    return "temporary", "recherche approfondie en attente", 300
            reference_option = "--plain-reference"
        duration = song["duration"]
        if not duration:
            from _kara_stems import duration as media_duration
            duration = media_duration(path)
        if not 1 <= duration <= 3600:
            return "review", "durée hors limites du moteur Kara", 0
        self.resources.collect_storage(monotonic(), force=True)
        if not self.resources.allowed_storage(self.current_settings, duration):
            return "suspended", "plafond du cache ou réserve disque", 1800

        def status(category):
            def receive(event):
                if event.get("event") == "progress" and type(event.get("percent")) in (int, float):
                    phase = PHASES.get(event.get("phase"), "Séparation voix / musique" if category == "DEMUCS" else "Préparation")
                    self.progress = (category, f"{song['title']} · {phase}", max(0, min(100, event["percent"])) / 100)
                elif event.get("event") == "result":
                    seconds = event.get("seconds")
                    if type(seconds) in (int, float):
                        self.metrics["turbo_seconds"] = seconds
                    if nocturnal and not event.get("cached"):
                        report = event.get("report", {})
                        refinement = report.get("refinement", {})
                        # A cached first-pass report describes old work. A
                        # local retry can contribute only its own elapsed time.
                        measurements = {}
                        if refinement.get("decodes"):
                            measurements["reprises"] = refinement.get("seconds")
                        if not refinement.get("reused_reference") and not report.get("reused_first_pass"):
                            measurements["turbo"] = report.get("analysis_seconds")
                        self.night_measures.update((name, seconds) for name, seconds in measurements.items()
                                                   if type(seconds) in (int, float) and isfinite(seconds)
                                                   and seconds >= 0)
            return receive

        if not cached_stems(path):
            began = monotonic()
            self.journal.emit("DEMUCS", f"Séparation voix / accompagnement · {song['title']}")
            rc, _ = self.output(["nice", "-n", "12", sys.executable, "-B", str(BIN / "_kara_stems.py"), str(path)],
                                1800, status("DEMUCS"))
            self.metrics["demucs_seconds"] = monotonic() - began
            if rc:
                return "temporary", "séparation indisponible", 6 * HOUR
            if nocturnal:
                self.night_measures["demucs"] = self.metrics["demucs_seconds"]
        analyzed = not saved or saved.get("refinement", {}).get("version") != REFINEMENT_VERSION
        if analyzed:
            self.journal.emit("TURBO", f"Analyse Turbo{' Vulkan' if nocturnal else ''} · {song['title']}")
            with tempfile.TemporaryDirectory(prefix="prepare-", dir=self.index.folder) as workspace:
                (Path(workspace) / ".parole-stage").touch()
                command = ["nice", "-n", "12", sys.executable, "-B", str(BIN / "_kara_prepare.py"),
                           "--cache", str(self.cache.root), "--local-only"]
                if nocturnal:
                    command.extend(("--device", "gpu", "--require-vulkan", "--observed-corrections-only"))
                if song["previous"] and not reference_option:
                    command.extend(("--previous-path", song["previous"]))
                if reference_option:
                    reference = Path(workspace) / "reference.txt"
                    reference.write_text(plain, encoding="utf-8")
                    command.extend((reference_option, str(reference), "--reference-provider", provider))
                    if rejected:
                        command.extend(("--rejected-lrc", document(rejected)))
                rc, _ = self.output([*command, "--", str(path)], 1800, status("TURBO"))
                if rc and not self.cache.cached(path, verify=False, insufficient=True):
                    return "temporary", "analyse Turbo indisponible", 6 * HOUR
        reference = self.cache.cached(path, verify=False)
        prepared_stems = (cached_stems(path) if reference and
                          reference.get("refinement", {}).get("version") == REFINEMENT_VERSION else None)
        if prepared_stems:
            if nocturnal:
                folder = self.cache.folder(path)
                try:
                    # Measure this complete generation, excluding locks,
                    # superseded drafts and unrelated changes to the cache.
                    files = (*prepared_stems, folder / "prepared.json", folder / (reference["lrc_sha256"] + ".lrc"))
                    self.night_measures["taille"] = sum(file.stat().st_blocks * 512 for file in files)
                except OSError:
                    pass
            updated = {"audio": reference["audio"].get("pcm_sha256", ""), "kara": "prêt"}
            origin = reference["source"]
            generated = bool(origin.get("plain_sha256") and reference_option == "--plain-reference")
            corrected = bool(reference["report"].get("changed") and analyzed)
            if origin.get("plain_sha256") and origin["kind"] == "local":
                updated["lrc"] = "généré"
                updated["provenance"] = {"lrc_provider": f"Turbo · {origin['provider']}"}
            if generated:
                self.journal.emit("LRC", f"LRC généré et validé · {song['title']}")
            if corrected:
                self.journal.emit("LRC", f"Minutage corrigé dans Kara · {song['title']}")
            self.refresh_song(song, **updated)
            self.night_lrc = (corrected, generated)
            self.journal.emit("KARA", f"Chanson prête · {song['title']}")
            return "done", "prêt", 0
        self.refresh_song(song)
        if (nocturnal and local and analyzed and
                self.cache.cached(path, verify=False, insufficient=True)):
            return "temporary", "LRC local insuffisant · recherche d’une autre référence", 10
        return "review", "référence Kara insuffisante", 0

    def do_codex(self, task):
        query = json.loads(task["payload"])
        self.journal.emit("CODEX", f"Recherche approfondie · {query['artist']} {query['title'] or query['album']}")
        try:
            result, error = research(self.index, query, self.current_settings, self.stopped,
                                     lambda text: self.journal.emit("CODEX", text))
        except PermissionError:
            return "temporary", "authentification isolée indisponible", DAY
        if result is None:
            self.journal.emit("CODEX", {"quota_local": "Limite quotidienne locale atteinte",
                                        "quota_distant": "Limite Codex signalée",
                                        "recherche_web_non_observée": "Recherche Web non confirmée"}[error], "WARNING")
            if error.startswith("quota"):
                with self.index.db() as db:
                    db.execute("INSERT OR REPLACE INTO settings VALUES('codex_suspended_until',?)",
                               (document(time() + (DAY if error == "quota_distant" else HOUR)),))
                return "temporary", error, HOUR
            return "temporary", error, min(DAY, 1800 * 2**min(task["attempts"], 5))
        if query["kind"] in ("adapter", "import_patch"):
            proposed = bool(result["adapter_python"])
            self.index.review("codex", task["subject"], "proposal" if proposed else "review", result)
            self.journal.emit("CODEX", "Ébauche et tests disponibles pour revue" if proposed else
                              "Proposition de code indisponible")
            return "review", "validation utilisateur nécessaire", 0
        self.index.review("codex", task["subject"], result["status"], result)
        if query["kind"] == "import_failure" and self.import_queue is not None:
            self.import_queue.reconsider_import(task["subject"])
            self.journal.emit("DOWNLOAD", "Diagnostic mutualisé disponible dans l’historique Codex")
        self.revision += 1
        self.journal.emit("CODEX", {"found": "Sources identifiées", "not_found": "Aucune source trouvée",
                                    "ambiguous": "Correspondance à vérifier"}[result["status"]])
        for source in result["sources"]:
            source_id, added = discover(self.index, source, task["subject"])
            if added:
                self.index.enqueue("source_probe", source_id, encoded_key(source_id), 40)
                self.journal.emit("SOURCE", "Adaptateur spécifique à examiner" if source["adapter"] == "custom" else
                                  "Fournisseur proposé · validation nécessaire")
        return ("done" if result["status"] == "found" else "review", result["status"], 0)

    def execute(self, task, *, nocturnal=False):
        started = monotonic()
        kind, subject = task["kind"], task["subject"]
        category = {"discography": "DISCOGRAPHIE", "prepare": "KARA", "source_probe": "SOURCE",
                    "cleanup": "CACHE", IMPORT_LYRICS: "LYRICS", IMPORT_COVER: "COVER"}.get(kind, kind.upper())
        with self.lock:
            self.interrupt.clear()
            if ((kind == "prepare" and (self.priority or monotonic() < self.priority_until))
                    or kind in IMPORT_ENRICHMENT and self.priority):
                stats = (dict(seconds=0., outcome="interrupted", cache_bytes=None, corrected=0, generated=0)
                         if nocturnal else None)
                self.index.finish(task, "suspended", "ressources prioritaires", 15, night=stats)
                return
            self.active = (kind, subject)
        self.night_running = nocturnal
        self.night_settings_at = 0.
        self.night_lrc = (False, False)
        self.night_measures = {}
        song = self.index.song(subject) if kind in ("lyrics", "lrc", "prepare") else None
        self.progress = (category, song["title"] if song is not None else "En cours", None)
        if kind not in ("prepare", "codex"):
            self.journal.emit(category, {"lyrics": "Recherche de paroles", "lrc": "Recherche d’un LRC",
                                        "album": "Vérification d’une édition", "discography": f"Vérification · {subject}",
                                        IMPORT_LYRICS: "Paroles et LRC des imports", IMPORT_COVER: "Pochette d’un import",
                                        "source": "Vérification des conditions et des correspondances",
                                        "source_probe": "Vérification de la documentation fournisseur",
                                        "cover": "Recherche de pochette", "metadata": "Vérification de l’identification", "cleanup": "Nettoyage des brouillons"}[kind]
                              + (f" · {song['title']}" if song is not None else ""))
        try:
            if kind == "prepare":
                if nocturnal:
                    if self.stopped():
                        raise Interrupted
                    result = self.prepare(task, nocturnal=True)
                else:
                    result = self.prepare(task)
            elif kind == "codex":
                result = self.do_codex(task)
            elif kind in IMPORT_ENRICHMENT:
                result = self.enrich_import(task)
            elif kind == "cleanup":
                removed = self.cleanup_interrupted()
                self.journal.emit("CACHE", f"{removed} dossiers intermédiaires retirés")
                result = ("done", "nettoyé", 0)
            else:
                _, data = self.output([sys.executable, "-B", str(BIN / "_parole_work.py"), kind, subject],
                                      timeout=220 if kind == "source" else self.current_settings["network_timeout"])
                value = json.loads(data)
                if kind in ("lyrics", "lrc"):
                    result = self.do_song(task, value)
                elif kind == "source_probe":
                    self.journal.emit("SOURCE", "Documentation accessible" if value["state"] == "available" else
                                      "Documentation ou conditions à vérifier")
                    result = ("done" if value["state"] == "available" else "review", value["state"], 0)
                elif kind == "cover":
                    outcome = value["state"]
                    if outcome == "not_found":
                        self.queue_codex("cover", subject, "aucune pochette vérifiée")
                    if outcome == "found":
                        self.journal.emit("COVER", "Pochette vérifiée et publiée")
                    result = ("temporary", outcome, DAY if outcome == "temporary" else 30 * DAY)
                elif kind == "source":
                    activated = value["state"] == "complete"
                    self.journal.emit("SOURCE", "Fournisseur validé et activé" if activated else "Vérification du fournisseur échouée",
                                      "INFO" if activated else "WARNING")
                    result = ("done" if activated else "review", value["state"], 0)
                else:
                    result = self.do_review(task, value)
        except Interrupted:
            self.journal.emit(category, "Traitement interrompu · reprise différée")
            result = ("suspended", "ressources libérées", 45)
            if nocturnal:
                self.night.close()
                if self.night.reason.startswith("Réouverture"):
                    self.resources.close()
                self.recover_night_drafts()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            self.journal.emit(category, "Moteur indisponible ou réponse invalide", "WARNING")
            result = (("review", "enrichissement partiel · maintenance complémentaire", 0)
                      if kind in IMPORT_ENRICHMENT and task["attempts"] >= 2 else
                      ("temporary", "opération indisponible", min(DAY, 300 * 2**min(task["attempts"], 8))))
        finally:
            with self.lock:
                if self.cancelled == self.active:
                    self.cancelled = None
                self.active = None
                self.interrupt.clear()
            self.progress = None
            self.night_running = False
        stats = None
        if nocturnal:
            state, detail, _ = result
            outcome = {"done": "ready", "review": "failed", "suspended": "interrupted"}.get(state, "deferred")
            if detail in ("séparation indisponible", "analyse Turbo indisponible", "opération indisponible"):
                outcome = "failed"
            corrected, generated = self.night_lrc
            storage = None
            elapsed = monotonic() - started
            try:
                self.resources.collect_storage(monotonic(), force=True)
                storage = self.resources.storage["bytes"]
            except (OSError, ValueError, subprocess.SubprocessError):
                pass
            stats = dict(seconds=round(elapsed, 3), cache_bytes=storage, outcome=outcome,
                         corrected=corrected, generated=generated, measurements=self.night_measures)
        self.index.finish(task, *result, night=stats)
        self.metrics[f"{kind}_last_seconds"] = round(monotonic() - started, 3)
        self.revision += 1

    def authorized_imports(self, settings):
        if (self.import_queue is None or not (settings["import_tracks"] or settings["import_albums"])
                or not self.index.source_enabled("YouTube Music")):
            return
        with self.index.db() as db:
            rows = db.execute("""SELECT * FROM import_permissions WHERE job IS NULL ORDER BY decided LIMIT 5""").fetchall()
            for row in rows:
                kind, subject = row["kind"], row["subject"]
                if not settings["import_tracks" if kind == "album" else "import_albums"]:
                    continue
                if db.execute("SELECT 1 FROM exclusions WHERE kind=? AND subject=?",
                              ("album" if kind == "album" else "release", subject)).fetchone():
                    continue
                if kind == "album":
                    matching = db.execute("SELECT state,value FROM reviews WHERE kind='album' AND subject=?",
                                          (subject,)).fetchone()
                    if not matching or matching["state"] != "incomplete" or not json.loads(matching["value"])["missing"]:
                        continue
                else:
                    if not any(any(release["id"] == subject for release in json.loads(check[0]).get("missing", []))
                               for check in db.execute("SELECT value FROM reviews WHERE kind='discography'")):
                        continue
                if not self.resources.allowed_storage(settings):
                    continue
                # A deterministic request identity reconciles a crash between
                # the import queue commit and the Parole receipt commit.
                request_id = encoded_key("parole", kind, subject, row["url"])
                job = self.import_queue.enqueue_maintenance(request_id, row["title"], row["url"])
                db.execute("UPDATE import_permissions SET job=? WHERE kind=? AND subject=?", (job, kind, subject))
                self.journal.emit("DOWNLOAD", f"Édition autorisée ajoutée à la file · {row['title']}")

    def admit_import(self, kind, subject, title, url):
        from _kara_import_queue import import_key
        from urllib.parse import urlsplit

        parsed = urlsplit(url)
        identity = import_key(url)
        if (kind not in ("album", "release") or parsed.scheme != "https"
                or parsed.netloc != "music.youtube.com" or parsed.fragment
                or identity[0] != "album" or not re.fullmatch(r"MPRE[A-Za-z0-9_-]{5,120}", identity[1])
                or parsed.path != "/browse/" + identity[1] or parsed.query):
            raise ValueError("Fiche d’édition YouTube Music attendue")
        with self.index.db() as db:
            db.execute("""INSERT INTO import_permissions VALUES(?,?,?,?,?,NULL)
                          ON CONFLICT(kind,subject) DO UPDATE SET url=excluded.url,title=excluded.title,
                          decided=excluded.decided,job=NULL WHERE url!=excluded.url""",
                       (kind, subject, title[:180], url, time()))
        self.wake.set()

    def loop(self):
        try:
            with lease(self.index.folder / "owner.lock", wait=False) as owner:
                if owner is None:
                    self.journal.emit("SYSTEM", "Maintenance déjà supervisée par une autre instance", "WARNING")
                    return
                self.owner = owner
                try:
                    if recovered := recover_artist_groups(self.index, self.import_queue,
                                                          **({"state": self.state_root} if self.state_root else {})):
                        self.journal.emit("ALBUM", f"{recovered} regroupement(s) repris")
                except (OSError, sqlite3.Error, ValueError):
                    self.journal.emit("ALBUM", "Regroupement interrompu à vérifier", "ERROR")
                if self.index.recover():
                    self.journal.emit("IDLE", "Préparation nocturne interrompue avant le redémarrage", "WARNING")
                    self.recover_night_drafts()
                self.journal.emit("SYSTEM", "Maintenance de Parole démarrée")
                while not self.shutdown.is_set():
                    try:
                        self.tick()
                    except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
                        self.night.close()
                        self.journal.emit("ERROR", "État de la maintenance indisponible, nouvelle tentative différée", "ERROR")
                        self.wake.wait(15)
                    if not self.shutdown.is_set():
                        self.wake.wait(1.5)
                    self.wake.clear()
        finally:
            self.owner = None
            self.night.close()
            self.resources.close()
            self.journal.close()

    def cleanup_interrupted(self):
        from shutil import rmtree

        count = 0
        # Only our explicitly marked work directories are removable. Demucs
        # owns this lease across the entire publication window.
        if STEM_ROOT.is_dir():
            with lease(STEM_ROOT / "demucs.lock", wait=False) as slot:
                if slot is not None:
                    for path in STEM_ROOT.glob("processing-*"):
                        if path.is_dir() and not path.is_symlink() and (path / ".parole-stage").is_file():
                            rmtree(path)
                            count += 1
        for prefix in ("prepare-", "codex-"):
            for path in self.index.folder.glob(prefix + "*"):
                if path.is_dir() and not path.is_symlink() and (path / ".parole-stage").is_file():
                    rmtree(path)
                    count += 1
        self.resources.collect_storage(monotonic(), force=True)
        return count

    def recover_night_drafts(self):
        # The interrupted process group has exited. The existing cleanup
        # takes the Demucs lease before deleting marked scratch directories.
        try:
            removed = self.cleanup_interrupted()
        except (OSError, ValueError, subprocess.SubprocessError):
            self.journal.emit("CACHE", "Nettoyage des brouillons reporté", "WARNING")
        else:
            if removed:
                self.journal.emit("CACHE", f"{removed} brouillons interrompus retirés")

    def tick(self):
        settings = self.current_settings = self.index.settings()
        now = time()
        if settings["paused"]:
            self.night.close()
            self.resources.close()
            return
        policy = tuple(settings[name] for name in NIGHT_RESTART_SETTINGS)
        if policy != self.night_policy or not settings["night_enabled"] or self.priority:
            self.night.close()
        if policy != self.night_policy:
            self.resources.sample = 0.
        self.night_policy = policy
        imports_pending = self.import_queue and self.import_queue.pending()
        if imports_pending and self.night.fd is not None:
            self.night.close()
            self.resources.close()
        if (not self.foreground.is_set() and not settings["night_enabled"] and not imports_pending
                and not enrichment_pending(self.index, now)):
            return
        if settings["night_enabled"] and not imports_pending and self.night.fd is not None and self.tick_night(settings, now):
            return
        if self.scan_now.is_set() or settings["startup"] and now >= self.next_scan:
            self.scan_now.clear()
            self.next_scan = now + max(1, settings["scan_minutes"]) * 60
            self.scan(settings)
            return
        if self.priority:
            return
        if self.import_diagnostics:
            self.import_diagnostics.tick(settings, now)
        if self.tick_import_enrichment(settings, now):
            return
        if settings["night_enabled"] and not imports_pending and self.tick_night(settings, now):
            return
        if not self.foreground.is_set():
            return
        if not settings["night_enabled"]:
            self.resources.check(settings)
        else:
            # Classical tasks retain their storage reserve even on an evening
            # in which the nocturnal governor never admits heavy work.
            self.resources.collect_storage(monotonic())
        local = self.index.claim(("cleanup",), now)
        if local:
            self.execute(local)
            return
        if self.block != self.resources.reason:
            self.block = self.resources.reason
            self.journal.emit("IDLE", self.block)
        self.authorized_imports(settings)
        if (not imports_pending and not settings["night_enabled"] and settings["autoprepare"] and self.resources.can_start
                and monotonic() >= self.priority_until and not self.priority):
            task = self.index.claim(("prepare",), now, bucket="heavy", limit=settings["heavy_daily"])
            if task is not None:
                self.execute(task)
                return

        if now < self.next_network or not self.resources.allowed_storage(settings):
            return
        flags = {"album": "albums", "source": None, "source_probe": None}
        kinds = [kind for kind in NETWORK if flags.get(kind, kind) is None or settings[flags.get(kind, kind)]]
        if (not settings["codex"] or now < settings.get("codex_suspended_until", 0)) and "codex" in kinds:
            kinds.remove("codex")
        manual = self.index.claim(("codex",), now, high=True, codex_auto=settings["codex_auto"]) if "codex" in kinds else None
        if manual is None:
            manual = self.index.claim(tuple(kind for kind in kinds if kind != "codex"), now, bucket="network",
                                      limit=settings["network_daily"], high=True, gap=settings["network_interval"])
        if manual is not None:
            self.next_network = now + settings["network_interval"]
            self.execute(manual)
            return
        # Stagger task classes so a large burst of missing LRC never starves
        # album checks or source validation for the entire library.
        turn = ("lrc", "lyrics", "album", "lrc", "discography", "lyrics", "cover", "metadata", "codex", "source", "source_probe")
        for _ in turn:
            kind = turn[self.network_cursor]
            self.network_cursor = (self.network_cursor + 1) % len(turn)
            if kind not in kinds:
                continue
            task = self.index.claim((kind,), now, bucket="network" if kind != "codex" else None,
                                    limit=settings["network_daily"], codex_auto=settings["codex_auto"],
                                    gap=settings["network_interval"])
            if task is not None:
                self.next_network = now + settings["network_interval"]
                self.execute(task)
                return

    def tick_night(self, settings, now):
        if not self.index.preparation_due(now):
            self.night.close()
            # Keep observing actual resume even after the last song or the
            # awake budget spent. A later idle interval has its own budget.
            self.resources.session_idle(settings["night_idle"])
            if self.resources.resumed:
                self.resources.resumed = False
                self.night.activity()
            return False
        if not self.night_allowed(settings, running=False):
            if (self.night.fd is not None and monotonic() < self.night_handover
                    and self.night.reason in {"CPU occupé", "Disque occupé", "GPU occupé ou non mesurable"}):
                return True
            self.night.close()
            return False
        if not self.night.open(settings):
            self.night.close()
            return False
        task = self.index.claim(("prepare",), now,
                                night=(self.night.budget, self.resources.storage.get("bytes", 0)))
        if task is None:
            self.night.close()
            return False
        self.execute(task, nocturnal=True)
        self.night_handover = monotonic() + 4.
        if not self.index.preparation_due(time()):
            self.night.close()
        return True
