"""Ordonnanceur de maintenance rattaché au propriétaire GTK du service Kara."""
from collections import deque
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from contextlib import nullcontext
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
from _kara_files import file_token
from _kara_stems import ROOT as STEM_ROOT
from _parole_codex import KINDS, research
from _parole_artist_groups import recover as recover_artist_groups
from _parole_index import (ARTWORK, LOCAL_REFERENCE_MISS, REFERENCE_REUSE_SECONDS, Index, affected_tracks,
                           document, encoded_key, inventory, path_rows, sidecar)
from _parole_index_watch import Watch
from _parole_import_diagnostics import ImportDiagnostics
from _parole_import_enrichment import COVER as IMPORT_COVER, KINDS as IMPORT_ENRICHMENT, LYRICS as IMPORT_LYRICS
from _parole_import_enrichment import cancel_artist, paths_for as enrichment_paths
from _parole_import_enrichment import schedule as schedule_enrichment
from _parole_lexicon import refresh_one as refresh_lexicon
from _parole_local_reference import SOURCE as LOCAL_SOURCE
from _parole_night import Night
from _parole_process import Interrupted, run
from _parole_resources import Resources
from _parole_sources import discover, seed, controls, ready_categories, night_lrc_providers
from _paroles_lrc import MAX_LRC_BYTES

CATEGORIES = ("SYSTEM", "SCAN", "INDEX", "LYRICS", "LRC", "ALBUM", "DISCOGRAPHIE", "DOWNLOAD", "METADATA",
              "COVER", "CODEX", "SOURCE", "DEMUCS", "TURBO", "KARA", "TV", "SYNC", "IDLE", "CACHE",
              "SUCCESS", "WARNING", "ERROR", "TRANSCRIPTION", "RESSOURCES")
HEAVY = ("prepare", "transcription")
NIGHT_NETWORK = ("lyrics", "lrc", "album", "discography", "cover", "metadata")
NETWORK = (*NIGHT_NETWORK, "codex", "source", "source_probe")
NETWORK_FLAGS = {"album": "albums", "source": None, "source_probe": None}
NETWORK_CATEGORY = {"lyrics": "lyrics", "lrc": "lrc", "album": "musicbrainz", "metadata": "musicbrainz",
                    "discography": "musicbrainz", "cover": "cover"}
NETWORK_TURN = ("lrc", "lyrics", "album", "lrc", "discography", "lyrics", "cover", "metadata", "codex", "source", "source_probe")
CHECKED_RESULTS = {"complete", "incomplete", "checked", "partial", "identified", "found", "not_found",
                   "invalid", "ambiguous", "instrumental", "present"}
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
    "cache_gib", "reserve_gib", "paused",
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
        self.resources = Resources(self.index.root)
        self.journal = Journal(self.index.folder)
        self.night = Night(self.index, self.journal)
        self.night_running = False
        self.night_settings_at = 0.
        self.next_night_watch = 0.
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
        self.index_wake = Event()
        self.lyrics_wake = Event()
        self.index_progress = None
        self.priority = False
        self.index_priority = False
        self.thread = None
        self.active = None
        self.progress = None
        self.block = ""
        self.cancelled = None
        self.priority_until = 0.
        self.next_scan = time() + 3
        self.plan_policy = None
        self.next_stats_audit = 0.
        self.index_constraint = ""
        self.network_cursor = 0
        self.network_width = 2
        self.local_reference_next = True
        self.network_active = {}
        self.import_enrichment_active = {}
        self.import_activity = (0, 0)
        self.next_enrichment_delivery = 0.
        self.import_lyrics_stats = None
        self.import_lyrics_live = {}
        self.import_lyrics_automatic = True
        self.import_lyrics_preempted = False
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
        self.index_wake.set()
        self.lyrics_wake.set()
        if self.thread is None:
            self.journal.close()

    def set_priority(self, busy, *, kara=None):
        with self.lock:
            self.priority = busy
            self.index_priority = busy if kara is None else kara
            if busy and self.active and self.active[0] in HEAVY:
                self.interrupt.set()
            if busy:
                # Only artwork retains the playback policy in these lanes.
                # Lyric workers publish sidecars and never replace the audio.
                for (kind, _), cancel in self.import_enrichment_active.items():
                    if kind != IMPORT_LYRICS:
                        cancel.set()
        if busy and self.active:
            self.wake.set()

    def yield_heavy(self):
        # Mobile/organizer callbacks may retry as soon as our process group
        # exits. The GTK thread does not wait on a decoder or GPU driver.
        with self.lock:
            self.priority_until = monotonic() + 5
            if self.active and self.active[0] in HEAVY:
                self.interrupt.set()
                return False
        return True

    def rescan(self):
        self.scan_now.set()
        self.index_wake.set()

    def pause(self, paused):
        self.index.configure(paused=paused)
        if paused:
            self.interrupt.set()
            with self.lock:
                for cancel in self.import_enrichment_active.values():
                    cancel.set()
        self.wake.set()
        self.index_wake.set()
        self.journal.emit("SYSTEM", "Maintenance suspendue" if paused else "Maintenance reprise")

    def show(self):
        self.foreground.set()
        self.wake.set()
        self.index_wake.set()

    def cancel(self, kind, subject):
        self.index.cancel(kind, subject)
        with self.lock:
            event = self.network_active.get((kind, subject)) or self.import_enrichment_active.get((kind, subject))
            if event:
                event.set()
            if self.active == (kind, subject):
                self.cancelled = self.active
                self.interrupt.set()
        self.wake.set()

    def cancel_artist_lyrics(self, request):
        subjects = cancel_artist(self.index, request)
        with self.lock:
            for subject in subjects:
                active = self.import_enrichment_active.get((IMPORT_LYRICS, subject))
                if active:
                    active.set()
        self.wake.set()

    def prioritize_artist_lyrics(self, subjects):
        # An artist request may reuse the active import. Otherwise yield
        # the same lane after terminating its existing provider process.
        with self.lock:
            for (kind, subject), cancel in self.import_enrichment_active.items():
                if kind == IMPORT_LYRICS and subject not in subjects and self.import_lyrics_automatic:
                    self.import_lyrics_preempted = True
                    cancel.set()
        self.lyrics_wake.set()

    def artist_lyrics_live(self, subjects):
        with self.lock:
            return {path: detail for subject in subjects
                    for path, detail in self.import_lyrics_live.get(subject, {}).items()}

    @staticmethod
    def inspect_cache(_path, old, token, side):
        # Keep the historical cache receipt when its song and sidecars are
        # unchanged. The library indexer does not inspect or build AI caches.
        if old and old["token"] == token and old["side"] == side:
            return old["cache"], old["kara"], json.loads(old["info"]).get("report", {})
        return "", "absent", {}

    def stopped(self):
        if self.shutdown.is_set() or self.interrupt.is_set():
            return True
        if self.night_running:
            if self.import_queue and monotonic() >= self.next_import_probe:
                self.next_import_probe = monotonic() + 1.
                if self.import_queue.pending() or self.import_enrichment_count():
                    self.night.report("Préparation nocturne suspendue · imports prioritaires")
                    return True
            return not self.night_allowed(self.current_settings, running=True, light=self.active[0] not in HEAVY)
        settings = getattr(self, "current_settings", None)
        if settings and settings["night_enabled"] and monotonic() >= self.next_night_watch:
            # Network workers and inventory may span many ticks. Keep power
            # supervision live without starting a second maintenance owner.
            self.next_night_watch = monotonic() + .5
            try:
                self.night.refresh(self.index.settings(wait=.15), fast=True)
            except (OSError, ValueError, sqlite3.Error):
                self.night.close(reason="réglages indisponibles")
        if self.active and self.active[0] in HEAVY:
            now = monotonic()
            if self.import_queue and now >= self.next_import_probe:
                self.next_import_probe = now + 1.
                if self.import_queue.pending() or self.import_enrichment_count():
                    self.interrupt.set()
                    return True
            allowed = self.resources.check(self.current_settings,
                                           busy=self.priority or monotonic() < self.priority_until, running=True)
            if not allowed:
                self.log_memory_stop()
            return not allowed
        return False

    def log_memory_stop(self):
        if self.resources.memory_detail:
            # Journal the sample that revoked admission before process
            # cleanup changes memory usage. The queue keeps its retry state.
            phase = self.progress[0] if self.progress else "Maintenance"
            self.journal.emit("RESSOURCES", f"{phase} suspendu · {self.resources.memory_detail}", "WARNING")
            self.memory_interruption = True

    def night_allowed(self, settings, *, running, light=False):
        now = monotonic()
        if running and now >= self.night_settings_at:
            self.night_settings_at = now + .5
            try:
                fresh = self.index.settings(wait=.15)
            except (OSError, sqlite3.Error, ValueError):
                return self.night.suspend("réglages indisponibles", running=True)
            if not fresh["night_enabled"] or fresh["paused"]:
                return self.night.refresh(fresh, running=True)
            if self.night.lid_fd is not None and not (fresh["night_lid"] and fresh["night_lid_qualified"]):
                return self.night.suspend("option capot fermé ou qualification thermique désactivée", running=True)
            if any(settings[name] != fresh[name] for name in NIGHT_RESTART_SETTINGS):
                return self.night.report("Préparation nocturne suspendue · réglages modifiés")
        idle = self.resources.session_idle(settings["night_idle"])
        if self.resources.resumed:
            self.resources.resumed = False
            self.night.activity()
            if running and self.night.desktop and not self.night.desktop.lid:
                return self.night.report("Reprise d’activité · préparation nocturne suspendue")
        if not self.night.check(settings, busy=self.priority or now < self.priority_until, running=running):
            return False
        if not (idle or self.night.desktop.lid):
            return self.night.report("Préparation nocturne en attente d’inactivité")
        if not self.resources.check(settings, running=running, nocturnal=True, lid=self.night.desktop.lid, light=light):
            if running and not light:
                self.log_memory_stop()
            return self.night.report(self.resources.reason)
        if running:
            self.night.report("Mode nocturne actif · traitement en cours")
        return True

    def changed(self):
        with self.lock:
            self.revision += 1

    def index_stopped(self):
        return self.shutdown.is_set() or self.index_priority or monotonic() < self.priority_until

    def scan(self, settings, *, limit=None, replan=False):
        started = monotonic()
        self.index_progress = 0
        self.journal.emit("INDEX", "Réconciliation de la bibliothèque")
        next_delivery = started
        yield_delivery = False

        def stopped():
            nonlocal next_delivery, yield_delivery
            if yield_delivery or self.index_stopped() or self.index_limit(settings):
                return True
            tick = monotonic()
            if tick < next_delivery:
                return False
            next_delivery = tick + .25
            # The same worker yields a full sweep to ready publications.
            # A retry with a bad file must not starve reconciliation.
            with self.index.db(wait=.15) as db:
                yield_delivery = db.execute("SELECT 1 FROM index_events WHERE due<=? AND attempts=0 LIMIT 1",
                                            (time(),)).fetchone() is not None
            return yield_delivery

        try:
            args = () if self.state_root is None else (self.state_root,)
            associations = self.index.associations(*args)
            result = inventory(self.index, self.collection, self.inspect_cache, stopped,
                               lambda scanned: setattr(self, "index_progress", scanned), limit=limit)
            elapsed = monotonic() - started
            self.metrics.update(result, scan_seconds=elapsed, associations=associations)
            self.journal.emit("INDEX", f"{result['scanned']} vérifiées · {result['unchanged']} inchangées · "
                              f"{result['changed']} mises à jour · {result['missing']} disparues · "
                              f"{result['errors']} erreurs · {elapsed:.2f} s · {result['taglib']} TagLib · {result['probes']} FFprobe")
            changed = result["changed"] or result["missing"]
            if changed:
                self.changed()
            if not stopped() and time() >= self.next_stats_audit:
                before_stats = monotonic()
                if not self.index.audit_dimensions():
                    self.journal.emit("INDEX", "Écart détecté dans les agrégats de bibliothèque", "WARNING")
                self.metrics["index_stats_seconds"] = monotonic() - before_stats
                self.next_stats_audit = time() + DAY
            policy = tuple(settings[name] for name in ("lyrics", "lrc", "metadata", "albums", "discography", "cover", "night_enabled", "releases"))
            if not stopped() and (changed or replan or self.plan_policy != policy):
                before_plan = monotonic()
                self.plan(settings)
                self.metrics["plan_seconds"] = monotonic() - before_plan
                if not stopped():
                    self.plan_policy = policy
        finally:
            if stopped():
                self.next_scan = min(self.next_scan, time() + 10)
                if replan:
                    self.scan_now.set()
            self.index_progress = None

    def index_loop(self):
        # One consumer in the existing maintenance lease owns both the
        # event queue and the periodic reconciliation. Provider work never
        # holds up its delivery, and GTK only signals its wake event.
        # On Linux a thread can lower its own scheduling priority. This also
        # applies to the one FFprobe it can launch at a time.
        try:
            os.setpriority(os.PRIO_PROCESS, 0, max(5, os.getpriority(os.PRIO_PROCESS, 0)))
        except OSError:
            pass
        watch = Watch(self.index)
        try:
            while not self.shutdown.is_set():
                self.index_wake.clear()
                try:
                    delay = self.index_turn(watch)
                except (OSError, sqlite3.Error, ValueError) as error:
                    self.journal.emit("INDEX", f"Indexation différée · {type(error).__name__}", "WARNING")
                    delay = 5
                self.index_wake.wait(delay)
        finally:
            watch.close()

    def lexicon_loop(self):
        # Only this low-priority worker tokenizes text. Index transactions
        # enqueue versioned work and leave provider or audio workers running.
        try:
            os.setpriority(os.PRIO_PROCESS, 0, max(8, os.getpriority(os.PRIO_PROCESS, 0)))
        except OSError:
            pass
        while not self.shutdown.is_set():
            try:
                settings = self.index.settings(wait=.15)

                def stopped():
                    return (self.shutdown.is_set() or settings["paused"] or self.night_context(settings)
                            or bool(self.index_limit(settings)))

                if refresh_lexicon(self.index, time(), stopped):
                    continue
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                self.journal.emit("LYRICS", "Lexique temporairement indisponible", "WARNING")
                self.shutdown.wait(5)
            self.shutdown.wait(.5)

    def index_turn(self, watch=None):
        if self.index_stopped():
            return .5
        settings = self.index.settings(wait=.15)
        if settings["paused"]:
            return .5
        now = time()
        limit = self.index_limit(settings)
        if limit != self.index_constraint:
            self.index_constraint = limit
            if limit:
                self.journal.emit("INDEX", f"Traitement léger · {limit}")
        if watch is not None:
            changes, gap = watch.poll()
            if gap:
                self.scan_now.set()
                self.journal.emit("INDEX", watch.reason or "Surveillance des dossiers rétablie")
            if changes:
                try:
                    with self.index.db(wait=.15) as db:
                        for mask in sorted(set(changes.values())):
                            self.index.notify((path for path, value in changes.items() if value == mask), mask, db=db)
                except sqlite3.Error:
                    # The kernel batch was consumed. Recover it with the same
                    # reconciliation used for an overflow.
                    self.scan_now.set()
                    raise
        backlog, events = self.index.events(now, light=bool(limit))
        self.metrics["index_pending"] = backlog
        if events:
            started = monotonic()
            result = inventory(self.index, self.collection, self.inspect_cache, self.index_stopped,
                               lambda *_: None, events=events, allow_probe=not limit,
                               plan_events=lambda db, paths: self.plan_events(db, settings, paths))
            duration = monotonic() - started
            self.metrics.update(index_last=result, index_seconds=duration)
            if result["changed"] or result["missing"]:
                self.changed()
            delivered = result["delivered"]
            if delivered:
                self.journal.emit("INDEX", f"{result['changed']} intégrées · {result['changed']/max(duration,.001):.1f} pistes/s · "
                                  f"{result['unchanged']} inchangées · {result['combined']} regroupées · "
                                  f"{result['receipts']+result['reused']} métadonnées réutilisées · "
                                  f"{result['taglib']} TagLib · {result['probes']} FFprobe · "
                                  f"retard {result['lag']/delivered:.2f} s · {backlog} en file à l’admission · "
                                  f"SQLite {result['phase_seconds'].get('sqlite_lock',0)*1000:.1f} ms")
                self.wake.set()
            return 0. if backlog > len(events) and delivered else .25
        if not limit and (self.scan_now.is_set() or settings["startup"] and now >= self.next_scan and
                                  (not backlog or now >= self.next_scan + 30)):
            replan = self.scan_now.is_set()
            self.scan_now.clear()
            self.next_scan = now + max(1, settings["scan_minutes"]) * 60
            self.scan(settings, replan=replan)
        return .25 if backlog else .5

    def index_limit(self, settings):
        if self.priority:
            return "lecture Strawberry prioritaire"
        active = self.active
        if active and active[0] in HEAVY:
            return "préparation ou transcription en cours"
        sample = self.resources.index_pressure
        if sample and monotonic() - sample[0] < 5:
            _, cpu, memory, psi, disk = sample
            if memory < settings["running_memory_mib"] or psi >= 1:
                return "pression mémoire"
            if cpu is not None and cpu >= max(60, settings["cpu"]):
                return "CPU occupé"
            if disk is not None and disk >= max(60, settings["disk_busy"]):
                return "stockage occupé"
        return ""

    def plan_tracks(self, db, settings, songs):
        now = time()
        # Queue cursors are stable song/edition identities, independent
        # from filter, pagination and GTK selection.
        blocked = {row[0] for row in db.execute("SELECT subject FROM exclusions WHERE kind='artist'")}
        for song in songs:
            revision = encoded_key(song["token"], song["side"], song["title"], song["artist"], song["edition"])
            subject = song["id"]
            if song["owner"] in blocked:
                db.execute("UPDATE tasks SET state='excluded' WHERE subject=? AND state!='running'", (subject,))
                continue
            tags = json.loads(song["info"])["tags"]
            missing_tags = any(not tags.get(field) for field in ("title", "artist", "album"))
            for kind, missing in (("lyrics", song["lyrics"] == "absent"), ("lrc", song["lrc"] in ("absent", "intégré")
                                   or settings["night_enabled"] and not json.loads(song["side"] or "[null]")[0]),
                                  ("metadata", missing_tags)):
                if not missing:
                    db.execute("UPDATE tasks SET state='done' WHERE kind=? AND subject=? "
                               "AND state IN ('queued','temporary','suspended')", (kind, subject))
                    continue
                priority = 25 if song["plays"] else 35
                if now - song["added"] <= DAY and song["previous"] is None:
                    priority = 20
                if kind == "lrc":
                    priority -= 1
                if settings.get(kind) or kind == "lrc" and settings["night_enabled"]:
                    self.index.enqueue(kind, subject, revision, priority, db=db)

    @staticmethod
    def group_kinds(settings):
        return tuple((kind, column, priority) for kind, column, priority in (
            ("album", "edition", 42), ("discography", "owner", 45), ("cover", "edition", 46))
            if settings["albums" if kind == "album" else kind])

    def plan_group(self, db, settings, kind, column, priority, names):
        if not names:
            return
        marks = ",".join("?" for _ in names)
        entries = db.execute(f"""SELECT {column} AS subject, max(added) AS revision, count(*) AS tracks,
                   max(owner IN (SELECT subject FROM exclusions WHERE kind='artist')) AS blocked
                   FROM songs WHERE album!='' AND {column} IN ({marks}) GROUP BY {column}""", names)
        for row in entries:
            subject = row["subject"]
            if row["blocked"] or db.execute("SELECT 1 FROM exclusions WHERE kind=? AND subject=?", (kind, subject)).fetchone():
                db.execute("UPDATE tasks SET state='excluded' WHERE kind=? AND subject=? AND state!='running'",
                           (kind, subject))
                continue
            revision = encoded_key(row["revision"], row["tracks"], settings["releases"])
            self.index.enqueue(kind, subject, revision, priority, db=db)

    def plan_events(self, db, settings, paths):
        songs = list(path_rows(db, paths).values())
        self.plan_tracks(db, settings, songs)
        for kind, column, priority in self.group_kinds(settings):
            names = sorted({song[column] for song in songs if song["album"]})
            self.plan_group(db, settings, kind, column, priority, names)

    def plan(self, settings, *, paths=None):
        if paths is not None:
            with self.index.db() as db:
                db.execute("BEGIN IMMEDIATE")
                self.plan_events(db, settings, paths)
            return
        # Long reconciliations must release the SQLite writer between pages.
        # The immutable song ID is the cursor, including across a rename.
        after = ""
        while not self.index_stopped():
            with self.index.db() as db:
                db.execute("BEGIN IMMEDIATE")
                songs = db.execute("SELECT * FROM songs WHERE id>? ORDER BY id LIMIT 128", (after,)).fetchall()
                if not songs:
                    break
                self.plan_tracks(db, settings, songs)
                after = songs[-1]["id"]
        for kind, column, priority in self.group_kinds(settings):
            after = ""
            while not self.index_stopped():
                with self.index.db() as db:
                    db.execute("BEGIN IMMEDIATE")
                    names = [row[0] for row in db.execute(f"""SELECT DISTINCT {column} FROM songs
                             WHERE {column}>? AND album!='' ORDER BY {column} LIMIT 128""", (after,))]
                    if not names:
                        break
                    self.plan_group(db, settings, kind, column, priority, names)
                    after = names[-1]
        if self.index_stopped():
            return
        with self.index.db() as db:
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind IN ('lyrics','lrc','prepare','metadata','transcription')
                         AND state NOT IN ('excluded','running') AND NOT EXISTS(SELECT 1 FROM songs WHERE id=tasks.subject)""")
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind IN ('album','cover')
                         AND state NOT IN ('excluded','running') AND NOT EXISTS(SELECT 1 FROM songs WHERE edition=tasks.subject)""")
            db.execute("""UPDATE tasks SET state='excluded' WHERE kind='discography'
                         AND state NOT IN ('excluded','running') AND NOT EXISTS(SELECT 1 FROM songs WHERE owner=tasks.subject)""")

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
        # Track searches use the persisted edition review. Ordinary
        # maintenance queries can fall back to the release tag in Strawberry.
        release = json.loads(review["value"]) if review else {}
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
        path = Path(song["path"])
        inventory(self.index, self.collection, self.inspect_cache, self.shutdown.is_set,
                  lambda *_: None, paths=[path])
        fresh = self.index.song(song["id"])
        if fresh and changes and fresh["path"] == str(path) and document(file_token(path)) == fresh["token"]:
            details = json.loads(fresh["info"])
            details.update(changes.pop("provenance", {}))
            self.index.update(song["id"], expected=fresh, lexicon="lrc" in changes, checked=time(),
                              info=document(details), **changes)
        self.changed()

    def do_song(self, task, result):
        identity, kind = task["subject"], task["kind"]
        song = self.index.song(identity)
        if song is None:
            return "excluded", "disparu", 0
        outcome = result["state"]
        if outcome == "rescan":
            self.index.notify([song["path"]])
            self.index_wake.set()
            return "temporary", outcome, 10
        if outcome == "found":
            provider = result["source"]
            # The child already delivered the verified file to V2. Network
            # settlement accounts for the result without waiting on inventory.
            self.index_wake.set()
            if provider == LOCAL_SOURCE:
                self.journal.emit(kind.upper(), f"Référence PCM locale intégrée · {song['title']}")
                return "done", "référence PCM locale", 0
            self.journal.emit("SUCCESS", f"{song['title']} · {'LRC' if kind == 'lrc' else 'Paroles'} vérifiés")
        elif outcome == "instrumental":
            self.index.update(identity, lyrics="instrumental", checked=time())
            self.changed()
        elif outcome == "present":
            self.index.notify([song["path"]])
            self.index_wake.set()
        elif outcome == "local_miss":
            return "temporary", LOCAL_REFERENCE_MISS, 0
        elif outcome in ("temporary", "network", "unavailable", "limited"):
            msg = {"temporary": "Publication ou moteur temporairement indisponible",
                   "network": "Erreur réseau", "unavailable": "Fournisseur indisponible",
                   "limited": "Limitation du fournisseur"}[outcome]
            if outcome != "limited":
                self.journal.emit(kind.upper(), msg, "WARNING")
            else:
                # Every task still records its own deferred outcome and
                # counters, but the same provider episode should not flood
                # the console once for every song.
                notices = getattr(self, "provider_notices", {})
                now = monotonic()
                if now >= notices.get(kind, -60.) + 60.:
                    self.journal.emit(kind.upper(), msg + " · recherches différées selon les quotas", "WARNING")
                    notices[kind] = now
                self.provider_notices = notices
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
        self.changed()
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

    def output(self, command, timeout=140, observer=None, maximum=262_144, *, extra_env=None, stop=None,
               import_owner=None):
        env = dict(os.environ, KARA_MUSIC_LIBRARY=str(self.index.root))
        if extra_env:
            env.update(extra_env)
        if self.state_root is not None:
            env["XDG_STATE_HOME"] = str(self.state_root)
        if self.night_running and import_owner is None:
            # The nocturnal governor has checked the locked session, power
            # and activity. Use the same compute budget with either lid state.
            env["KARA_DEMUCS_THREADS"] = "12"
        # All input paths are program arguments; no shell evaluates titles or paths.
        retain = tuple(fd for fd in (self.owner, import_owner) if fd is not None)
        return run(command, stop=self.stopped if stop is None else stop, timeout=timeout, event=observer, env=env, maximum=maximum,
                   retain=retain)

    def network_output(self, task, settings, *, stop=None, local_reference_only=False):
        kind, subject = task["kind"], task["subject"]
        command = [sys.executable, "-B", str(BIN / "_parole_work.py"), kind, subject]
        if self.night_running:
            command.append("--original-lrc")
        elif local_reference_only:
            command.append("--local-reference-only")
        code, data = self.output(command,
                                 timeout=220 if kind == "source" else settings["network_timeout"], stop=stop)
        if code:
            raise ValueError("Moteur réseau arrêté")
        return json.loads(data)

    def network_result(self, task, value):
        kind = task["kind"]
        outcome = value["state"]
        if kind in ("lyrics", "lrc"):
            return self.do_song(task, value)
        if kind == "cover":
            if outcome == "not_found":
                self.queue_codex("cover", task["subject"], "aucune pochette vérifiée")
            if outcome == "found":
                self.journal.emit("COVER", "Pochette vérifiée et publiée")
            return "temporary", outcome, DAY if outcome == "temporary" else 30 * DAY
        if kind == "source_probe":
            self.journal.emit("SOURCE", "Documentation accessible" if outcome == "available" else
                              "Documentation ou conditions à vérifier")
            return ("done" if outcome == "available" else "review"), outcome, 0
        if kind == "source":
            self.journal.emit("SOURCE", "Fournisseur validé et activé" if outcome == "complete" else
                              "Vérification du fournisseur échouée", "INFO" if outcome == "complete" else "WARNING")
            return ("done" if outcome == "complete" else "review"), outcome, 0
        return self.do_review(task, value)

    def finish_network(self, task, future, window):
        publication = False
        activity = ()
        try:
            value = future.result()
            result = self.network_result(task, value)
            publication = value["state"] == "found" and value.get("source") not in (None, "Strawberry", LOCAL_SOURCE)
            activity = self.reference_activity(task, value, result)
        except Interrupted:
            result = "suspended", "ressources libérées", 45
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            self.journal.emit(task["kind"].upper(), "Moteur indisponible ou réponse invalide", "WARNING")
            result = "temporary", "opération indisponible", min(DAY, 300 * 2**min(task["attempts"], 8))
        self.index.finish(task, *result, closed=self.closed_outcome(window, result), network_found=publication,
                          activity=activity)
        self.changed()
        return self.network_completed(result)

    @staticmethod
    def network_completed(result):
        state, detail, _ = result
        return state != "suspended" and (state != "temporary" or detail in CHECKED_RESULTS)

    @staticmethod
    def reference_activity(task, value, result):
        kind = task["kind"]
        if kind in ("lyrics", "lrc"):
            if value["state"] == "found" and result[0] == "done" and value.get("source") in ("Strawberry", LOCAL_SOURCE):
                return (f"local_{kind}",)
            if value["state"] == "limited" and result[0] == "temporary":
                return ("quota",)
        return ()

    @staticmethod
    def closed_outcome(window, result):
        if window is not None:
            state, detail, _ = result
            outcome = ("interrupted" if state == "suspended" else "completed"
                       if state in ("done", "review", "excluded") or detail in CHECKED_RESULTS else "deferred")
            return window, outcome
        return None

    def network_batch(self, tasks, settings, *, nocturnal):
        stop = Event()
        window = self.night.window if nocturnal else None
        successful = 0
        with self.lock:
            self.active = ("network", "")
            self.interrupt.clear()
            self.network_active = {(task["kind"], task["subject"]): Event() for task in tasks}
        self.night_running = nocturnal
        self.night_settings_at = 0.
        self.journal.emit("SYSTEM", f"Recherches classiques en parallèle · {len(tasks)} tâches")
        try:
            if self.stopped():
                stop.set()
            with ThreadPoolExecutor(max_workers=len(tasks), thread_name_prefix="parole-recherche") as pool:
                # Only the maintenance owner changes queue state and
                # supervises the shared resource governor. Each thread
                # merely runs one bounded engine with its own stop event.
                pending = {}
                for task in tasks:
                    event = self.network_active[task["kind"], task["subject"]]
                    job = pool.submit(self.network_output, task, settings,
                                      stop=lambda event=event: stop.is_set() or event.is_set())
                    pending[job] = task
                try:
                    while pending:
                        if not stop.is_set() and self.stopped():
                            stop.set()
                        completed, _ = wait(pending, timeout=.15, return_when=FIRST_COMPLETED)
                        for job in sorted(completed, key=lambda item: (pending[item]["kind"], pending[item]["subject"])):
                            task = pending.pop(job)
                            successful += self.finish_network(task, job, window)
                finally:
                    stop.set()
        finally:
            with self.lock:
                self.network_active.clear()
                self.active = None
                self.interrupt.clear()
            self.night_running = False
            if nocturnal and self.night.release_after_work:
                self.night.close(reason=self.night.reason)
        # Keep a finite production bound, then grow after healthy batches.
        # A restricted or failing batch reduces contention at the transport.
        if successful * 2 < len(tasks):
            self.network_width = max(1, self.network_width // 2)
        elif successful == len(tasks):
            self.network_width = min(20, self.network_width + 1)

    def enrich_import(self, task, owner, stop):
        kind, subject = task["kind"], task["subject"]
        paths = enrichment_paths(self.index, kind, subject)
        def update(event):
            if event.get("event") == "activity":
                self.import_activity = event["lyrics"], event["lrc"]
            elif event.get("event") in ("track-start", "track-result"):
                with self.lock:
                    self.import_lyrics_live.setdefault(subject, {})[event["path"]] = event.get("result")

        try:
            code, data = self.output([sys.executable, "-B", str(BIN / "_parole_work.py"), kind, subject],
                                     timeout=180 + 105 * ((len(paths) + 1) // 2) if kind == IMPORT_LYRICS else 180,
                                     observer=update if kind == IMPORT_LYRICS else None,
                                     maximum=2 * 1024**2 if kind == IMPORT_LYRICS else 262_144,
                                     extra_env={"KARA_ENRICHMENT_EVENTS": "1"}, stop=stop, import_owner=owner)
        finally:
            if kind == IMPORT_LYRICS:
                self.import_activity = (0, 0)
        value = json.loads(data.splitlines()[-1]) if not code else {"state": "temporary"}
        if kind == IMPORT_LYRICS and paths:
            if "tracks_seconds" in value:
                self.import_lyrics_stats = value
                self.index.review("import_lyrics_metrics", subject, "measured", value)
                self.journal.emit("LYRICS", f"{value['lyrics'].get('found', 0)} textes · "
                                  f"{value['lrc'].get('found', 0)} LRC · {value['seconds']:.2f} s · "
                                  f"{value['requests']} requêtes · {value['cache_hits']} recherches évitées")
            self.index_wake.set()
        if kind == IMPORT_COVER and value["state"] == "complete" and paths:
            self.index.notify(affected_tracks(self.index, paths[0]), ARTWORK)
            self.index_wake.set()
        if stop():
            raise Interrupted
        if value["state"] == "complete":
            return "done", "enrichissement vérifié", 0
        if value["state"] == "temporary":
            payload = json.loads(task["payload"])
            if kind == IMPORT_LYRICS and (not payload.get("manual") or payload.get("automatic")):
                # Provider outages and quotas convey no absence. Preserve
                # the exact album receipt across retry and restart.
                return "temporary", "fournisseur ou lecture temporairement indisponible", 300 * min(72, 2 ** min(7, task["attempts"] - 1))
            if kind != IMPORT_LYRICS and task["attempts"] < 2:
                return "temporary", "recherche différée", 300
        return "review", "enrichissement partiel · maintenance complémentaire", 0

    def import_enrichment_count(self):
        with self.lock:
            return len(self.import_enrichment_active)

    def retry_enrichment_delivery(self):
        if (self.import_queue is None or self.import_queue.pause_requested()
                or monotonic() < self.next_enrichment_delivery):
            return
        self.next_enrichment_delivery = monotonic() + 10
        for job, url, payload in self.import_queue.enrichment_pending():
            outcome = json.loads(payload)
            scheduled = schedule_enrichment(self.index, outcome.get("tracks", ()))
            if self.import_queue.acknowledge_enrichment(job, url, payload, scheduled):
                self.index_wake.set()

    def import_enrichment_loop(self, kind):
        # Two bounded lanes under the same maintenance owner. Text and LRC
        # share a provider lookup, while artwork has its own durable claims.
        try:
            os.setpriority(os.PRIO_PROCESS, 0, max(5, os.getpriority(os.PRIO_PROCESS, 0)))
        except OSError:
            pass
        wake = self.lyrics_wake if kind == IMPORT_LYRICS else self.shutdown
        while not self.shutdown.is_set():
            if kind == IMPORT_LYRICS:
                wake.clear()
            try:
                if kind == IMPORT_LYRICS:
                    self.retry_enrichment_delivery()
                if self.tick_import_enrichment(self.index.settings(wait=.15), time(), kind):
                    continue
            except (OSError, ValueError, TypeError, KeyError, sqlite3.Error):
                self.journal.emit("ERROR", "File d’enrichissement temporairement indisponible", "WARNING")
                self.shutdown.wait(5)
            wake.wait(.5)

    def tick_import_enrichment(self, settings, now, kind=IMPORT_LYRICS):
        enabled = (settings["lyrics"] or settings["lrc"]) if kind == IMPORT_LYRICS else settings["cover"]
        manual_only = kind == IMPORT_LYRICS and not enabled
        if ((not enabled and not manual_only) or settings["paused"] or self.night_context(settings)
                or self.shutdown.is_set()
                or (kind != IMPORT_LYRICS and self.priority)
                or not self.index.work_due((kind,), now, manual_only=manual_only)):
            return False
        if ((self.import_queue is not None and self.import_queue.pause_requested())
                or disk_usage(self.index.root).free <= settings["reserve_gib"] * 1024**3):
            return False
        with (self.import_queue.activity() if self.import_queue is not None else nullcontext()) as owner:
            if owner is not None and self.import_queue.pause_requested():
                return False
            task = self.index.claim((kind,), now, manual_only=True) if kind == IMPORT_LYRICS else None
            if task is None and not manual_only:
                task = self.index.claim((kind,), now)
            if task is None:
                return False
            identity = kind, task["subject"]
            cancel = Event()
            with self.lock:
                self.import_enrichment_active[identity] = cancel
                if kind == IMPORT_LYRICS:
                    payload = json.loads(task["payload"])
                    self.import_lyrics_automatic = not payload.get("manual") or bool(payload.get("automatic"))
            try:
                self.run_import_enrichment(task, owner, cancel)
            finally:
                with self.lock:
                    self.import_enrichment_active.pop(identity, None)
                    if kind == IMPORT_LYRICS:
                        self.import_lyrics_live.pop(task["subject"], None)
                        self.import_lyrics_automatic = True
                        self.import_lyrics_preempted = False
        return True

    def run_import_enrichment(self, task, owner, cancel):
        kind = task["kind"]
        category = "LYRICS" if kind == IMPORT_LYRICS else "COVER"
        started = monotonic()
        next_check = 0.

        def stopped():
            nonlocal next_check
            if self.shutdown.is_set() or cancel.is_set() or (kind != IMPORT_LYRICS and self.priority):
                return True
            now = monotonic()
            if now >= next_check:
                next_check = now + .25
                if (owner is not None and self.import_queue.pause_requested()
                        or self.index.settings(wait=.15)["paused"]):
                    return True
            return False

        self.journal.emit(category, "Recherche de paroles et de LRC" if kind == IMPORT_LYRICS else "Pochette d’un import")
        paused = preempted = False
        try:
            # Import pause drains the child and the claim before the activity
            # lease is released. Other network work has separate cancellation.
            result = self.enrich_import(task, owner, stopped)
        except Interrupted:
            paused = owner is not None and self.import_queue.pause_requested()
            preempted = kind == IMPORT_LYRICS and self.import_lyrics_preempted
            result = "suspended", "ressources libérées", 0 if paused or preempted else 45
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            self.journal.emit(category, "Moteur indisponible ou réponse invalide", "WARNING")
            payload = json.loads(task["payload"])
            automatic_lyrics = kind == IMPORT_LYRICS and (not payload.get("manual") or payload.get("automatic"))
            result = (("temporary", "opération indisponible", 300 * min(72, 2 ** min(7, task["attempts"] - 1)))
                      if automatic_lyrics or task["attempts"] < 2 else
                      ("review", "enrichissement partiel · maintenance complémentaire", 0))
        self.index.finish(task, *result, refund_attempt=paused or preempted)
        if paused:
            self.journal.emit(category, "Enrichissement suspendu · état sauvegardé · ressources libérées")
        self.metrics[f"{kind}_last_seconds"] = round(monotonic() - started, 3)
        self.changed()

    def do_codex(self, task):
        query = json.loads(task["payload"])
        if query["kind"] == "draft_correction":
            return "excluded", "Correction de brouillon IA archivée", 0
        self.journal.emit("CODEX", f"Recherche approfondie · {query['artist']} {query['title'] or query['album']}")
        try:
            result, error = research(self.index, query, self.current_settings, self.stopped,
                                     lambda text: self.journal.emit("CODEX", text))
        except PermissionError:
            return "temporary", "authentification isolée indisponible", DAY
        if result is None:
            self.journal.emit("CODEX", {"quota_local": "Limite quotidienne locale atteinte",
                                        "quota_distant": "Limite Codex signalée",
                                        "recherche_web_non_observée": "Recherche Web non confirmée",
                                        "relecture_indisponible": "Relecture Codex indisponible"}[error], "WARNING")
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
        self.changed()
        self.journal.emit("CODEX", {"found": "Sources identifiées", "not_found": "Aucune source trouvée",
                                    "ambiguous": "Correspondance à vérifier"}[result["status"]])
        for source in result["sources"]:
            source_id, added = discover(self.index, source, task["subject"])
            if added:
                self.index.enqueue("source_probe", source_id, encoded_key(source_id), 40)
                self.journal.emit("SOURCE", "Adaptateur spécifique à examiner" if source["adapter"] == "custom" else
                                  "Fournisseur proposé · validation nécessaire")
        return ("done" if result["status"] == "found" else "review", result["status"], 0)

    def night_lrc_result(self, task, value):
        outcome = value["state"]
        if outcome in ("found", "present", "rescan", "excluded", "invalid"):
            return self.do_song(task, value)
        if outcome in ("not_found", "ambiguous", "instrumental"):
            # Provider catalogue misses already have their own temporary
            # negative entries. A track delay prevents traversal churn.
            return "temporary", outcome, DAY
        now = time()
        reopening = [row["next_available"] for row in night_lrc_providers(self.index, now=now)
                     if row["next_available"] and row["next_available"] > now]
        # A quota expiry can make this recording searchable again earlier
        # than the per-track retry. Other tracks can still use ready sources.
        delay = min(reopening) - now if reopening else min(DAY, 120 * 2**min(task["attempts"], 9))
        return "temporary", outcome, max(.05, delay)

    def execute(self, task, *, nocturnal=False, local_reference_only=False):
        started = monotonic()
        kind, subject = task["kind"], task["subject"]
        if nocturnal and kind != "lrc":
            self.index.finish(task, "suspended", "Hors file nocturne LRC", 0, refund_attempt=True)
            return
        category = {"discography": "DISCOGRAPHIE", "prepare": "KARA", "source_probe": "SOURCE",
                    "cleanup": "CACHE"}.get(kind, kind.upper())
        network_found = False
        activity = ()
        window = self.night.window if nocturnal else None
        with self.lock:
            self.interrupt.clear()
            if kind in HEAVY and (self.priority or monotonic() < self.priority_until):
                stats = (dict(seconds=0., outcome="interrupted", cache_bytes=None, corrected=0, generated=0)
                         if nocturnal and kind == "prepare" else None)
                self.index.finish(task, "suspended", "ressources prioritaires", 15, night=stats,
                                  closed=(window, "interrupted") if window is not None else None)
                return
            self.active = (kind, subject)
        self.night_running = nocturnal
        self.night_settings_at = 0.
        self.night_lrc = (False, False)
        self.memory_interruption = False
        self.night_measures = {}
        song = self.index.song(subject) if kind in ("lyrics", "lrc", *HEAVY) else None
        self.progress = (category, song["title"] if song is not None else "En cours", None)
        if local_reference_only:
            self.journal.emit(category, "Vérification d’une référence locale")
        elif kind not in ("prepare", "codex"):
            self.journal.emit(category, {"lyrics": "Recherche de paroles", "lrc": "Recherche d’un LRC",
                                        "transcription": "Transcription locale pour vérification",
                                        "album": "Vérification d’une édition", "discography": f"Vérification · {subject}",
                                        "source": "Vérification des conditions et des correspondances",
                                        "source_probe": "Vérification de la documentation fournisseur",
                                        "cover": "Recherche de pochette", "metadata": "Vérification de l’identification", "cleanup": "Nettoyage des brouillons"}[kind]
                              + (f" · {song['title']}" if song is not None else ""))
        try:
            if nocturnal and self.stopped():
                raise Interrupted
            if kind in HEAVY:
                result = ("suspended", "Traitement IA Kara archivé", DAY)
            elif kind == "codex":
                result = self.do_codex(task)
            elif kind == "cleanup":
                removed = self.cleanup_interrupted()
                self.journal.emit("CACHE", f"{removed} dossiers intermédiaires retirés")
                result = ("done", "nettoyé", 0)
            else:
                value = self.network_output(task, self.current_settings, local_reference_only=local_reference_only)
                result = self.night_lrc_result(task, value) if nocturnal else self.network_result(task, value)
                network_found = value["state"] == "found" and value.get("source") not in (None, "Strawberry", LOCAL_SOURCE)
                activity = (("night_lrc",) if nocturnal and network_found and result[0] == "done"
                            else self.reference_activity(task, value, result))
        except Interrupted:
            result = ("suspended", "ressources libérées", 45)
            self.journal.emit(category, "Traitement interrompu · reprise différée")
            if nocturnal:
                if self.night.release_after_work:
                    self.night.close(reason=self.night.reason)
                else:
                    self.night.stop_work()
                if kind in HEAVY:
                    self.recover_night_drafts()
        except (OSError, ValueError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError):
            self.journal.emit(category, "Moteur indisponible ou réponse invalide", "WARNING")
            result = "temporary", "opération indisponible", min(DAY, 300 * 2**min(task["attempts"], 8))
        finally:
            with self.lock:
                if self.cancelled == self.active:
                    self.cancelled = None
                self.active = None
                self.interrupt.clear()
            self.progress = None
            self.night_running = False
        stats = None
        state, detail, _ = result
        if kind == "transcription" and detail == "brouillon à vérifier":
            activity = ("draft",)
        if state == "suspended" and self.memory_interruption:
            activity = ("memory",)
        if kind == "prepare" and state == "done" and detail in ("prêt", "référence prête"):
            corrected, generated = self.night_lrc
            activity = tuple(label for label, observed in (("ready", True), ("corrected", corrected and not generated),
                                                           ("generated", generated)) if observed)
        if nocturnal and kind == "prepare":
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
        self.index.finish(task, *result, night=stats, closed=self.closed_outcome(window, result),
                          refund_attempt=detail == LOCAL_REFERENCE_MISS, network_found=network_found,
                          activity=activity)
        self.metrics[f"{kind}_last_seconds"] = round(monotonic() - started, 3)
        self.changed()
        return result

    def authorized_imports(self, settings):
        if (self.import_queue is None or self.import_queue.pause_requested()
                or not (settings["import_tracks"] or settings["import_albums"])
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
                if job is None:
                    return
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
                index_thread = Thread(target=self.index_loop, name="parole-index", daemon=False)
                lexicon_thread = Thread(target=self.lexicon_loop, name="parole-lexicon", daemon=False)
                enrichment_threads = [Thread(target=self.import_enrichment_loop, args=(kind,),
                                             name=f"parole-{kind[7:]}", daemon=False) for kind in IMPORT_ENRICHMENT]
                workers = (index_thread, lexicon_thread, *enrichment_threads)
                for thread in workers:
                    thread.start()
                try:
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
                    self.shutdown.set()
                    self.index_wake.set()
                    for thread in workers:
                        thread.join()
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

    def night_context(self, settings=None):
        if settings is None:
            settings = getattr(self, "current_settings", {})
        state = self.night.desktop
        return bool(settings.get("night_enabled") and state and (state.lid or state.locked))

    def tick(self):
        settings = self.current_settings = self.index.settings()
        now = time()
        if settings["paused"]:
            self.night.refresh(settings)
            self.resources.close()
            return
        policy = tuple(settings[name] for name in NIGHT_RESTART_SETTINGS)
        if policy != self.night_policy:
            self.resources.sample = 0.
            self.night.stop_work()
            self.night.next_desktop = 0.
        self.night_policy = policy
        desktop_ready = self.night.refresh(settings)
        if settings["night_enabled"]:
            self.resources.session_idle(settings["night_idle"])
            if self.resources.resumed:
                self.resources.resumed = False
                self.night.activity()
        if self.night_context(settings):
            active_import = self.import_queue and self.import_queue.count().get("running", 0)
            if desktop_ready and not self.priority and not active_import and not self.import_enrichment_count():
                self.tick_night(settings, now)
            else:
                self.night.stop_work()
            # An empty or throttled nighttime queue cannot fall through to
            # unrelated daytime maintenance.
            return
        imports_pending = self.import_enrichment_count() or self.import_queue and self.import_queue.pending()
        if imports_pending or self.priority:
            self.night.stop_work()
            if desktop_ready:
                self.night.report("Préparation nocturne suspendue · Kara ou imports prioritaires")
        if not self.foreground.is_set() and not settings["night_enabled"] and not imports_pending:
            return
        if self.priority:
            # During ordinary playback, allow one conventional lyric task
            # at a time and give the combined artist/import lane precedence.
            # The playback branch deliberately skips the compute governor.
            if not self.foreground.is_set() or self.index_priority:
                return
            due = self.index.work_due((IMPORT_LYRICS,), now)
            with self.lock:
                active_lyrics = any(kind == IMPORT_LYRICS for kind, _ in self.import_enrichment_active)
            if not due and not active_lyrics and disk_usage(self.index.root).free > settings["reserve_gib"] * 1024**3:
                kinds = tuple(kind for kind in self.network_kinds(settings, now) if kind in ("lyrics", "lrc"))
                task = self.claim_network(settings, now, kinds)
                if task is not None:
                    self.execute(task)
            return
        if self.import_diagnostics and not self.import_queue.pause_requested():
            self.import_diagnostics.tick(settings, now)
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
        if not self.resources.allowed_storage(settings):
            return
        self.tick_network(settings, now, self.network_kinds(settings, now))

    def network_kinds(self, settings, now):
        ready = ready_categories(self.index, now)
        return tuple(kind for kind in NETWORK
                     if (NETWORK_FLAGS.get(kind, kind) is None or settings[NETWORK_FLAGS.get(kind, kind)])
                     and (kind not in NETWORK_CATEGORY or NETWORK_CATEGORY[kind] in ready)
                     and (kind != "codex" or now >= settings.get("codex_suspended_until", 0)))

    @staticmethod
    def local_reference_kinds(settings):
        return tuple(kind for kind in ("lrc", "lyrics") if settings[kind])

    def tick_local_reference(self, kinds, now, *, nocturnal):
        task = self.index.claim(kinds, now, local_reference=True)
        if task is None:
            return False
        # The same durable task and governor own the operation. Only this
        # turn skips provider admission because it cannot access the network.
        self.execute(task, nocturnal=nocturnal, local_reference_only=True)
        self.local_reference_next = False
        return True

    def tick_network(self, settings, now, kinds, *, nocturnal=False):
        manual = self.index.claim(("codex",), now, high=True, codex_auto=settings["codex_auto"]) if "codex" in kinds else None
        if manual is not None:
            self.execute(manual, nocturnal=nocturnal)
            return True
        local_kinds = self.local_reference_kinds(settings)
        try_local_first = self.local_reference_next
        if try_local_first and self.tick_local_reference(local_kinds, now, nocturnal=nocturnal):
            return True
        kinds_without_codex = tuple(kind for kind in kinds if kind != "codex")
        first = self.index.claim(kinds_without_codex, now, high=True) or self.claim_network(settings, now, kinds)
        if first is None:
            return not try_local_first and self.tick_local_reference(local_kinds, now, nocturnal=nocturnal)
        self.local_reference_next = True
        tasks = [first]
        maximum = max(1, min(20, settings["network_workers"], self.network_width))
        if first["kind"] in NIGHT_NETWORK and maximum > 1:
            batch_kinds = tuple(kind for kind in kinds if kind in NIGHT_NETWORK)
            subjects = [first["subject"]]
            while len(tasks) < maximum:
                task = self.claim_network(settings, now, batch_kinds, avoid=subjects)
                if task is None:
                    break
                tasks.append(task)
                subjects.append(task["subject"])
        if len(tasks) == 1:
            result = self.execute(first, nocturnal=nocturnal)
            if self.network_width == 1 and result and self.network_completed(result):
                self.network_width = 2
        else:
            self.network_batch(tasks, settings, nocturnal=nocturnal)
        return True

    def claim_network(self, settings, now, kinds, *, avoid=()):
        # Interleave job classes and avoid concurrent changes to the same
        # track or edition, including text/LRC pairs on one recording.
        for _ in NETWORK_TURN:
            kind = NETWORK_TURN[self.network_cursor]
            self.network_cursor = (self.network_cursor + 1) % len(NETWORK_TURN)
            if kind in kinds:
                task = self.index.claim((kind,), now, codex_auto=settings["codex_auto"], avoid=avoid)
                if task is not None:
                    return task
        return None

    def tick_night(self, settings, now):
        with self.index.db(wait=.15) as db:
            due = self.index.night_lrc_due(db)
            providers = night_lrc_providers(self.index, now=now, db=db)
        try:
            if due is None:
                return self.night.report("Aucun LRC à rechercher dans la file nocturne")
            if not any(row["availability"] == "ready" for row in providers):
                return self.night.report("En attente d’un fournisseur de LRC")
            if due > now:
                return self.night.report("Recherches LRC différées jusqu’à la prochaine échéance")
            if not self.night_allowed(settings, running=False, light=True) or not self.night.open(settings):
                return False
            task = self.index.claim(("lrc",), now, night_lrc=True)
            if task is None:
                return False
            self.execute(task, nocturnal=True)
            return True
        finally:
            self.night.stop_work()
