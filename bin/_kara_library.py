"""Navigateur et liste de lecture Strawberry depuis Paroles (MPRIS uniquement).

Ne lit jamais de fichier directement : Strawberry reste l'unique lecteur audio.
Lecture, file d'attente et navigation disponibles sans activer le jeu.
"""
from dataclasses import dataclass
from pathlib import Path
from heapq import merge
import sqlite3
import time
from unicodedata import normalize
from urllib.parse import urlsplit, unquote

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib, Gtk, Pango, Gdk

PLAYER = 'org.mpris.MediaPlayer2.strawberry'
OBJECT = '/org/mpris/MediaPlayer2'
PLAYBACK = 'org.mpris.MediaPlayer2.Player'
TRACKLIST = 'org.mpris.MediaPlayer2.TrackList'
PROPERTIES = 'org.freedesktop.DBus.Properties'
NO_TRACK = '/org/mpris/MediaPlayer2/TrackList/NoTrack'
PAGE_SIZE = 60


def search_key(text):
    return "".join(c for c in normalize("NFKD", text.casefold()) if not (0x300 <= ord(c) <= 0x36f))


@dataclass(frozen=True)
class Song:
    path: Path
    title: str
    artist: str
    album: str
    duration: float | None = None


def all_songs(database, library):
    """Lecture du catalogue local, y compris les titres sans paroles LRC."""
    library = Path(library).resolve()
    unique, songs = set(), []
    with sqlite3.connect(Path(database).absolute().as_uri() + '?mode=ro', uri=True, timeout=3) as connection:
        rows = connection.execute('SELECT url, title, artist, album FROM songs WHERE unavailable = 0 ORDER BY artist COLLATE NOCASE, album COLLATE NOCASE, title COLLATE NOCASE')
        for uri, title, artist, album in rows:
            address = urlsplit(uri or '')
            if address.scheme != 'file' or address.netloc not in ('','localhost'):
                continue
            path = Path(unquote(address.path)).resolve()
            if path in unique or not path.is_relative_to(library) or not path.is_file():
                continue
            unique.add(path)
            song = Song(path, title or path.stem, artist or 'Artiste inconnu', album or '')
            songs.append((song, search_key(f'{song.title} {song.artist} {song.album}')))
    return songs


def library_snapshot(database, library):
    rows = all_songs(database, library)
    rows.sort(key=catalogue_sort)
    return rows, {song.path for song, _ in rows if song.path.with_suffix('.lrc').is_file()}


def catalogue_sort(row):
    song = row[0]
    return song.artist.casefold(), song.album.casefold(), song.title.casefold(), str(song.path)


class MusicLibraryWindow(Gtk.Window):
    def __init__(self, controller, database, library):
        super().__init__(title='Bibliothèque et liste de lecture — Strawberry', transient_for=controller,
                         destroy_with_parent=True, hide_on_close=True,
                         default_width=1050, default_height=690)
        self.controller = controller
        self.database = database
        self.library = library
        self.songs = []
        self.lrc_paths = set()
        self.publications = {}
        self.publication_lrc = set()
        self.rendered_page = None
        self.updating_filters = False
        self.filtered = []
        self.page = 0
        self.artist_names = ['Tous les artistes']
        self.album_names = ['Tous les albums']
        self.loading = False
        self.tracks = []
        self.playlist_items = []
        self.metadata_cursor = 0
        self.current_id = None
        self.fetching_queue = False
        self.refresh_deadline = 0
        self.action_in_progress = False
        self.queue_rebuilding = False
        self.selected_ids = set()
        self.remove_pending = []
        self.remove_errors = []
        self.removed_count = 0
        self.removing_selection = False

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8,
                       margin_start=14, margin_end=14, margin_top=12, margin_bottom=12)
        self.set_child(root)
        info = Gtk.Label(label='La lecture reste dans Strawberry. Clique sur ▶ pour lire, ou sur + pour ajouter à la liste.',
                         xalign=0, wrap=True, css_classes=['lyrics-info'])
        root.append(info)
        split = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True)
        split.set_wide_handle(True)
        split.set_position(585)
        root.append(split)

        left = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_end=8)
        split.set_start_child(left)
        left.append(Gtk.Label(label='Bibliothèque musicale', xalign=0, css_classes=['track-title']))
        self.search = Gtk.SearchEntry(placeholder_text='Titre, artiste ou album')
        self.search.connect('search-changed', self.update_results)
        left.append(self.search)
        selectors = Gtk.Box(spacing=8)
        self.artists = Gtk.DropDown.new_from_strings(self.artist_names)
        self.artists.set_hexpand(True)
        self.artists.connect('notify::selected', self.artist_changed)
        selectors.append(self.artists)
        self.albums = Gtk.DropDown.new_from_strings(self.album_names)
        self.albums.set_hexpand(True)
        self.albums.connect('notify::selected', self.update_results)
        selectors.append(self.albums)
        left.append(selectors)
        self.catalogue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.NONE)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(self.catalogue_list)
        left.append(scroll)
        paging = Gtk.Box(spacing=8)
        self.previous_page = Gtk.Button(label='◀ Page précédente')
        self.previous_page.connect('clicked', self.page_change, -1)
        paging.append(self.previous_page)
        self.library_status = Gtk.Label(label='Chargement de Strawberry…', hexpand=True,
                                        xalign=.5, css_classes=['lyrics-info'])
        paging.append(self.library_status)
        self.next_page = Gtk.Button(label='Page suivante ▶')
        self.next_page.connect('clicked', self.page_change, 1)
        paging.append(self.next_page)
        left.append(paging)

        right = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_start=8)
        split.set_end_child(right)
        heading = Gtk.Box(spacing=8)
        heading.append(Gtk.Label(label='Liste de lecture Strawberry', xalign=0, hexpand=True,
                                 css_classes=['track-title']))
        reload_button = Gtk.Button(icon_name='view-refresh-symbolic', tooltip_text='Actualiser la liste de lecture')
        reload_button.connect('clicked', lambda *_: self.refresh_queue(force=True))
        heading.append(reload_button)
        right.append(heading)
        self.queue_status = Gtk.Label(label='Lecture de la liste de Strawberry…', xalign=0,
                                      css_classes=['lyrics-info'])
        right.append(self.queue_status)
        self.queue_list = Gtk.ListBox(selection_mode=Gtk.SelectionMode.MULTIPLE)
        self.queue_list.add_css_class('playlist-tracks')
        self.queue_list.set_activate_on_single_click(False)
        self.queue_list.connect('selected-rows-changed', self.queue_selection_changed)
        self.queue_list.connect('row-activated', self.queue_row_activated)
        self.queue_keys = Gtk.EventControllerKey()
        self.queue_keys.connect('key-pressed', self.queue_key_pressed)
        self.queue_list.add_controller(self.queue_keys)
        queue_scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        queue_scroll.set_child(self.queue_list)
        right.append(queue_scroll)
        selection_bar = Gtk.Box(spacing=8)
        self.selection_status = Gtk.Label(label='Aucun titre sélectionné', xalign=0,
                                          hexpand=True, css_classes=['lyrics-info'])
        selection_bar.append(self.selection_status)
        self.remove_selected_button = Gtk.Button(label='Retirer la sélection', sensitive=False,
                                                  tooltip_text='Retirer de Strawberry, sans effacer les fichiers musicaux')
        self.remove_selected_button.connect('clicked', self.remove_selected)
        selection_bar.append(self.remove_selected_button)
        right.append(selection_bar)
        self.shortcut_tip = Gtk.Label(label='Clic · Ctrl+clic · Maj+clic · Ctrl+A · Suppr · Double-clic pour écouter',
                                      xalign=0, wrap=True, css_classes=['lyrics-info'])
        right.append(self.shortcut_tip)
        # Actual controls are in Paroles main window; repeat for convenience.
        actions = Gtk.Box(spacing=8, halign=Gtk.Align.CENTER)
        for name, action, tooltip in [('⏮', 'Previous', 'Morceau précédent'),
                                      ('⏯', 'PlayPause', 'Lecture / pause'),
                                      ('⏭', 'Next', 'Morceau suivant')]:
            button = Gtk.Button(label=name, tooltip_text=tooltip)
            button.connect('clicked', lambda _b, method=action: self.controller.player_command(method))
            actions.append(button)
        right.append(actions)
        self.feedback = Gtk.Label(label='', xalign=0, wrap=True,
                                  css_classes=['lyrics-info'])
        root.append(self.feedback)
        self.load_library()
        self.refresh_queue(force=True)

    def load_library(self):
        if self.loading:
            return
        self.loading = True
        task = self.controller.tag_reader.submit(library_snapshot, self.database, self.library)
        task.add_done_callback(lambda result: GLib.idle_add(self.library_loaded, result))

    def library_loaded(self, future):
        self.loading = False
        if self.controller.closed:
            return GLib.SOURCE_REMOVE
        try:
            self.songs, self.lrc_paths = future.result()
        except (OSError, ValueError, sqlite3.Error) as error:
            self.library_status.set_text(f'Catalogue indisponible : {error}')
            return GLib.SOURCE_REMOVE
        self.apply_publications()
        return GLib.SOURCE_REMOVE

    def publish_rows(self, rows, lyrics, removed=()):
        for path in removed:
            self.publications[path] = None
            self.publication_lrc.discard(path)
        for row in rows:
            self.publications[row[0].path] = row
            self.publication_lrc.discard(row[0].path)
        self.publication_lrc.update(lyrics)
        if not self.loading and self.get_visible():
            self.apply_publications()

    @staticmethod
    def selection(widget, values):
        position = widget.get_selected()
        return values[position] if position < len(values) else values[0]

    def apply_publications(self):
        # Keep the active filter and page when a small path delta arrives.
        # The old catalogue and decorations came from the background load.
        if self.loading:
            return
        if self.publications:
            remaining = (row for row in self.songs if row[0].path not in self.publications)
            added = sorted((row for row in self.publications.values() if row is not None), key=catalogue_sort)
            self.songs = list(merge(remaining, added, key=catalogue_sort))
            self.lrc_paths.difference_update(self.publications)
            self.lrc_paths.update(self.publication_lrc)
            self.publications.clear()
            self.publication_lrc.clear()
        artists = ['Tous les artistes', *sorted({song.artist for song, _ in self.songs}, key=search_key)]
        if artists != self.artist_names:
            selected = self.selection(self.artists, self.artist_names)
            self.artist_names = artists
            self.updating_filters = True
            self.artists.set_model(Gtk.StringList.new(artists))
            self.artists.set_selected(artists.index(selected) if selected in artists else 0)
            self.updating_filters = False
        self.artist_changed(preserve=True)

    def artist_changed(self, *_args, preserve=False):
        if self.updating_filters:
            return
        artist = self.selection(self.artists, self.artist_names)
        albums = {song.album for song, _ in self.songs
                  if song.album and (artist == 'Tous les artistes' or song.artist == artist)}
        values = ['Tous les albums', *sorted(albums, key=search_key)]
        selected = self.selection(self.albums, self.album_names) if preserve else values[0]
        self.updating_filters = True
        if values != self.album_names:
            self.album_names = values
            self.albums.set_model(Gtk.StringList.new(values))
        self.albums.set_selected(values.index(selected) if selected in values else 0)
        self.updating_filters = False
        self.update_results(preserve=preserve)

    def update_results(self, *_args, preserve=False):
        if self.updating_filters:
            return
        query = search_key(self.search.get_text()).split()
        artist = self.selection(self.artists, self.artist_names)
        album = self.selection(self.albums, self.album_names)
        self.filtered = [song for song, key in self.songs
                         if (artist == 'Tous les artistes' or artist == song.artist)
                         and (album == 'Tous les albums' or album == song.album)
                         and all(term in key for term in query)]
        self.page = min(self.page, max(0, (len(self.filtered)-1)//PAGE_SIZE)) if preserve else 0
        self.render_library()

    def page_change(self, _button, direction):
        new = self.page + direction
        if 0 <= new < max(1,(len(self.filtered) + PAGE_SIZE - 1)//PAGE_SIZE):
            self.page = new
            self.render_library()

    @staticmethod
    def _empty_list(view):
        while child := view.get_first_child():
            view.remove(child)

    def render_library(self):
        start = self.page * PAGE_SIZE
        shown = self.filtered[start:start+PAGE_SIZE]
        total = len(self.filtered)
        self.library_status.set_text(f'{start+1 if total else 0}–{min(start+len(shown),total)} / {total} titres')
        self.previous_page.set_sensitive(self.page>0)
        self.next_page.set_sensitive((self.page+1)*PAGE_SIZE<total)
        signature = [(song, song.path in self.lrc_paths) for song in shown]
        if self.rendered_page == signature:
            return
        self.rendered_page = signature
        self._empty_list(self.catalogue_list)
        for song in shown:
            row = Gtk.Box(spacing=7, margin_top=5, margin_bottom=5,
                          margin_start=5, margin_end=5)
            detail = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
            detail.append(Gtk.Label(label=song.title, xalign=0,
                                    ellipsize=Pango.EllipsizeMode.END,
                                    tooltip_text=song.title))
            subtitle = ' · '.join((song.artist, song.album)).strip(' ·')
            lyric_suffix = ' · ♪ paroles' if song.path in self.lrc_paths else ''
            detail.append(Gtk.Label(label=subtitle+lyric_suffix, xalign=0,
                                    ellipsize=Pango.EllipsizeMode.END,
                                    tooltip_text=subtitle,
                                    css_classes=['lyrics-info']))
            row.append(detail)
            play = Gtk.Button(icon_name='media-playback-start-symbolic',
                              tooltip_text='Ajouter et lire ce titre')
            play.connect('clicked', self.add_song, song, True)
            row.append(play)
            append = Gtk.Button(label='+', tooltip_text='Ajouter à la fin de la liste')
            append.connect('clicked', self.add_song, song, False)
            row.append(append)
            self.catalogue_list.append(row)

    def refresh_queue(self, force=False):
        if self.controller.closed or self.fetching_queue:
            return
        if not force and (not self.get_visible() or time.monotonic()<self.refresh_deadline):
            return
        self.fetching_queue = True
        self.refresh_deadline = time.monotonic()+3.0
        self.controller.bus.call(PLAYER, OBJECT, PROPERTIES, 'Get',
                                 GLib.Variant('(ss)', (TRACKLIST,'Tracks')),
                                 GLib.VariantType.new('(v)'),
                                 Gio.DBusCallFlags.NO_AUTO_START, 1600, None,
                                 self.tracks_reply)

    def tracks_reply(self, bus, result):
        if self.controller.closed:
            self.fetching_queue = False
            return
        try:
            tracks = list(bus.call_finish(result).unpack()[0])
        except GLib.Error as error:
            self.fetching_queue = False
            self.queue_status.set_text('Liste de lecture inaccessible')
            self.feedback.set_text(str(error))
            return
        self.tracks = tracks
        self.playlist_items = []
        self.metadata_cursor = 0
        if not tracks:
            self.fetching_queue = False
            self.render_queue()
            return
        self.request_metadata_batch()

    def request_metadata_batch(self):
        batch = self.tracks[self.metadata_cursor:self.metadata_cursor+250]
        if not batch:
            self.fetching_queue = False
            self.render_queue()
            return
        self.controller.bus.call(PLAYER, OBJECT, TRACKLIST, 'GetTracksMetadata',
                                 GLib.Variant('(ao)', (batch,)),
                                 GLib.VariantType.new('(aa{sv})'),
                                 Gio.DBusCallFlags.NO_AUTO_START, 2800, None,
                                 self.queue_metadata_reply)

    def queue_metadata_reply(self, bus, result):
        if self.controller.closed:
            self.fetching_queue = False
            return
        try:
            metadata, = bus.call_finish(result).unpack()
        except GLib.Error as error:
            self.fetching_queue = False
            self.queue_status.set_text('Métadonnées de playlist indisponibles')
            self.feedback.set_text(str(error))
            return
        self.playlist_items.extend(metadata)
        self.metadata_cursor += len(metadata)
        if len(metadata) < min(250, len(self.tracks)-self.metadata_cursor+len(metadata)):
            self.fetching_queue = False
            self.queue_status.set_text('Liste de lecture incomplète')
            self.render_queue()
            return
        self.request_metadata_batch()

    def selected_track_ids(self):
        """La sélection est indexée sur les identifiants MPRIS, pas les chemins.

        Deux occurrences de la même chanson dans une liste restent indépendantes.
        """
        selected = []
        for row in self.queue_list.get_selected_rows():
            index = row.get_index()
            if 0 <= index < len(self.playlist_items):
                track = self.playlist_items[index].get('mpris:trackid')
                if isinstance(track, str) and track:
                    selected.append(track)
        return selected

    def queue_selection_changed(self, _listbox):
        if self.queue_rebuilding:
            return
        self.selected_ids = set(self.selected_track_ids())
        self.update_selection_controls()

    def update_selection_controls(self):
        count = len(self.selected_ids)
        total = len(self.tracks)
        self.selection_status.set_text(
            f'{count} titre(s) sélectionné(s) sur {total}' if count
            else f'Aucun titre sélectionné · {total} dans la liste')
        self.remove_selected_button.set_sensitive(count > 0 and not self.removing_selection
                                                   and self.controller.party.active is None)

    def queue_key_pressed(self, _controller, keyval, _keycode, state):
        ctrl = bool(state & Gdk.ModifierType.CONTROL_MASK)
        if ctrl and keyval in (Gdk.KEY_a, Gdk.KEY_A):
            self.queue_list.select_all()
            return True
        if keyval in (Gdk.KEY_Delete, Gdk.KEY_KP_Delete) and not ctrl:
            self.remove_selected(None)
            return True
        return False

    def queue_row_activated(self, _listbox, row):
        """GTK n'active les lignes qu'au double-clic (ou via Entrée)."""
        index = row.get_index()
        if 0 <= index < len(self.playlist_items):
            track = self.playlist_items[index].get('mpris:trackid')
            if isinstance(track, str):
                self.go_to(None, track)

    def render_queue(self):
        if not self.get_visible():
            return
        # La liste est actualisée à chaque nouveau morceau, mais les sélections
        # Ctrl+clic et Ctrl+A doivent rester sélectionnées.
        preserve = set(self.selected_ids)
        self.queue_rebuilding = True
        try:
            self._empty_list(self.queue_list)
            self.queue_status.set_text(f'{len(self.tracks)} titres dans la liste Strawberry')
            if not self.playlist_items:
                self.selected_ids.clear()
                self.queue_list.append(Gtk.Label(label='Liste de lecture vide', xalign=0,
                                                 css_classes=['lyrics-info']))
                return
            still_selected = set()
            for i, data in enumerate(self.playlist_items):
                track = data.get('mpris:trackid')
                if not track or not isinstance(track, str):
                    continue
                title = str(data.get('xesam:title') or 'Titre inconnu')
                artists = data.get('xesam:artist') or []
                artist = ' · '.join(artists) if isinstance(artists, (tuple,list)) else str(artists)
                playing = track == self.current_id
                label = Gtk.Label(label=f'{i+1}. {title} — {artist}', xalign=0, hexpand=True,
                                  ellipsize=Pango.EllipsizeMode.END,
                                  margin_start=8, margin_end=8, margin_top=7, margin_bottom=7)
                label.set_tooltip_text(f'{title} — {artist}')
                row = Gtk.ListBoxRow(activatable=True, selectable=True)
                if playing:
                    row.add_css_class('party-current')
                row.set_child(label)
                self.queue_list.append(row)
                if track in preserve:
                    self.queue_list.select_row(row)
                    still_selected.add(track)
            self.selected_ids = still_selected
            if len(self.tracks)>len(self.playlist_items):
                self.queue_status.set_text(f'{len(self.playlist_items)} / {len(self.tracks)} titres disponibles')
        finally:
            self.queue_rebuilding = False
            self.update_selection_controls()

    def update_current(self, track_id, playback):
        if track_id != self.current_id:
            self.current_id = track_id
            if self.get_visible() and self.playlist_items:
                self.render_queue()
        self.refresh_queue()

    def _call(self, interface, method, params, callback=None):
        self.controller.bus.call(PLAYER, OBJECT, interface, method, params, None,
                                 Gio.DBusCallFlags.NO_AUTO_START, 3500, None, callback)

    def add_song(self, _button, song, play):
        if self.action_in_progress or self.controller.closed:
            return
        if not song.path.is_file():
            self.feedback.set_text('Fichier audio introuvable')
            return
        if self.controller.party.active is not None:
            self.feedback.set_text('Termine le tour en cours avant de changer de chanson.')
            return
        self.action_in_progress = True
        uri = song.path.as_uri()
        after = self.tracks[-1] if self.tracks else NO_TRACK
        self.feedback.set_text(f'{"Lecture" if play else "Ajout"} · {song.title}')
        params = GLib.Variant('(sob)', (uri,after,bool(play)))
        self._call(TRACKLIST, 'AddTrack', params,
                   lambda bus,result: self.add_done(bus,result,play,song))

    def add_done(self, bus, result, play, song):
        self.action_in_progress = False
        if self.controller.closed:
            return
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self.feedback.set_text(f'Impossible d’ajouter {song.title} : {error.message}')
        else:
            self.feedback.set_text(f'{song.title} ajouté à Strawberry')
            if play:
                self.controller.player_command('Play')
            GLib.timeout_add(240, self._refresh_once)

    def _refresh_once(self):
        self.refresh_queue(force=True)
        return GLib.SOURCE_REMOVE

    def go_to(self, _button, track):
        if self.controller.party.active is not None:
            self.feedback.set_text('Termine le tour en cours avant de changer de chanson.')
            return
        self._call(TRACKLIST, 'GoTo', GLib.Variant('(o)',(track,)),
                   lambda bus,result: self.go_to_done(bus,result))

    def go_to_done(self,bus,result):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self.feedback.set_text(f'Impossible de lire ce titre : {error.message}')
            return
        self.controller.player_command('Play')

    def remove_selected(self, _button):
        if self.removing_selection or self.controller.closed:
            return
        if self.controller.party.active is not None:
            self.feedback.set_text('Termine le tour en cours avant de modifier la liste.')
            return
        selected = set(self.selected_track_ids())
        if not selected:
            return
        # Inverser l'ordre facilite les modifications de playlists dynamiques.
        self.remove_pending = [track for track in reversed(self.tracks) if track in selected]
        if not self.remove_pending:
            return
        self.removing_selection = True
        self.action_in_progress = True
        self.removed_count = 0
        self.remove_errors = []
        self.update_selection_controls()
        self.feedback.set_text(f'Retrait de {len(self.remove_pending)} titre(s) de Strawberry…')
        self.remove_next()

    def remove_next(self):
        if self.controller.closed:
            self.removing_selection = False
            self.action_in_progress = False
            return
        if not self.remove_pending:
            self.removing_selection = False
            self.action_in_progress = False
            if self.remove_errors:
                self.feedback.set_text(f'{self.removed_count} titre(s) retiré(s) ; '
                                       f'{len(self.remove_errors)} échec(s).')
                self.selected_ids = set(self.remove_errors)
            else:
                self.feedback.set_text(f'{self.removed_count} titre(s) retiré(s) de la liste. '
                                       'Aucun fichier supprimé.')
                self.selected_ids.clear()
            self.update_selection_controls()
            GLib.timeout_add(220, self._refresh_once)
            return
        track = self.remove_pending.pop(0)
        self._call(TRACKLIST, 'RemoveTrack', GLib.Variant('(o)', (track,)),
                   lambda bus, result: self.remove_next_done(bus, result, track))

    def remove_next_done(self, bus, result, track):
        try:
            bus.call_finish(result)
        except GLib.Error:
            self.remove_errors.append(track)
        else:
            self.removed_count += 1
        self.remove_next()

    def remove_track(self, _button, track):
        if self.controller.party.active is not None:
            self.feedback.set_text('Termine le tour avant de modifier la liste.')
            return
        self._call(TRACKLIST,'RemoveTrack',GLib.Variant('(o)',(track,)),self.remove_done)

    def remove_done(self,bus,result):
        try:
            bus.call_finish(result)
        except GLib.Error as error:
            self.feedback.set_text(f'Impossible de retirer le titre : {error.message}')
            return
        GLib.timeout_add(240, self._refresh_once)
