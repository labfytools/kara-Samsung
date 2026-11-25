"""Recherche et import d’albums dans la fenêtre de paroles."""
from collections import deque
import json
import os
from pathlib import Path
import signal
import sqlite3
from time import monotonic

from gi.repository import Gio, GLib, Gtk, Pango

from _kara_import_queue import COMPLETE, ImportQueue, MAX_BATCHES, import_key
from _kara_import_resources import DOWNLOADS, PROCESSING, WORKING_RESERVE, check_space, lease, slot_count
from _kara_import_result import CATEGORIES, ImportIssue
from _musique_oz import INBOX, SHARED_LIBRARY
from _kara_paths import BIN, catalogue_command

KARA = BIN / "kara"
PIPE = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE


def show_details(widget, title, text, journal=None):
    parent = widget.get_root()
    window = Gtk.Window(title=title, transient_for=parent if isinstance(parent, Gtk.Window) else None,
                        modal=True, destroy_with_parent=True, default_width=720, default_height=420)
    scroll = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER)
    view = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True,
                        top_margin=12, bottom_margin=12, left_margin=12, right_margin=12)
    view.get_buffer().set_text(text)
    scroll.set_child(view)
    if journal:
        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        scroll.set_vexpand(True)
        panel.append(scroll)
        open_journal = Gtk.Button(label="Ouvrir le journal complet", halign=Gtk.Align.END,
                                  margin_end=12, margin_bottom=8)
        open_journal.connect("clicked", lambda button: show_journal(button, journal))
        panel.append(open_journal)
        window.set_child(panel)
    else:
        window.set_child(scroll)
    window.present()


def show_journal(widget, journal):
    lines = []
    try:
        with Path(journal).open(encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except ValueError:
                    record = None
                if not isinstance(record, dict):
                    lines.append(line.rstrip())
                    continue
                if record.get("event") == "output":
                    lines.append(f"{record['stage']} · {record.get('url') or 'bibliothèque'}\n{record['output'].rstrip()}")
                elif record.get("event") == "album":
                    lines.append(f"{record['title']} · {record['message']}")
                elif record.get("event") != "header":
                    lines.append(record.get("message", ""))
    except OSError as error:
        lines = [str(error)]
    show_details(widget, "Journal de l’import", "\n\n".join(lines))


class AlbumImport(Gtk.Revealer):
    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.lookup = None
        self.lookup_live = False
        self.suggestion_timer = None
        self.suggestion_query = ""
        self.suggestions = []
        self.imports = []
        self.can_launch = lambda: True
        self.on_update = lambda *_: None
        self.albums = []
        self.by_id = {}
        self.imports_snapshot = ([], {})
        self.next_poll = 0
        self.closing = False
        self.refreshing = {}
        self.queue_error = ""
        try:
            self.queue = ImportQueue()
        except (OSError, sqlite3.Error) as error:
            self.queue = None
            self.queue_error = str(error)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, css_classes=["album-import"])
        self.set_child(panel)
        form = Gtk.Box(spacing=8)
        panel.append(form)

        self.artist = Gtk.Entry(placeholder_text="Artiste", width_chars=13, max_width_chars=18, max_length=150)
        self.artist.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY, "system-search-symbolic")
        self.artist.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Rechercher les albums")
        self.artist.connect("activate", self.search_artists)
        self.artist.connect("icon-press", self.search_artists)
        self.artist_changed_handler = self.artist.connect("changed", self.artist_changed)
        form.append(self.artist)
        self.link = Gtk.Entry(placeholder_text="Lien d’album YouTube Music", hexpand=True, max_length=4096)
        self.link.connect("changed", self.refresh_controls)
        self.link.connect("activate", self.start)
        form.append(self.link)
        self.submit = Gtk.Button(label="Télécharger", sensitive=False, css_classes=["fetch"])
        self.submit.connect("clicked", self.start)
        form.append(self.submit)

        self.results_header = Gtk.Box(spacing=8, visible=False)
        self.results_title = Gtk.Label(xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        self.results_header.append(self.results_title)
        self.download_all = Gtk.Button(label="Tout télécharger", visible=False, css_classes=["fetch"])
        self.download_all.connect("clicked", self.start_all)
        self.results_header.append(self.download_all)
        panel.append(self.results_header)

        self.results = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        self.results_view = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                               propagate_natural_height=True, max_content_height=200, visible=False)
        self.results_view.set_child(self.results)
        panel.append(self.results_view)

        self.status = Gtk.Label(xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END, visible=False,
                                css_classes=["lyrics-info"])
        status_box = Gtk.Box(spacing=6)
        status_box.append(self.status)
        self.search_detail = Gtk.Button(label="Détails", visible=False)
        self.search_detail.connect("clicked", lambda btn: show_details(btn, "Recherche d’albums", self.lookup_detail))
        self.lookup_detail = ""
        status_box.append(self.search_detail)
        panel.append(status_box)
        controls = Gtk.Box(spacing=8)
        panel.append(controls)
        controls.append(Gtk.Label(label="Lots simultanés"))
        self.concurrency = Gtk.SpinButton.new_with_range(1, MAX_BATCHES, 1)
        self.concurrency.set_tooltip_text("Nombre maximal de lots actifs")
        self.concurrency_handler = self.concurrency.connect("value-changed", self.configure_concurrency)
        controls.append(self.concurrency)
        self.pause = Gtk.Button(label="Mettre la file en pause")
        self.pause.set_tooltip_text("Suspend les nouveaux départs. Les lots actifs peuvent se terminer.")
        self.pause.connect("clicked", lambda _: self.configure(paused=not self.imports_snapshot[1].get("paused", False)))
        controls.append(self.pause)
        self.queue_status = Gtk.Label(xalign=0., wrap=True, hexpand=True, css_classes=["lyrics-info"])
        panel.append(self.queue_status)
        self.overall = Gtk.ProgressBar(show_text=True)
        panel.append(self.overall)
        if self.queue is None:
            self.pause.set_sensitive(False)
            self.concurrency.set_sensitive(False)
            self.queue_status.set_text(f"File indisponible · {self.queue_error}")
        self.import_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.import_view = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              propagate_natural_height=True, max_content_height=185, visible=False)
        self.import_view.set_child(self.import_rows)
        panel.append(self.import_view)
        self.history_button = Gtk.Button(label="Historique", halign=Gtk.Align.END, sensitive=self.queue is not None)
        self.history_button.connect("clicked", self.show_history)
        panel.append(self.history_button)
        GLib.idle_add(self.launch_next)

    def show_history(self, button):
        window = Gtk.Window(title="Historique des importations", transient_for=button.get_root(),
                            destroy_with_parent=True, default_width=640, default_height=420)
        scroll = Gtk.ScrolledWindow()
        rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=12,
                       margin_bottom=12, margin_start=12, margin_end=12)
        scroll.set_child(rows)
        window.set_child(scroll)
        more = Gtk.Button(label="Imports précédents")
        cursor = None

        def append_page(_button):
            nonlocal cursor
            try:
                page = self.queue.history(cursor, limit=26)
                if more.get_parent() is rows:
                    rows.remove(more)
                for row in page[:25]:
                    job = ImportJob(row["targets"], None, identity=row["id"], option=row["option"],
                                    journal=row["journal"])
                    job.outcomes = self.queue.outcomes(row["id"])
                    job.finish_import("success" if row["state"] == "done" else "cancelled",
                                      row["message"], 1., "", notify=False, history=True)
                    rows.append(job)
                    cursor = row["seq"]
                if len(page) > 25:
                    rows.append(more)
                elif cursor is None:
                    rows.append(Gtk.Label(label="Aucune importation terminée."))
            except (OSError, sqlite3.Error) as error:
                rows.append(Gtk.Label(label=f"Historique indisponible · {error}"))

        more.connect("clicked", append_page)
        append_page(more)
        window.present()

    @property
    def running(self):
        return bool(self.imports_snapshot[1].get("active") or self.imports_snapshot[1].get("waiting"))

    def toggle(self, button):
        opened = button.get_active()
        self.set_reveal_child(opened)
        if opened and self.lookup is None:
            self.artist.grab_focus()
        elif not opened:
            self.cancel_suggestions()

    def refresh_controls(self, _widget=None):
        idle = self.lookup is None or self.lookup_live
        self.artist.set_sensitive(idle)
        self.results.set_sensitive(idle)
        self.submit.set_sensitive(bool(self.link.get_text().strip()))
        self.download_all.set_sensitive(idle and bool(self.albums))

    def message(self, text, *, failed=False, detail=""):
        self.status.set_text(text)
        self.status.set_tooltip_text(text)
        self.status.set_visible(True)
        self.lookup_detail = detail
        self.search_detail.set_visible(bool(detail))
        if failed:
            self.status.add_css_class("import-error")
        else:
            self.status.remove_css_class("import-error")

    def hide_message(self):
        self.status.set_visible(False)
        self.search_detail.set_visible(False)

    def clear_results(self):
        self.albums = []
        while row := self.results.get_first_child():
            self.results.remove(row)
        self.results_header.set_visible(False)
        self.results_view.set_visible(False)
        self.download_all.set_visible(False)

    def artist_changed(self, _entry):
        if self.lookup is not None and not self.lookup_live:
            return
        self.cancel_suggestions()
        self.suggestion_query = ""
        self.suggestions = []
        self.clear_results()
        self.hide_message()
        query = self.artist.get_text().strip()
        if self.get_reveal_child() and len(query) >= 2:
            self.suggestion_timer = GLib.timeout_add(320, self.suggest_artists, query)

    def suggest_artists(self, query):
        self.suggestion_timer = None
        if self.get_reveal_child() and self.artist.get_text().strip() == query:
            self.search("artistes", query, lambda artists: self.suggestions_found(query, artists), live=True)
        return GLib.SOURCE_REMOVE

    def suggestions_found(self, query, artists):
        self.suggestion_query = query
        self.suggestions = artists
        if artists:
            self.show_artist_choices(artists[:6], "Suggestions d’artistes")

    def cancel_suggestions(self):
        if self.suggestion_timer is not None:
            GLib.source_remove(self.suggestion_timer)
            self.suggestion_timer = None
        if self.lookup_live:
            self.stop_lookup()

    def search_artists(self, _widget, *args):
        query = self.artist.get_text().strip()
        if not query:
            return
        self.cancel_suggestions()
        if self.lookup is not None:
            return
        self.clear_results()
        if query == self.suggestion_query and self.suggestions:
            self.artists_found(self.suggestions)
            return
        self.message("Recherche de l’artiste sur YouTube Music…")
        self.search("artistes", query, self.artists_found)

    def search(self, action, value, callback, *, live=False):
        try:
            self.lookup = Gio.Subprocess.new(catalogue_command(action, value), PIPE)
        except (GLib.Error, ValueError) as error:
            if not live:
                category = "path" if isinstance(error, ValueError) else "dependency"
                self.message(CATEGORIES[category], failed=True, detail=str(error))
            return
        self.lookup_live = live
        self.refresh_controls()
        self.lookup.communicate_utf8_async(None, None, self.search_finished, callback)

    def stop_lookup(self):
        if self.lookup is not None:
            self.lookup.force_exit()
            self.lookup = None
        self.lookup_live = False

    def cancel_search(self):
        self.cancel_suggestions()
        self.stop_lookup()

    def search_finished(self, process, result, callback):
        if process != self.lookup:
            return
        live = self.lookup_live
        self.lookup = None
        self.lookup_live = False
        self.refresh_controls()
        output = ""
        try:
            _, output, _ = process.communicate_utf8_finish(result)
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("Réponse du catalogue invalide.")
        except (GLib.Error, ValueError, TypeError) as error:
            if not live:
                self.message("La recherche YouTube Music a échoué.", failed=True, detail=output or str(error))
            return
        if not process.get_successful() or "data" not in payload:
            if not live:
                text = CATEGORIES.get(payload.get("category"), "La recherche YouTube Music a échoué.")
                self.message(text, failed=True, detail=payload.get("error") or output)
            return
        callback(payload["data"])

    def artists_found(self, artists):
        query = self.artist.get_text().strip().casefold()
        exact = [item for item in artists if item["name"].casefold() == query]
        if len(exact) == 1 or len(artists) == 1:
            self.get_albums((exact or artists)[0])
            return
        if not artists:
            self.message("Aucun artiste trouvé.")
            return
        self.show_artist_choices(artists, "Choisir un artiste")

    def show_artist_choices(self, artists, title):
        self.clear_results()
        self.results_title.set_text(title)
        self.results_header.set_visible(True)
        self.results_view.set_visible(True)
        self.hide_message()
        for item in artists:
            text = " · ".join(part for part in (item["name"], item["followers"]) if part)
            self.add_row(text, "Albums", lambda _button, artist=item: self.get_albums(artist))

    def get_albums(self, artist):
        self.cancel_suggestions()
        if self.lookup is not None:
            return
        self.clear_results()
        self.artist.handler_block(self.artist_changed_handler)
        try:
            self.artist.set_text(artist["name"])
        finally:
            self.artist.handler_unblock(self.artist_changed_handler)
        self.message(f"Recherche des albums de {artist['name']}…")
        self.search("albums", artist["id"], self.albums_found)

    def albums_found(self, data):
        self.clear_results()
        self.albums = [(album["title"], album["url"]) for album in data["albums"]]
        if not self.albums:
            self.message("Aucun album trouvé pour cet artiste.")
            return
        artist = data["name"] or self.artist.get_text().strip()
        self.results_title.set_text(f"{artist} · {len(self.albums)} album(s)")
        self.results_header.set_visible(True)
        self.results_view.set_visible(True)
        self.download_all.set_visible(True)
        self.hide_message()
        for album in data["albums"]:
            job = (album["title"], album["url"])
            text = " · ".join(str(part) for part in (album["title"], album["year"]) if part)
            self.add_row(text, "Télécharger", lambda _button, selected=job: self.start_queue([selected]))
        self.refresh_controls()

    def add_row(self, text, action, handler):
        row = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        label = Gtk.Label(label=text, xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        label.set_tooltip_text(text)
        row.append(label)
        button = Gtk.Button(label=action)
        button.connect("clicked", handler)
        row.append(button)
        self.results.append(row)

    def start_all(self, _widget):
        self.start_queue(self.albums)

    def start(self, _widget):
        url = self.link.get_text().strip()
        if url and self.start_queue([("", url)]):
            self.link.set_text("")
            self.link.grab_focus()

    def start_queue(self, jobs, *, journal=None):
        return self.enqueue(jobs, journal=journal) is not None

    def enqueue(self, jobs, *, journal=None, request=None, option=None):
        try:
            if self.queue is None:
                raise OSError(self.queue_error)
            check_space(self.queue.directory, state=True)
            identity = self.queue.enqueue(jobs, journal=journal, option=option, request=request)
        except (ValueError, OSError, sqlite3.Error) as error:
            self.message("Impossible d’enregistrer l’import.", failed=True, detail=str(error))
            return None
        self.hide_message()
        self.launch_next(force=True)
        return self.by_id[identity]

    def start_maintenance(self, option, title):
        return self.enqueue([(title, "")], option=option) is not None

    def configure_concurrency(self, spin):
        self.configure(batches=spin.get_value_as_int())

    def configure(self, **settings):
        try:
            if self.queue is None:
                raise OSError(self.queue_error)
            self.queue.configure(**settings)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.message(str(error), failed=True)
            return False
        self.launch_next(force=True)
        return True

    def add_import(self, row):
        job = ImportJob(row["targets"], self.import_finished, identity=row["id"], option=row["option"],
                        retry=self.retry_job, journal=row["journal"])
        self.by_id[job.identity] = job
        self.imports.append(job)
        job.update_counts()
        job.on_update = self.changed
        self.import_rows.prepend(job)
        self.import_view.set_visible(True)
        return job

    def changed(self, job, kind, payload):
        if kind == "progress":
            self.on_update(job, kind, payload)
        elif kind in {"finished", "album", "running"}:
            self.on_update(job, kind, payload)
            self.next_poll = 0

    def launch_next(self, force=False):
        now = monotonic()
        if self.queue is None or self.closing or not force and now < self.next_poll:
            return GLib.SOURCE_REMOVE
        self.next_poll = now + 0.75
        try:
            self.sync_queue()
        except (OSError, sqlite3.Error) as error:
            self.queue_status.set_text(f"File indisponible · {error}")
        return GLib.SOURCE_REMOVE

    def sync_queue(self):
        rows = self.queue.rows()
        paused, capacity = self.queue.settings()
        inbox = SHARED_LIBRARY / INBOX
        can_start = self.can_launch() and not paused
        limitation = ""
        if can_start and any(row["state"] in {"queued", "running"} for row in rows):
            try:
                check_space(self.queue.directory, state=True)
                if inbox.is_dir() and not inbox.is_symlink():
                    check_space(inbox, extra=WORKING_RESERVE)
            except ImportIssue as error:
                can_start, limitation = False, str(error)
        self.pause.set_label("Reprendre la file" if paused else "Mettre la file en pause")
        self.concurrency.handler_block(self.concurrency_handler)
        self.concurrency.set_value(capacity)
        self.concurrency.handler_unblock(self.concurrency_handler)
        active = sum(row["state"] == "running" for row in rows)
        for row in rows:
            identity, state = row["id"], row["state"]
            job = self.by_id.get(identity) or self.add_import(row)
            job.awaiting_refresh = bool(row["refresh"])
            if job.done:
                job.update_details()
            if job.process is not None:
                continue
            if state in {"running", "queued"}:
                if job.done:
                    self.import_rows.remove(job)
                    self.imports.remove(job)
                    job = self.add_import(row)
                job.store_state = state
                job.message(row["message"])
                if state == "running" or can_start and active < capacity:
                    with lease(self.queue.directory / f"{identity}.lock", wait=False) as fd:
                        if fd is None:
                            # An import started by the previous instance still
                            # owns the lot. Its worker commits the result itself.
                            if state == "running":
                                job.message("Import actif · session précédente")
                            continue
                        if state == "running":
                            self.queue.transition(identity, "running", "queued", "Import interrompu · reprise en attente")
                            job.store_state = "queued"
                            active -= 1
                            job.message("Import interrompu · reprise en attente")
                        if can_start and active < capacity and self.queue.claim(identity):
                            active += 1
                            job.outcomes = self.queue.outcomes(identity)
                            job.launch(fd)
            elif not job.done or job.final_event != {"done": "success", "cancelled": "cancelled"}.get(state, "failure"):
                job.journal = row["journal"]
                job.outcomes = self.queue.outcomes(identity)
                kind = {"done": "success", "cancelled": "cancelled"}.get(state, "failure")
                detail = read_final_detail(job.journal) if kind == "failure" else ""
                job.finish_import(kind, row["message"], 1., detail, notify=False)
            if row["refresh"] and job.done and self.refreshing.get(identity) != row["refresh"]:
                self.schedule_refresh(job, row["refresh"])
        visible = {row["id"] for row in rows}
        for job in list(self.imports):
            if job.identity not in visible and job.done:
                self.import_rows.remove(job)
                self.imports.remove(job)
                self.by_id.pop(job.identity)
        self.import_view.set_visible(bool(self.imports))
        self.update_queue_summary(paused, capacity, inbox, limitation)

    def update_queue_summary(self, paused, capacity, inbox, limitation):
        counts = self.queue.count()
        albums, tracks, settled, planned = self.queue.totals()
        active, waiting = counts.get("running", 0), counts.get("queued", 0)
        errors = counts.get("failed", 0) + counts.get("partial", 0)
        downloads = slot_count(inbox / ".kara-transferts", DOWNLOADS)
        processing = slot_count(inbox / ".kara-traitements", PROCESSING)
        running = [job for job in self.imports if job.process is not None]
        progressed = sum(job.album_fraction for job in running)
        progress = min(1., (settled + progressed) / planned) if planned else 0.
        owner_rows = [job.snapshot() for job in reversed(self.imports)]
        self.imports_snapshot = (owner_rows, dict(active=active, waiting=waiting, albums=albums, tracks=tracks,
                                                  errors=errors, downloads=downloads, processing=processing,
                                                  progress=progress, paused=paused, batches=capacity,
                                                  limitation=CATEGORIES["space"] if limitation else "",
                                                  max_batches=MAX_BATCHES))
        summary = (f"{active} lot(s) actif(s) · {waiting} en attente · {albums} album(s) terminé(s) · "
                   f"{tracks} piste(s) traitée(s) · {errors} erreur(s)\n"
                   f"{downloads}/{DOWNLOADS} téléchargement(s) · {processing}/{PROCESSING} traitement(s)")
        self.queue_status.set_text(f"{summary}\n{limitation}" if limitation else summary)
        self.overall.set_fraction(progress)
        self.overall.set_text(f"Progression globale · {progress:.0%}")

    def import_finished(self, job):
        # A normal worker already committed. A failed spawn or an abrupt
        # worker exit still needs a terminal record and an explicit retry.
        self.queue.finish(job.identity, job.final_event, job.status.get_text())
        self.schedule_refresh(job, self.queue.job(job.identity)["refresh"])
        self.next_poll = 0

    def schedule_refresh(self, job, generation):
        job.awaiting_refresh = bool(generation)
        job.refresh_generation = generation
        self.refreshing[job.identity] = generation
        self.on_update(job, "settled", {})

    def release_refresh(self, generations):
        for identity, generation in generations:
            if self.refreshing.get(identity) == generation:
                self.refreshing.pop(identity, None)

    def acknowledge_refresh(self, generations):
        self.queue.refreshed(generations)
        self.release_refresh(generations)

    def retry_job(self, identity):
        if self.queue.transition(identity, "failed", "queued", "Relance en attente…") or self.queue.transition(
                identity, "partial", "queued", "Relance en attente…"):
            self.launch_next(force=True)
            return True
        return False

    def cancel_job(self, identity):
        if self.queue.transition(identity, "queued", "cancelled", "Demande annulée", refresh=True):
            self.launch_next(force=True)
            return True
        job = self.by_id.get(identity)
        if job and job.stop_ready:
            job.stop_batch(None)
            return True
        return False


def read_final_detail(path):
    if not path:
        return ""
    try:
        details = ""
        with Path(path).open(encoding="utf-8") as log:
            for line in log:
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if payload.get("event") == "failure":
                    details = payload.get("detail", "")
        return details
    except OSError:
        return ""


class ImportJob(Gtk.Box):
    def __init__(self, jobs, on_finished, *, identity, option=None, retry=None, journal=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.identity = identity
        self.jobs = jobs
        self.option = option
        self.keys = {import_key(url) for _, url in jobs} if option is None else set()
        self.on_finished = on_finished
        self.retry = retry
        self.journal = journal
        self.done = False
        self.process = None
        self.stop_ready = False
        self.stopping = False
        self.fraction = 0.0
        self.album_fraction = 0.0
        self.final_event = None
        self.refresh_generation = 0
        self.awaiting_refresh = False
        self.store_state = "queued"
        self.measured = False
        self.result = None
        self.outcomes = {}
        self.unstructured = deque(maxlen=32)
        self.failed_jobs = []
        self.detail_text = ""
        self.timer = None
        self.on_update = lambda *_: None
        self.mobile_event = {}
        header = Gtk.Box(spacing=6)
        first_title, first_url = jobs[0]
        if len(jobs) == 1:
            title = first_title or "Import par lien"
        else:
            title = f"{len(jobs)} albums · {first_title}" if first_title else f"{len(jobs)} albums"
        self.title = title
        label = Gtk.Label(label=title, xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        label.set_tooltip_text(first_title or first_url)
        header.append(label)
        self.stop = Gtk.Button(label="Arrêter l’import", visible=option is None, sensitive=False)
        self.stop.set_tooltip_text("Termine les albums dont le téléchargement a commencé.")
        self.stop.connect("clicked", self.stop_batch)
        header.append(self.stop)
        self.details_button = Gtk.Button(label="Détails", visible=False)
        self.details_button.connect("clicked", lambda btn: show_details(
            btn, "Détails de l’import", self.detail_text, self.journal))
        header.append(self.details_button)
        self.retry_button = Gtk.Button(label="Relancer les échecs", visible=False)
        self.retry_button.connect("clicked", self.retry_failed)
        header.append(self.retry_button)
        self.append(header)
        self.progress = Gtk.ProgressBar(show_text=True)
        self.append(self.progress)
        self.status = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END, css_classes=["lyrics-info"])
        self.append(self.status)
        self.counts = Gtk.Label(xalign=0., css_classes=["lyrics-info"])
        self.append(self.counts)

    def message(self, text, *, failed=False):
        self.status.set_text(text)
        self.status.set_tooltip_text(text)
        if failed:
            self.status.add_css_class("import-error")
        else:
            self.status.remove_css_class("import-error")

    def launch(self, fd):
        self.message("Vérification de la collection…")
        try:
            launcher = Gio.SubprocessLauncher.new(PIPE)
            # Give the engine the same locked description used to claim the
            # job. A restarted GTK instance cannot schedule it twice.
            lease_fd = 9
            launcher.take_fd(os.dup(fd), lease_fd)
            # Imports already admitted can finish during a tour at lower CPU
            # priority than the real-time voice path.
            self.process = launcher.spawnv(["nice", "-n", "6", str(KARA), "--suivi-json", "--import-file", self.identity,
                                            "--import-lease", str(lease_fd)])
            launcher.close()
        except GLib.Error as error:
            self.finish_import("failure", "Impossible de lancer kara.", None, str(error))
            return False
        self.on_update(self, "running", {})
        self.timer = GLib.timeout_add(150, self.pulse)
        stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read_line)
        return True

    def update_counts(self):
        snapshot = self.snapshot()
        if self.option is None:
            self.counts.set_text(f"{snapshot['completed']}/{snapshot['albums']} album(s) terminé(s) · "
                                 f"{snapshot['processed']} piste(s) traitée(s) · {snapshot['errors']} erreur(s)")
        self.progress.set_text(f"{self.fraction:.0%}")

    def snapshot(self):
        status = self.status.get_text()
        state = ("partial" if any(item.get("tracks") for item in self.outcomes.values()) else "failed"
                 ) if self.final_event == "failure" else (
                 "cancelled" if self.final_event == "cancelled" else "done" if self.done else
                 "running" if self.process else self.store_state)
        if self.awaiting_refresh and state == "done":
            state, status = "integrating", "Actualisation de la bibliothèque"
        return dict(id=f"queue:{self.identity}", title=self.title, albums=len(self.jobs), tracks=None,
                    state=state, progress=self.fraction if self.measured else None, message=status, note="",
                    remaining=[{"title": title or (outcome or {}).get("title") or "Import par lien",
                                "message": (outcome or {}).get("message", "En attente"),
                                "errors": [CATEGORIES.get(entry.get("category"), "Erreur")
                                           for entry in (outcome or {}).get("diagnostics", [])]}
                               for title, _, outcome in self.visible_albums()],
                    completed=sum(item.get("status") in COMPLETE for item in self.outcomes.values()),
                    processed=len({path for item in self.outcomes.values() for path in item.get("tracks", [])}),
                    errors=sum(item.get("status") in {"failure", "partial"} for item in self.outcomes.values()))

    def stop_batch(self, _widget):
        if self.process is not None and self.stop_ready and not self.stopping:
            self.stopping = True
            self.stop.set_sensitive(False)
            self.process.send_signal(signal.SIGUSR1)
            self.message("Fin des albums dont le téléchargement a commencé…")

    def pulse(self):
        if not self.measured:
            self.progress.pulse()
        return GLib.SOURCE_CONTINUE

    def read_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
        except GLib.Error:
            line = None
            stream.close_async(GLib.PRIORITY_DEFAULT, None, None)
        if line is None:
            self.process.wait_async(None, self.finished)
            return
        self.update(line)
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read_line)

    def update(self, line):
        try:
            payload = json.loads(line)
        except ValueError:
            self.unstructured.append(line)
            return
        if not isinstance(payload, dict):
            return
        event = payload.get("event")
        text = payload.get("message")
        if not isinstance(text, str):
            return
        fraction = payload.get("fraction")
        if type(fraction) not in (float, int) or not 0 <= fraction <= 1:
            fraction = None
        if event == "journal":
            self.journal = payload.get("path")
        elif event == "album" and isinstance(payload.get("url"), str):
            self.outcomes[payload["url"]] = payload
            self.album_fraction = 0.
            if fraction is not None:
                self.fraction = max(self.fraction, fraction)
                self.progress.set_fraction(self.fraction)
            self.update_counts()
        elif event in {"success", "failure", "skip", "cancelled"}:
            details = self.diagnostics(payload)
            if isinstance(payload.get("detail"), str):
                details.insert(0, payload["detail"])
            self.result = (event, text, fraction, "\n\n".join(details))
        elif event == "progress":
            if self.option is None and not self.stop_ready:
                self.stop_ready = True
                self.stop.set_sensitive(not self.stopping)
            if fraction is not None:
                self.measured = True
                self.fraction = max(self.fraction, fraction)
                self.progress.set_fraction(self.fraction)
                self.progress.set_text(f"{self.fraction:.0%}")
            self.album_fraction = payload.get("album_fraction", 0.)
            processed = payload.get("processed", 0)
            self.message(f"{text} · {processed} piste(s) traitée(s)" if processed else text)
        self.on_update(self, event, payload)

    def finished(self, process, result):
        try:
            completed = process.wait_finish(result) and process.get_successful()
        except GLib.Error:
            completed = False
        event, text, fraction, detail = self.result or ("failure", "Kara s’est arrêté avant le bilan.", None, "")
        if not completed and event not in {"failure", "cancelled"}:
            event, text = "failure", "Kara s’est arrêté avec une erreur."
        self.finish_import(event, text, fraction, detail)

    @staticmethod
    def diagnostics(payload):
        return [f"{CATEGORIES.get(entry.get('category'), 'Erreur')} · {entry.get('stage')}\n{entry.get('detail')}"
                for entry in payload.get("diagnostics", [])]

    def finish_import(self, event, text, fraction, detail, *, notify=True, history=False):
        self.process = None
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self.done = True
        self.final_event = event
        self.awaiting_refresh = self.awaiting_refresh or notify
        self.stop.set_visible(False)
        self.fraction = max(self.fraction, fraction) if fraction is not None else self.fraction
        self.progress.set_fraction(self.fraction)
        self.update_counts()
        self.message(text, failed=event == "failure")
        self.final_detail = detail
        self.update_details(history=history)
        if notify:
            self.on_finished(self)

    def visible_albums(self, history=False):
        for title, url in self.jobs:
            outcome = self.outcomes.get(url)
            if (history or not self.done or self.awaiting_refresh or not outcome or
                    outcome.get("status") not in COMPLETE):
                yield title, url, outcome

    def update_details(self, history=False):
        sections = [self.status.get_text()]
        if self.final_detail:
            sections.append(self.final_detail)
        if self.unstructured:
            sections.append("\n".join(self.unstructured))
        self.failed_jobs = []
        for title, url, result in self.visible_albums(history):
            if result:
                heading = f"{title or result['title']} · {result['message']} · {result['added']} piste(s) ajoutée(s)"
                lines = [heading, url, *self.diagnostics(result)]
                if result.get("remaining"):
                    lines.append(f"Fichiers conservés dans {result['remaining']}")
                sections.append("\n".join(lines))
            if not self.option and self.final_event == "failure" and (
                result is None or result.get("status") in {"failure", "partial"}
            ):
                self.failed_jobs.append((title, url))
        if self.journal:
            sections.append(f"Journal complet\n{self.journal}")
        self.detail_text = "\n\n".join(sections)
        self.details_button.set_visible(len(sections) > 1)
        self.retry_button.set_visible(bool(self.failed_jobs and self.retry))

    def retry_failed(self, _button):
        if self.failed_jobs and self.retry(self.identity):
            self.retry_button.set_visible(False)
