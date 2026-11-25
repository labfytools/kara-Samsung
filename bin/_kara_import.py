"""Recherche et import d’albums dans la fenêtre de paroles."""
from collections import deque
from dataclasses import asdict
import json
import os
from pathlib import Path
import signal
import sqlite3
from time import monotonic
from urllib.parse import urlsplit

from gi.repository import Gio, GLib, Gtk, Pango

from _kara_import_local import LOCAL, local_target, local_url
from _kara_import_queue import COMPLETE, HISTORY_DAYS, ImportQueue, MAX_BATCHES, import_key
from _kara_import_resources import DOWNLOADS, PROCESSING, WORKING_RESERVE, check_space, lease, slot_count
from _kara_import_result import CATEGORIES, ImportIssue
from _musique_oz import AUDIO, INBOX, SHARED_LIBRARY
from _kara_paths import BIN, catalogue_command
from _parole_import_providers import (ALL, ARCHIVE, JAMENDO, MANUAL, OPEN_LICENSES, YOUTUBE,
                                      archive_identifier, jamendo_identifier)
from _parole_jamendo import client_id as jamendo_client
from _parole_import_metrics import LOOKUP, observe
from _parole_index import encoded_key
from _parole_music_identity import CONFIRMED, PROBABLE, compare as compare_editions, from_catalogue, normalized
from _parole_music_registry import approved_aliases
from _parole_offer import create_plan, offers_are_equivalent, valid_offer

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
        self.provider_enabled = lambda _: True
        self.measure_index = None
        self.albums = []
        self.artist_routes = {}
        self.edition_facts = {}
        self.edition_reviews = {}
        self.offers = {}
        self.offer_rows = {}
        self.by_id = {}
        self.imports_snapshot = ([], {})
        self.next_poll = 0
        self.next_history_prune = 0
        self.history_window = None
        self.refresh_history = None
        self.closing = False
        self.refreshing = {}
        self.queue_error = ""
        try:
            self.queue = ImportQueue()
        except (OSError, sqlite3.Error) as error:
            self.queue = None
            self.queue_error = str(error)

        panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, hexpand=True, vexpand=True,
                        css_classes=["album-import"])
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
        self.link = Gtk.Entry(placeholder_text="Lien d’album", hexpand=True, max_length=4096)
        self.link.connect("changed", self.refresh_controls)
        self.link.connect("activate", self.start)
        form.append(self.link)
        self.submit = Gtk.Button(label="Télécharger", sensitive=False, css_classes=["fetch"])
        self.submit.connect("clicked", self.start)
        form.append(self.submit)
        self.local_menu = Gtk.MenuButton(label=LOCAL, sensitive=self.queue is not None)
        local_popover = Gtk.Popover()
        local_choices = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        for caption, mode in (("Choisir un dossier d’album", "folder"), ("Choisir une archive ZIP", "zip"),
                              ("Choisir un fichier audio", "audio")):
            button = Gtk.Button(label=caption)
            button.connect("clicked", lambda _button, mode=mode: self.choose_local(local_popover, mode))
            local_choices.append(button)
        local_popover.set_child(local_choices)
        self.local_menu.set_popover(local_popover)
        form.append(self.local_menu)

        provider_row = Gtk.Box(spacing=8)
        self.provider = Gtk.DropDown()
        provider_row.append(self.provider)
        self.official_download = Gtk.LinkButton(label="Ouvrir le téléchargement officiel", visible=False, uri="")
        provider_row.append(self.official_download)
        self.download_help = Gtk.LinkButton(label="Aide du fournisseur", visible=False, uri="")
        provider_row.append(self.download_help)
        self.download_license = Gtk.DropDown.new_from_strings(["Licence du titre", *OPEN_LICENSES])
        self.download_license.set_visible(False)
        provider_row.append(self.download_license)
        panel.append(provider_row)

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
        self.pause.set_tooltip_text("Suspend les imports et leurs enrichissements en cours. Les données reprises sont conservées.")
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
        # The imports own the remaining height. Search results have their
        # own bounded viewport so the controls and footer stay accessible.
        self.import_view = Gtk.ScrolledWindow(hexpand=True, vexpand=True,
                                              hscrollbar_policy=Gtk.PolicyType.NEVER,
                                              vscrollbar_policy=Gtk.PolicyType.AUTOMATIC)
        self.import_view.set_child(self.import_rows)
        panel.append(self.import_view)
        self.history_button = Gtk.Button(label="Historique", halign=Gtk.Align.END, sensitive=self.queue is not None)
        self.history_button.connect("clicked", self.show_history)
        panel.append(self.history_button)
        self.provider.connect("notify::selected", self.change_provider)
        self.refresh_providers()
        GLib.idle_add(self.launch_next)

    def show_history(self, button):
        if self.history_window is not None:
            self.history_window.present()
            return
        try:
            days, _ = self.queue.history_policy()
        except (OSError, sqlite3.Error) as error:
            self.message(f"Historique indisponible · {error}", failed=True)
            return
        window = Gtk.Window(title="Historique des importations", transient_for=button.get_root(),
                            destroy_with_parent=True, default_width=740, default_height=460)
        self.history_window = window
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10, css_classes=["panel-content", "import-history"])
        actions = Gtk.Box(spacing=6)
        remove = Gtk.Button(label="Supprimer la sélection", sensitive=False)
        successful = Gtk.Button(label="Effacer les importations réussies")
        clear = Gtk.Button(label="Effacer tout l’historique", css_classes=["destructive-action"])
        for action in (remove, successful, clear):
            actions.append(action)
        content.append(actions)
        retention_row = Gtk.Box(spacing=6)
        retention_label = Gtk.Label(label="Nettoyage automatique")
        retention = Gtk.DropDown.new_from_strings(["Conserver indéfiniment", "Après 7 jours", "Après 30 jours", "Après 90 jours"])
        retention_label.set_mnemonic_widget(retention)
        retention_row.append(retention_label)
        retention_row.append(retention)
        content.append(retention_row)
        message = Gtk.Label(xalign=0, wrap=True, css_classes=["muted"])
        content.append(message)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        rows = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE)
        scroll.set_child(rows)
        content.append(scroll)
        more = Gtk.Button(label="Imports précédents")
        content.append(more)
        window.set_child(content)
        cursor = None

        def append_page(_button=None):
            nonlocal cursor
            try:
                page = self.queue.history(cursor, limit=26)
                for row in page[:25]:
                    job = ImportJob(row["targets"], None, identity=row["id"], option=row["option"],
                                    journal=row["journal"])
                    job.outcomes = self.queue.outcomes(row["id"])
                    event = {"done": "success", "cancelled": "cancelled"}.get(row["state"], "failure")
                    job.finish_import(event,
                                      row["message"], 1., "", notify=False, history=True)
                    selection = Gtk.ListBoxRow(child=job, activatable=False)
                    rows.append(selection)
                    cursor = row["seq"]
                more.set_visible(len(page) > 25)
                successful.set_sensitive(cursor is not None)
                clear.set_sensitive(cursor is not None)
            except (OSError, sqlite3.Error) as error:
                message.set_text(f"Historique indisponible · {error}")

        def reload_history():
            nonlocal cursor
            cursor = None
            while row := rows.get_first_child():
                rows.remove(row)
            append_page()

        def apply(change, *, retention_change=False):
            try:
                removed = change()
            except (OSError, sqlite3.Error, ValueError) as error:
                message.set_text(f"Nettoyage impossible · {error}")
                return
            self.launch_next(force=True)
            message.set_text(f"{removed} entrée(s) retirée(s) de l’historique." if removed else
                             "Conservation mise à jour." if retention_change else "Aucune entrée à effacer.")

        def configure_history(widget, _property):
            selected = widget.get_selected()
            if selected < len(HISTORY_DAYS):
                apply(lambda: self.queue.configure_history(HISTORY_DAYS[selected]), retention_change=True)

        def confirm_clear(_button):
            confirm = Gtk.AlertDialog(message="Effacer tout l’historique des importations ?",
                                      detail="Les entrées disparaîtront aussi sur le mobile.",
                                      buttons=["Annuler", "Effacer"], cancel_button=0, default_button=0)

            def confirmed(dialog, result):
                try:
                    approved = dialog.choose_finish(result) == 1
                except GLib.Error:
                    return
                if approved:
                    apply(self.queue.delete_history)

            confirm.choose(window, None, confirmed)

        def closed(_window):
            if self.history_window is window:
                self.history_window = self.refresh_history = None

        def close_requested(widget):
            closed(widget)
            return False

        def remove_selected(_button):
            identities = [row.get_child().identity for row in rows.get_selected_rows()]
            apply(lambda: self.queue.delete_history(identities))

        rows.set_placeholder(Gtk.Label(label="Aucune importation terminée.", css_classes=["muted"]))
        rows.connect("selected-rows-changed", lambda box: remove.set_sensitive(bool(box.get_selected_rows())))
        remove.connect("clicked", remove_selected)
        successful.connect("clicked", lambda _: apply(lambda: self.queue.delete_history(successful=True)))
        clear.connect("clicked", confirm_clear)
        retention.set_selected(HISTORY_DAYS.index(days))
        retention.connect("notify::selected", configure_history)
        window.connect("close-request", close_requested)
        window.connect("destroy", closed)
        self.refresh_history = reload_history
        more.connect("clicked", append_page)
        append_page()
        window.present()

    @property
    def running(self):
        return bool(self.imports_snapshot[1].get("active") or self.imports_snapshot[1].get("waiting"))

    def toggle(self, button):
        opened = button.get_active()
        self.set_reveal_child(opened)
        if opened and self.lookup is None:
            self.refresh_providers()
            (self.artist if self.catalogue_available() else self.link).grab_focus()
        elif not opened:
            self.cancel_suggestions()

    def selected_provider(self):
        selected = self.provider.get_selected_item()
        return selected.get_string() if selected else ALL

    def refresh_providers(self):
        choices = [ALL, LOCAL]
        try:
            choices.extend(name for name in (YOUTUBE, ARCHIVE, *MANUAL) if self.provider_enabled(name))
        except (OSError, sqlite3.Error):
            choices.extend((YOUTUBE, ARCHIVE, *MANUAL))
        model = self.provider.get_model()
        current = [model.get_string(i) for i in range(model.get_n_items())] if model else []
        if choices != current:
            selected = self.selected_provider()
            self.provider.set_model(Gtk.StringList.new(choices))
            self.provider.set_selected(choices.index(selected) if selected in choices else 0)

    def can_search(self, source):
        provider = self.selected_provider()
        if provider != ALL:
            return provider == source
        model = self.provider.get_model()
        return bool(model and any(model.get_string(i) == source for i in range(model.get_n_items())))

    def can_search_youtube(self):
        return self.can_search(YOUTUBE)

    def can_search_archive(self):
        return self.can_search(ARCHIVE)

    def can_search_jamendo(self):
        return bool(jamendo_client() and self.can_search(JAMENDO))

    def catalogue_available(self):
        return self.can_search_youtube() or self.can_search_archive() or self.can_search_jamendo()

    def change_provider(self, *_args):
        self.cancel_search()
        self.suggestions = []
        self.suggestion_query = ""
        self.clear_results()
        self.hide_message()
        route = MANUAL.get(self.selected_provider())
        self.download_help.set_visible(bool(route and route.help))
        self.download_license.set_visible(bool(route and route.licenses))
        self.download_license.set_selected(0)
        if route:
            self.download_help.set_uri(route.help)
        reference = ("Page publique de l’édition" if route and route.require_reference else
                     "Page publique de l’édition (facultatif)" if route else "Lien d’album")
        self.link.set_placeholder_text(reference)
        selected = self.selected_provider()
        self.artist.set_placeholder_text("Artiste ou album" if selected in (ARCHIVE, JAMENDO) else "Artiste")
        manual = selected == LOCAL or route is not None and not (selected == JAMENDO and self.can_search_jamendo())
        self.submit.set_label("Importer" if manual else "Télécharger")
        self.refresh_controls()

    def refresh_controls(self, _widget=None):
        idle = self.lookup is None or self.lookup_live
        self.artist.set_sensitive(idle and self.catalogue_available())
        self.results.set_sensitive(idle)
        provider = self.selected_provider()
        url = self.link.get_text().strip()
        route = MANUAL.get(provider)
        download = route.downloads if route else ""
        if route and not download and url:
            try:
                download = route.release_reference(url)
            except ImportIssue:
                pass
        self.official_download.set_visible(bool(download))
        if download:
            self.official_download.set_uri(download)
        self.submit.set_sensitive(self.queue is not None and (provider in MANUAL or provider == LOCAL
                                  or bool(url) and (self.catalogue_available() or local_url(url))))
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
        self.artist_routes.clear()
        self.edition_facts.clear()
        self.edition_reviews.clear()
        self.offers.clear()
        self.offer_rows.clear()
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
        if self.get_reveal_child() and self.can_search_youtube() and len(query) >= 2:
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
        if not query or not self.catalogue_available():
            return
        self.cancel_suggestions()
        if self.lookup is not None:
            return
        self.clear_results()
        if not self.can_search_youtube():
            self.search_next_catalogue(YOUTUBE, query)
            return
        if query == self.suggestion_query and self.suggestions:
            self.artists_found(self.suggestions)
            return
        self.message("Recherche de l’artiste sur YouTube Music…")
        self.search("artistes", query, self.artists_found)

    def search(self, action, value, callback, *, live=False):
        source = {"archive": ARCHIVE, "jamendo": JAMENDO}.get(action, YOUTUBE)
        mode = {"archive": "--chercher-archives", "jamendo": "--chercher-jamendo"}.get(action)
        started = monotonic()
        try:
            command = [str(KARA), mode, value] if mode else catalogue_command(action, value)
            self.lookup = Gio.Subprocess.new(command, PIPE)
        except (GLib.Error, ValueError) as error:
            if not live:
                category = "path" if isinstance(error, ValueError) else "dependency"
                self.message(CATEGORIES[category], failed=True, detail=str(error))
            return
        self.lookup_live = live
        if not live:
            self.on_update(None, "provider", {"provider": source, "message": f"{source} · recherche de catalogue"})
        self.refresh_controls()
        self.lookup.communicate_utf8_async(None, None, self.search_finished, (callback, source, started))

    def stop_lookup(self):
        if self.lookup is not None:
            self.lookup.force_exit()
            self.lookup = None
        self.lookup_live = False

    def cancel_search(self):
        self.cancel_suggestions()
        self.stop_lookup()

    def search_finished(self, process, result, call):
        if process != self.lookup:
            return
        live = self.lookup_live
        callback, source, started = call
        self.lookup = None
        self.lookup_live = False
        if source == YOUTUBE and not live and self.measure_index is not None:
            observe(self.measure_index, source, {LOOKUP: monotonic() - started})
        self.refresh_controls()
        output = ""
        try:
            _, output, _ = process.communicate_utf8_finish(result)
            payload = json.loads(output)
            if not isinstance(payload, dict):
                raise ValueError("Réponse du catalogue invalide.")
        except (GLib.Error, ValueError, TypeError) as error:
            if not live:
                self.message(f"La recherche {source} a échoué.", failed=True, detail=output or str(error))
                self.search_next_catalogue(source, self.artist.get_text().strip())
            return
        if not process.get_successful() or "data" not in payload:
            if not live:
                text = CATEGORIES.get(payload.get("category"), f"La recherche {source} a échoué.")
                self.message(text, failed=True, detail=payload.get("error") or output)
                self.search_next_catalogue(source, self.artist.get_text().strip())
            return
        callback(payload["data"])

    def artists_found(self, artists):
        query = self.artist.get_text().strip().casefold()
        exact = [item for item in artists if item["name"].casefold() == query]
        if len(exact) == 1 or len(artists) == 1:
            self.get_albums((exact or artists)[0])
            return
        if not artists:
            if not self.search_next_catalogue(YOUTUBE, self.artist.get_text().strip()):
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
        artist = data["name"] or self.artist.get_text().strip()
        if not self.albums:
            if not self.search_next_catalogue(YOUTUBE, artist):
                self.message("Aucun album trouvé pour cet artiste.")
            return
        self.results_title.set_text(f"{artist} · {len(self.albums)} album(s)")
        self.results_header.set_visible(True)
        self.results_view.set_visible(True)
        self.download_all.set_visible(True)
        self.hide_message()
        for album in data["albums"]:
            job = (album["title"], album["url"])
            if data.get("artist_id"):
                self.artist_routes[job[1]] = artist, data["artist_id"], album["id"]
            self.edition_facts[job[1]] = (YOUTUBE, from_catalogue({**album, "artist": artist}))
            text = " · ".join(str(part) for part in (album["title"], album["year"]) if part)
            self.add_row(f"{YOUTUBE} · {text}", "Télécharger", lambda _button, selected=job: self.start_queue([selected]))
        self.refresh_controls()
        self.search_next_catalogue(YOUTUBE, artist)

    def search_next_catalogue(self, previous, query):
        if not query:
            return False
        if previous == YOUTUBE and self.can_search_archive():
            source, action = ARCHIVE, "archive"
        elif previous in (YOUTUBE, ARCHIVE) and self.can_search_jamendo():
            source, action = JAMENDO, "jamendo"
        else:
            return False
        self.message(f"Recherche sur {source}…")
        self.search(action, query, lambda albums: self.remote_results(albums, source, query))
        return True

    def remote_results(self, albums, source, query):
        for album in albums:
            target = (album["title"], album["url"])
            self.albums.append(target)
            formats = ", ".join(album["formats"])
            license_name = next((name for name, url in OPEN_LICENSES.items() if url == album["license"]), "")
            quality = f"{formats} d’origine" if source == ARCHIVE else f"{formats} demandé"
            comparison = self.compare_result(source, album)
            text = " · ".join(part for part in (source, album["artist"], album["title"],
                              f"{album['tracks']} piste(s)", quality, "Gratuit", license_name, comparison) if part)
            related = self.equivalent_offers(album)
            self.offers[target[1]] = album
            if self.measure_index is not None and related:
                existing = self.offer_rows[related[0]["url"]]
                label = existing.get_first_child()
                text = (f"{album['artist']} · {album['title']} · {album['tracks']} piste(s) · Gratuit · "
                        + " / ".join(f"{offer['provider']} ({', '.join(offer['formats'])})" for offer in [*related, album]))
                label.set_label(text)
                label.set_tooltip_text(text)
                self.offer_rows[target[1]] = existing
            else:
                row = self.offer_rows[target[1]] = self.add_row(
                    text, "Télécharger", lambda _button, selected=target: self.start_offer(selected))
                if comparisons := self.edition_reviews.get(target[1]):
                    detail = Gtk.Button(label="Comparer")
                    detail.connect("clicked", lambda widget, values=comparisons: show_details(
                        widget, "Correspondances d’édition", json.dumps(values, ensure_ascii=False, indent=2)))
                    row.append(detail)
        if self.albums:
            self.results_title.set_text(f"{len(self.albums)} résultat(s)")
            self.results_header.set_visible(True)
            self.results_view.set_visible(True)
            self.download_all.set_visible(self.selected_provider() != ALL)
            self.hide_message()
        else:
            self.message("Aucune édition à licence compatible trouvée.")
        self.refresh_controls()
        self.search_next_catalogue(source, query)

    def compare_result(self, source, offer):
        facts = from_catalogue(offer.get("facts"))
        if facts is None:
            return ""
        try:
            aliases = approved_aliases(self.measure_index, facts.title)
        except sqlite3.Error:
            aliases = ()
        verdicts = set()
        reviews = []
        for url, (existing_source, existing) in self.edition_facts.items():
            if existing_source == source or existing is None or normalized(existing.title) != normalized(facts.title):
                continue
            result = compare_editions(existing, facts, aliases)
            verdicts.add(result.confidence)
            data = {"confiance": result.confidence, "preuves": result.reasons, "positions": result.positions,
                    "sources": [{"fournisseur": existing_source, "lien": url, "édition": asdict(existing)},
                                {"fournisseur": source, "lien": offer["url"], "édition": asdict(facts)}]}
            reviews.append(data)
            if self.measure_index is not None:
                try:
                    identity = encoded_key("comparison-edition", sorted((url, offer["url"])))
                    self.measure_index.review("identity", identity, result.confidence, data)
                except sqlite3.Error:
                    pass
            self.on_update(None, "provider", {
                "provider": source, "message": f"{existing_source} / {source} · {facts.title} · {result.confidence} · "
                                              + ", ".join(result.reasons),
            })
        self.edition_facts[offer["url"]] = (source, facts)
        self.edition_reviews[offer["url"]] = reviews
        if CONFIRMED in verdicts:
            return "Édition confirmée"
        return "Édition à vérifier" if PROBABLE in verdicts else ""

    def equivalent_offers(self, candidate):
        if self.selected_provider() != ALL or not valid_offer(candidate):
            return []
        try:
            return [other for other in self.offers.values()
                    if valid_offer(other) and offers_are_equivalent(other, candidate, index=self.measure_index)]
        except sqlite3.Error:
            return []

    def start_offer(self, target):
        title, url = target
        offer = self.offers[url]
        candidates = self.equivalent_offers(offer)
        if self.measure_index is not None and candidates:
            try:
                url = create_plan(self.measure_index, [offer, *candidates])
            except (ValueError, OSError, sqlite3.Error) as error:
                self.message("Impossible d’enregistrer l’édition multisource.", failed=True, detail=str(error))
                return
        self.start_queue([(title, url)])

    def add_row(self, text, action, handler):
        row = Gtk.Box(spacing=8, margin_start=8, margin_end=8, margin_top=4, margin_bottom=4)
        label = Gtk.Label(label=text, xalign=0.0, hexpand=True, ellipsize=Pango.EllipsizeMode.END)
        label.set_tooltip_text(text)
        row.append(label)
        button = Gtk.Button(label=action)
        button.connect("clicked", handler)
        row.append(button)
        self.results.append(row)
        return row

    def start_all(self, _widget):
        self.start_queue(self.albums)

    def start(self, _widget):
        url = self.link.get_text().strip()
        provider = self.selected_provider()
        try:
            parsed = urlsplit(url)
        except ValueError:
            self.message("Lien d’album invalide.", failed=True)
            return
        from_archive = bool(archive_identifier(parsed))
        from_jamendo = bool(jamendo_identifier(parsed))
        jamendo_download = provider == JAMENDO and self.can_search_jamendo() and from_jamendo
        if provider in MANUAL and not jamendo_download or provider == LOCAL and not local_url(url):
            self.local_menu.popup()
            return
        if provider == YOUTUBE and local_url(url):
            self.message("Sélectionne Fichiers locaux pour importer ce chemin.", failed=True)
            return
        if (provider == ARCHIVE and not from_archive or provider == YOUTUBE and (from_archive or from_jamendo)) and url:
            self.message(f"Sélectionne un lien du fournisseur {provider}.", failed=True)
            return
        if url and self.start_queue([("", url)]):
            self.link.set_text("")
            self.link.grab_focus()

    def choose_local(self, popover, mode):
        popover.popdown()
        provider = self.selected_provider()
        reference = self.link.get_text().strip() if provider in MANUAL else ""
        license_name = self.download_license.get_selected_item()
        license_url = OPEN_LICENSES.get(license_name.get_string(), "")
        route = MANUAL.get(provider)
        if route:
            try:
                route.provenance(reference, license_url)
            except ImportIssue as error:
                self.message(str(error), failed=True)
                return
        chooser = Gtk.FileChooserNative.new(
            {"folder": "Choisir un dossier d’album", "zip": "Choisir une archive ZIP",
             "audio": "Choisir un fichier audio"}[mode], self.get_root(),
            Gtk.FileChooserAction.SELECT_FOLDER if mode == "folder" else Gtk.FileChooserAction.OPEN,
            "Importer", "Annuler")
        if mode != "folder":
            allowed = Gtk.FileFilter(name="Archives ZIP" if mode == "zip" else "Fichiers audio")
            for suffix in ("zip",) if mode == "zip" else sorted(ext[1:] for ext in AUDIO):
                allowed.add_suffix(suffix)
            chooser.add_filter(allowed)

        def selected(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                chosen = dialog.get_file()
                if chosen is not None and chosen.is_native() and chosen.get_uri_scheme() == "file":
                    try:
                        selected_uri = local_target(chosen.get_uri(), provider, reference, license_url)
                    except ImportIssue as error:
                        self.message(str(error), failed=True)
                    else:
                        self.start_queue([(chosen.get_basename(), selected_uri)])
                else:
                    self.message("Sélectionne des fichiers sur le disque local.", failed=True)
            dialog.destroy()

        chooser.connect("response", selected)
        chooser.show()

    def start_queue(self, jobs, *, journal=None):
        return self.enqueue(jobs, journal=journal) is not None

    def enqueue(self, jobs, *, journal=None, request=None, option=None):
        try:
            if self.queue is None:
                raise OSError(self.queue_error)
            check_space(self.queue.directory, state=True)
            routes = {url: self.artist_routes[url] for _, url in jobs if url in self.artist_routes}
            identity = self.queue.enqueue(jobs, journal=journal, option=option, request=request, artist_routes=routes)
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
            changed = self.queue.configure(**settings)
        except (OSError, sqlite3.Error, ValueError) as error:
            self.message(str(error), failed=True)
            return False
        if changed:
            message = ("Pause de la file demandée" if settings["paused"] else
                       "Reprise de la file · lots et enrichissements en attente réactivés")
            self.on_update(None, "queue", {"message": message})
        self.launch_next(force=True)
        return True

    def add_import(self, row):
        job = ImportJob(row["targets"], self.import_finished, identity=row["id"], option=row["option"],
                        retry=self.retry_job, journal=row["journal"], remove=self.remove_job)
        job.outcomes = self.queue.outcomes(job.identity)
        self.by_id[job.identity] = job
        self.imports.append(job)
        job.update_counts()
        job.on_update = self.changed
        self.import_rows.prepend(job)
        return job

    def changed(self, job, kind, payload):
        if kind == "progress":
            self.on_update(job, kind, payload)
        elif kind in {"finished", "album", "running", "provider", "suspended"}:
            self.on_update(job, kind, payload)
            self.next_poll = 0

    def launch_next(self, force=False):
        now = monotonic()
        if self.queue is None or self.closing or not force and now < self.next_poll:
            return GLib.SOURCE_REMOVE
        self.next_poll = now + 0.75
        try:
            if now >= self.next_history_prune:
                self.queue.prune_history()
                self.next_history_prune = now + 60
            self.sync_queue()
        except (OSError, sqlite3.Error) as error:
            self.queue_status.set_text(f"File indisponible · {error}")
        return GLib.SOURCE_REMOVE

    def sync_queue(self):
        rows = self.queue.rows(include_hidden_refresh=True)
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
        self.concurrency.handler_block(self.concurrency_handler)
        self.concurrency.set_value(capacity)
        self.concurrency.handler_unblock(self.concurrency_handler)
        active = sum(row["state"] == "running" for row in rows)
        for row in rows:
            identity, state = row["id"], row["state"]
            if not row["visible"]:
                # Clearing a lot hides the card immediately. The independently
                # persisted catalogue acknowledgement must still be replayed.
                if row["refresh"] and self.refreshing.get(identity) != row["refresh"]:
                    task = ImportJob(row["targets"], None, identity=identity, option=row["option"],
                                     journal=row["journal"])
                    task.outcomes = self.queue.outcomes(identity)
                    event = {"done": "success", "cancelled": "cancelled"}.get(state, "failure")
                    task.finish_import(event, row["message"], 1., "", notify=False, history=True)
                    self.schedule_refresh(task, row["refresh"])
                continue
            job = self.by_id.get(identity) or self.add_import(row)
            job.awaiting_refresh = bool(row["refresh"])
            job.store_state = state
            job.queue_paused = paused
            if job.done:
                if state in ("failed", "partial", "review") and job.process is None:
                    job.message(row["message"], failed=True)
                job.update_details()
            if job.process is not None:
                if paused:
                    job.message("Suspension en cours…")
                continue
            if state in {"running", "queued"}:
                if job.done:
                    self.import_rows.remove(job)
                    self.imports.remove(job)
                    job = self.add_import(row)
                job.store_state = state
                job.queue_paused = paused
                job.message("En pause · reprise en attente" if paused and state == "queued" else row["message"])
                if state == "running" or can_start and active < capacity:
                    with lease(self.queue.directory / f"{identity}.lock", wait=False) as fd:
                        if fd is None:
                            # An import started by the previous instance still
                            # owns the lot. Its worker commits the result itself.
                            if state == "running":
                                job.message("Suspension en cours…" if paused else "Import actif · session précédente")
                            continue
                        if state == "running":
                            message = ("En pause · reprise en attente" if paused else "Import interrompu · reprise en attente")
                            self.queue.transition(identity, "running", "queued", message)
                            job.store_state = "queued"
                            active -= 1
                            job.message(message)
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
        visible = {row["id"] for row in rows if row["visible"]}
        for job in list(self.imports):
            if job.identity not in visible and job.process is None:
                self.import_rows.remove(job)
                self.imports.remove(job)
                self.by_id.pop(job.identity)
        self.update_queue_summary(capacity, inbox, limitation)

    def update_queue_summary(self, capacity, inbox, limitation):
        pause_state = self.queue.pause_state()
        _, history_revision = self.queue.history_policy()
        if self.refresh_history and history_revision != self.imports_snapshot[1].get("history_revision"):
            self.refresh_history()
        self.pause.set_label({"running": "Mettre la file en pause", "suspending": "Suspension en cours…",
                              "paused": "Reprendre la file"}[pause_state])
        self.pause.set_sensitive(pause_state != "suspending")
        if pause_state == "paused" and self.imports_snapshot[1].get("pause_state") != "paused":
            self.on_update(None, "queue", {"message": "File suspendue · état sauvegardé · créneaux libérés"})
        counts = self.queue.count(visible=True)
        albums, tracks, settled, planned = self.queue.totals()
        active, waiting = counts.get("running", 0), counts.get("queued", 0)
        errors = counts.get("failed", 0) + counts.get("partial", 0) + counts.get("review", 0)
        downloads = slot_count(inbox / ".kara-transferts", DOWNLOADS)
        processing = slot_count(inbox / ".kara-traitements", PROCESSING)
        running = [job for job in self.imports if job.process is not None]
        now = monotonic()
        rates = [rate for job in running for stamp, rate in job.transfer_rates.values() if now - stamp <= 5.]
        progressed = sum(job.album_fraction for job in running)
        progress = min(1., (settled + progressed) / planned) if planned else 0.
        owner_rows = [job.snapshot() for job in reversed(self.imports)]
        self.imports_snapshot = (owner_rows, dict(active=active, waiting=waiting, albums=albums, tracks=tracks,
                                                  errors=errors, downloads=downloads, processing=processing,
                                                  audio_bytes_per_second=sum(rates) if rates else None,
                                                  progress=progress, paused=pause_state != "running",
                                                  pause_state=pause_state, batches=capacity,
                                                  history_revision=history_revision,
                                                  limitation=CATEGORIES["space"] if limitation else "",
                                                  max_batches=MAX_BATCHES))
        summary = (f"{active} lot(s) actif(s) · {waiting} en attente · {albums} album(s) audio complet(s) · "
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
                identity, "partial", "queued", "Relance en attente…") or self.queue.transition(
                identity, "review", "queued", "Relance manuelle en attente…"):
            self.launch_next(force=True)
            return True
        return False

    def remove_job(self, identity):
        try:
            self.queue.delete_history((identity,))
        except (OSError, sqlite3.Error) as error:
            self.message(f"Retrait impossible · {error}", failed=True)
            return
        self.launch_next(force=True)

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
    def __init__(self, jobs, on_finished, *, identity, option=None, retry=None, journal=None, remove=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.identity = identity
        self.jobs = jobs
        self.option = option
        self.keys = {import_key(url) for _, url in jobs} if option is None else set()
        self.on_finished = on_finished
        self.retry = retry
        self.remove = remove
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
        self.queue_paused = False
        self.measured = False
        self.result = None
        self.outcomes = {}
        self.transfer_rates = {}
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
        self.remove_button = Gtk.Button(label="Retirer", visible=False,
                                        tooltip_text="Retirer ce lot de la liste et de l’historique")
        if remove:
            self.remove_button.connect("clicked", lambda _: remove(identity))
        header.append(self.remove_button)
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
        if self.queue_paused and state in {"queued", "running"}:
            state, status = (("suspended", "En pause · reprise en attente") if state == "queued" else
                             ("suspending", "Suspension en cours…"))
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
        if event == "transfer":
            rate, position = payload.get("bytes_per_second"), payload.get("position")
            if type(rate) in (int, float) and 0 <= rate <= 2 * 1024**3 and type(position) is int:
                self.transfer_rates[payload.get("url"), position] = monotonic(), rate
            return
        if event == "journal":
            self.journal = payload.get("path")
        elif event == "album" and isinstance(payload.get("url"), str):
            self.outcomes[payload["url"]] = payload
            self.transfer_rates = {key: rate for key, rate in self.transfer_rates.items() if key[0] != payload["url"]}
            self.album_fraction = payload.get("album_fraction", 0.)
            if fraction is not None:
                self.fraction = max(self.fraction, fraction)
                self.progress.set_fraction(self.fraction)
            self.update_counts()
        elif event in {"success", "failure", "skip", "cancelled", "suspended"}:
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
        if completed and event == "suspended":
            self.process = None
            if self.timer is not None:
                GLib.source_remove(self.timer)
                self.timer = None
            self.result = None
            self.store_state = "queued"
            self.stopping = self.stop_ready = False
            self.stop.set_sensitive(False)
            self.transfer_rates.clear()
            self.album_fraction = 0.
            self.message(text)
            self.on_update(self, "suspended", {"message": text})
            return
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
        self.remove_button.set_visible(bool(self.done and self.remove))

    def retry_failed(self, _button):
        if self.failed_jobs and self.retry(self.identity):
            self.retry_button.set_visible(False)
