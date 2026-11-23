"""Sélection de chansons pour une préparation locale indépendante des joueurs."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import GLib, Gtk
import sqlite3

from _kara_party import search_key
from _kara_library import all_songs
from _kara_stems import cached_stems


class PreparationWindow(Gtk.Window):
    def __init__(self, controller, database, library):
        super().__init__(title="Préparer des chansons pour le karaoké", transient_for=controller,
                         destroy_with_parent=True, hide_on_close=True,
                         default_width=690, default_height=620)
        self.controller = controller
        self.database = database
        self.library = library
        self.selected = {}
        self.songs = []
        self.artist_names = ["Tous les artistes"]
        self.album_names = ["Tous les albums"]
        self.loading = False
        self.busy = False
        self.list_limit = 100

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        self.set_child(root)
        heading = Gtk.Label(label="Préparer des chansons à l'avance", xalign=0,
                            css_classes=["track-title"])
        root.append(heading)
        intro = Gtk.Label(label="Sélectionne des morceaux ou des albums. Avec LRC : calage Turbo, analyse mot par mot "
                          "et séparation Demucs. Sans LRC : séparation IA des voix uniquement.",
                          xalign=0, wrap=True, css_classes=["lyrics-info"])
        root.append(intro)

        self.search = Gtk.SearchEntry(placeholder_text="Rechercher un artiste, titre ou album")
        self.search.connect("search-changed", self.render)
        root.append(self.search)
        filters = Gtk.Box(spacing=8)
        self.artist_filter = Gtk.DropDown.new_from_strings(self.artist_names)
        self.artist_filter.set_hexpand(True)
        self.artist_filter.connect("notify::selected", self.artist_changed)
        filters.append(self.artist_filter)
        self.album_filter = Gtk.DropDown.new_from_strings(self.album_names)
        self.album_filter.set_hexpand(True)
        self.album_filter.connect("notify::selected", self.render)
        filters.append(self.album_filter)
        root.append(filters)
        self.album_select = Gtk.Button(label="Sélectionner tout l’album",
                                       tooltip_text="Sélectionner tous les titres de l’album, avec ou sans paroles LRC")
        self.album_select.connect("clicked", self.select_album)
        root.append(self.album_select)
        self.listbox = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(vexpand=True, min_content_height=240,
                                    hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.listbox)
        root.append(scroll)

        self.results_info = Gtk.Label(label="Chargement du catalogue…", xalign=0,
                                      wrap=True, css_classes=["lyrics-info"])
        root.append(self.results_info)
        buttons = Gtk.Box(spacing=8)
        self.add_party = Gtk.Button(label="Ajouter les morceaux des joueurs",
                                    tooltip_text="Ajouter les chansons choisies dans la fenêtre Joueurs")
        self.add_party.connect("clicked", self.add_player_songs)
        buttons.append(self.add_party)
        self.select_filtered = Gtk.Button(label="Ajouter les résultats affichés",
                                          tooltip_text="Ajouter jusqu'à 100 chansons de la recherche actuelle")
        self.select_filtered.connect("clicked", self.select_results)
        buttons.append(self.select_filtered)
        self.clear = Gtk.Button(label="Vider la sélection")
        self.clear.connect("clicked", self.clear_selection)
        buttons.append(self.clear)
        root.append(buttons)
        actions = Gtk.Box(spacing=8)
        self.prepare_button = Gtk.Button(label="Préparer la sélection", css_classes=["suggested-action"])
        self.prepare_button.connect("clicked", self.prepare)
        actions.append(self.prepare_button)
        self.cancel_button = Gtk.Button(label="Annuler la préparation")
        self.cancel_button.connect("clicked", lambda *_: self.controller.cancel_preparation())
        actions.append(self.cancel_button)
        root.append(actions)

        self.selected_info = Gtk.Label(label="Aucun morceau sélectionné", xalign=0,
                                       css_classes=["lyrics-info"])
        root.append(self.selected_info)
        self.progress = Gtk.ProgressBar(show_text=True, visible=False)
        root.append(self.progress)
        self.message = Gtk.Label(label="", wrap=True, xalign=0, css_classes=["lyrics-info"])
        root.append(self.message)
        self.update_controls()
        self.load_catalogue()

    def load_catalogue(self):
        if self.loading:
            return
        self.loading = True
        self.results_info.set_text("Chargement du catalogue Strawberry…")
        task = self.controller.tag_reader.submit(all_songs, self.database, self.library)
        task.add_done_callback(lambda done: GLib.idle_add(self.catalogue_ready, done))

    def catalogue_ready(self, done):
        self.loading = False
        if self.controller.closed:
            return GLib.SOURCE_REMOVE
        try:
            self.songs = list(done.result())
        except (OSError, ValueError, sqlite3.Error) as error:
            self.results_info.set_text(f"Collection Strawberry inaccessible : {error}")
        else:
            self.artist_names = ["Tous les artistes", *sorted({song.artist for song, _ in self.songs}, key=search_key)]
            self.artist_filter.set_model(Gtk.StringList.new(self.artist_names))
            self.artist_filter.set_selected(0)
            self.artist_changed()
        return GLib.SOURCE_REMOVE

    def artist_changed(self, *_args):
        artist = self.artist_names[self.artist_filter.get_selected()]
        albums = {song.album for song, _ in self.songs
                  if song.album and (artist == "Tous les artistes" or song.artist == artist)}
        self.album_names = ["Tous les albums", *sorted(albums, key=search_key)]
        self.album_filter.set_model(Gtk.StringList.new(self.album_names))
        self.album_filter.set_selected(0)
        self.render()

    def filtered_songs(self):
        terms = search_key(self.search.get_text()).split()
        artist = self.artist_names[self.artist_filter.get_selected()]
        album = self.album_names[self.album_filter.get_selected()]
        return [(song, key) for song, key in self.songs
                if (artist == "Tous les artistes" or song.artist == artist)
                and (album == "Tous les albums" or song.album == album)
                and all(term in key for term in terms)]

    def render(self, *_args):
        matches = self.filtered_songs()
        while child := self.listbox.get_first_child():
            self.listbox.remove(child)
        for song, _ in matches[:self.list_limit]:
            label = " · ".join(s for s in (song.title, song.artist, song.album) if s)
            has_lyrics = song.path.with_suffix(".lrc").is_file()
            if not has_lyrics:
                label += " · Sans LRC : séparation IA seule"
            if cached_stems(song.path):
                label += " · IA prête"
            row = Gtk.CheckButton(label=label, active=song.path in self.selected,
                                  sensitive=not self.busy,
                                  margin_top=6, margin_bottom=6)
            row.set_tooltip_text(label)
            row.connect("toggled", self.toggle_song, song)
            self.listbox.append(row)
        unavailable = sum(not song.path.with_suffix(".lrc").is_file() for song, _ in matches)
        if len(matches) > self.list_limit:
            self.results_info.set_text(f"{len(matches)} titres · {unavailable} sans LRC (IA seule) · {self.list_limit} affichés")
        else:
            self.results_info.set_text(f"{len(matches)} titres · {unavailable} sans LRC (IA seule)")
        self.update_controls()

    def toggle_song(self, control, song):
        if control.get_active():
            self.selected[song.path] = song
        else:
            self.selected.pop(song.path, None)
        self.update_controls()

    def select_results(self, _button):
        matches = self.filtered_songs()
        if (not self.search.get_text().strip()
                and self.artist_filter.get_selected() == 0
                and self.album_filter.get_selected() == 0):
            self.message.set_text("Choisis d’abord un artiste, un album ou fais une recherche.")
            return
        for song, _ in matches[:self.list_limit]:
            self.selected[song.path] = song
        self.render()

    def select_album(self, _button):
        if self.busy or self.loading or not self.songs:
            return
        if self.artist_filter.get_selected() == 0 or self.album_filter.get_selected() == 0:
            self.message.set_text("Choisis un artiste puis un album avant de sélectionner l’album entier.")
            return
        artist = self.artist_names[self.artist_filter.get_selected()]
        album = self.album_names[self.album_filter.get_selected()]
        matches = [song for song, _ in self.songs if song.artist == artist and song.album == album]
        before = len(self.selected)
        with_lyrics = sum(song.path.with_suffix(".lrc").is_file() for song in matches)
        for song in matches:
            self.selected[song.path] = song
        without_lyrics = len(matches) - with_lyrics
        self.message.set_text(
            f"{album} · {len(matches)}/{len(matches)} titres sélectionnés"
            + f" · {with_lyrics} avec LRC"
            + (f" · {without_lyrics} sans LRC (IA seule)" if without_lyrics else "")
            + f" · {len(self.selected)-before} nouveau(x) titre(s)")
        self.render()

    def add_player_songs(self, _button):
        for player in self.controller.party.players:
            if player.song is not None:
                self.selected[player.song.path] = player.song
        self.render()

    def clear_selection(self, _button):
        self.selected.clear()
        self.render()

    def update_controls(self):
        count = len(self.selected)
        with_lyrics = sum(song.path.with_suffix(".lrc").is_file() for song in self.selected.values())
        self.selected_info.set_text(
            f"{count} titre(s) · {with_lyrics} avec LRC · {count - with_lyrics} sans LRC (IA seule)"
            if count else "Aucun morceau sélectionné")
        self.prepare_button.set_sensitive(count > 0 and not self.busy and not self.controller.validating)
        self.clear.set_sensitive(count > 0 and not self.busy)
        self.add_party.set_sensitive(not self.busy)
        self.album_select.set_sensitive(not self.busy and not self.loading
                                        and self.artist_filter.get_selected() != 0
                                        and self.album_filter.get_selected() != 0)
        self.artist_filter.set_sensitive(not self.busy and not self.loading)
        self.album_filter.set_sensitive(not self.busy and not self.loading)
        self.select_filtered.set_sensitive(not self.busy and not self.loading
                                           and (bool(search_key(self.search.get_text()).strip())
                                                or self.artist_filter.get_selected() != 0
                                                or self.album_filter.get_selected() != 0))
        self.cancel_button.set_sensitive(self.busy and not self.controller.preload_cancelled)
        self.search.set_sensitive(not self.busy)

    def set_busy(self, busy):
        self.busy = bool(busy)
        self.update_controls()
        # Rebuild for the disabled state of each check button.
        self.render()
        self.progress.set_visible(self.busy or self.progress.get_fraction() > 0)

    def set_progress(self, text, fraction):
        self.message.set_text(text)
        self.progress.set_visible(True)
        self.progress.set_fraction(max(0., min(1., fraction)))
        self.progress.set_text(f"{round(self.progress.get_fraction() * 100)} %")

    def prepare(self, _button):
        if not self.selected or self.busy:
            return
        self.controller.preload_songs(tuple(self.selected.values()))
