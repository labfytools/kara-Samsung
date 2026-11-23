"""Recherche et import d’albums dans la fenêtre de paroles."""
import json
from pathlib import Path
import signal
from urllib.parse import parse_qs, urlsplit

from gi.repository import Gio, GLib, Gtk, Pango

BIN = Path(__file__).resolve().parent
KARA = BIN / "kara"
CATALOGUE = BIN / "_kara_catalogue.py"
CATALOGUE_PYTHON = Path.home() / ".local/share/kara/catalogue/bin/python"
PIPE = Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_MERGE


class AlbumImport(Gtk.Revealer):
    def __init__(self):
        super().__init__(transition_type=Gtk.RevealerTransitionType.SLIDE_DOWN)
        self.lookup = None
        self.lookup_live = False
        self.suggestion_timer = None
        self.suggestion_query = ""
        self.suggestions = []
        self.imports = []
        self.albums = []

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

        self.status = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END, visible=False,
                                css_classes=["lyrics-info"])
        panel.append(self.status)
        self.import_rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.import_view = Gtk.ScrolledWindow(hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              propagate_natural_height=True, max_content_height=185, visible=False)
        self.import_view.set_child(self.import_rows)
        panel.append(self.import_view)

    @property
    def running(self):
        return any(not job.done for job in self.imports)

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

    def message(self, text, *, failed=False):
        self.status.set_text(text)
        self.status.set_tooltip_text(text)
        self.status.set_visible(True)
        if failed:
            self.status.add_css_class("import-error")
        else:
            self.status.remove_css_class("import-error")

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
        self.status.set_visible(False)
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
            self.lookup = Gio.Subprocess.new(
                [str(CATALOGUE_PYTHON), "-B", str(CATALOGUE), action, value], PIPE
            )
        except GLib.Error:
            if not live:
                self.message("Impossible d’ouvrir le catalogue YouTube Music.", failed=True)
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
        try:
            _, output, _ = process.communicate_utf8_finish(result)
            payload = json.loads(output)
        except (GLib.Error, ValueError, TypeError):
            if not live:
                self.message("La recherche YouTube Music a échoué.", failed=True)
            return
        if not process.get_successful() or "data" not in payload:
            if not live:
                self.message(payload.get("error") or "La recherche YouTube Music a échoué.", failed=True)
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
        self.status.set_visible(False)
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
        self.status.set_visible(False)
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

    def start_queue(self, jobs):
        occupied = {key for job in self.imports if not job.done for key in job.keys}
        pending = []
        for title, url in jobs:
            key = import_key(url)
            if key not in occupied:
                pending.append((title, url))
                occupied.add(key)
        if not pending:
            self.message("Cet import est déjà en cours.")
            return False
        self.status.set_visible(False)
        return self.add_import(ImportJob(pending, self.import_finished))

    def start_maintenance(self, option, title):
        if any(job.option == option and not job.done for job in self.imports):
            self.message(f"{title} · recherche déjà en cours.")
            return False
        self.status.set_visible(False)
        return self.add_import(ImportJob([(title, "")], self.import_finished, option=option))

    def add_import(self, job):
        self.imports.append(job)
        self.import_rows.prepend(job)
        self.import_view.set_visible(True)
        return job.launch()

    def import_finished(self, _job):
        completed = [job for job in self.imports if job.done]
        for job in completed[:-6]:
            self.import_rows.remove(job)
            self.imports.remove(job)


def import_key(url):
    try:
        parsed = urlsplit(url)
    except ValueError:
        return ("url", url)
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
    return ("url", url)


class ImportJob(Gtk.Box):
    def __init__(self, jobs, on_finished, *, option=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.jobs = jobs
        self.option = option
        self.keys = {import_key(url) for _, url in jobs} if option is None else set()
        self.on_finished = on_finished
        self.done = False
        self.process = None
        self.stop_ready = False
        self.stopping = False
        self.fraction = 0.0
        self.measured = False
        self.result = None
        self.timer = None
        header = Gtk.Box(spacing=6)
        first_title, first_url = jobs[0]
        if len(jobs) == 1:
            title = first_title or "Import par lien"
        else:
            title = f"{len(jobs)} albums · {first_title}" if first_title else f"{len(jobs)} albums"
        label = Gtk.Label(label=title, xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        label.set_tooltip_text(first_title or first_url)
        header.append(label)
        self.stop = Gtk.Button(label="Arrêter le lot", visible=len(jobs)>1, sensitive=False)
        self.stop.set_tooltip_text("Termine les albums dont le téléchargement a commencé.")
        self.stop.connect("clicked", self.stop_batch)
        header.append(self.stop)
        self.append(header)
        self.progress = Gtk.ProgressBar()
        self.append(self.progress)
        self.status = Gtk.Label(xalign=0.0, ellipsize=Pango.EllipsizeMode.END, css_classes=["lyrics-info"])
        self.append(self.status)

    def message(self, text, *, failed=False):
        self.status.set_text(text)
        self.status.set_tooltip_text(text)
        if failed:
            self.status.add_css_class("import-error")
        else:
            self.status.remove_css_class("import-error")

    def launch(self):
        urls = [url for _, url in self.jobs]
        if self.option:
            arguments = [self.option]
        else:
            arguments = ["--lot", *urls] if len(urls) > 1 else ["--", urls[0]]
        self.message("Vérification de la collection…")
        try:
            self.process = Gio.Subprocess.new([str(KARA), "--suivi-json", *arguments], PIPE)
        except GLib.Error:
            self.finish_import("failure", "Impossible de lancer kara.", None)
            return False
        self.timer = GLib.timeout_add(110, self.pulse)
        stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read_line)
        return True

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
        if event in {"success", "failure", "skip"}:
            self.result = (event, text, fraction)
        elif event == "progress":
            if len(self.jobs) > 1 and not self.stop_ready:
                self.stop_ready = True
                self.stop.set_sensitive(not self.stopping)
            if fraction is not None:
                self.measured = True
                self.fraction = max(self.fraction, fraction)
                self.progress.set_fraction(self.fraction)
            self.message(text)

    def finished(self, process, result):
        try:
            completed = process.wait_finish(result) and process.get_successful()
        except GLib.Error:
            completed = False
        event, text, fraction = self.result or (
            ("success", "Import terminé.", 1.0) if completed else ("failure", "Kara s’est arrêté avant la fin.", None)
        )
        self.finish_import(event if completed else "failure", text, fraction)

    def finish_import(self, event, text, fraction):
        self.process = None
        if self.timer is not None:
            GLib.source_remove(self.timer)
            self.timer = None
        self.done = True
        self.stop.set_visible(False)
        self.progress.set_fraction(max(self.fraction, fraction) if fraction is not None else self.fraction)
        self.message(text, failed=event == "failure")
        self.on_finished(self)
