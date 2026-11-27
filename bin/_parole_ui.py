"""Tableau de bord GTK4 de Parole et consultation ponctuelle des textes."""
from collections import OrderedDict
from datetime import datetime
from difflib import SequenceMatcher
import json
from math import ceil
from pathlib import Path
import sqlite3
import sys
from time import monotonic, time
from uuid import uuid4

from gi.repository import Gdk, Gio, GLib, GObject, Gtk, Pango

from _kara_import import show_details
from _kara_paths import BIN, STATE
from _parole_artist_groups import apply as group_artists, last_group, preview as preview_group, undo as undo_group
from _parole_index import key, document, encoded_key, sidecar
from _parole_import_enrichment import artist_results, queue_artist, ARTIST_OUTCOMES, artist_summary, track_outcome
from _parole_import_metrics import LOOKUP, STARTUP, SUCCEEDED, THROUGHPUT, TOTAL, snapshot as import_metrics
from _parole_offer import AUTOMATIC, STRATEGIES
from _parole_music_registry import set_alias
from _parole_sources import configure_limits, controls, lyrics_rows, move_lyrics_source, provider_limits
from _parole_resources import GIB
from _parole_work import save_correction

PAGE = 80
STATES = {"absent": "Absent", "prêt": "Prêt", "partiel": "Partiel", "externe": "Externe",
          "généré": "Généré", "local": "Local", "intégré": "Intégré", "à vérifier": "À vérifier",
          "obsolète": "Obsolète", "instrumental": "Instrumental", "inconnu": "Inconnu"}
CATEGORY_COLORS = {"DOWNLOAD": "download", "CODEX": "compute",
                   "SOURCE": "source", "SCAN": "source", "INDEX": "source", "SYNC": "source", "IDLE": "source", "SUCCESS": "success",
                   "WARNING": "warning", "ERROR": "error"}


def box(orientation=Gtk.Orientation.VERTICAL, spacing=8, **kwargs):
    return Gtk.Box(orientation=orientation, spacing=spacing, **kwargs)


def button(text, handler, icon=None, **kwargs):
    widget = Gtk.Button(**kwargs)
    if text and icon:
        area = box(Gtk.Orientation.HORIZONTAL, 6)
        area.append(Gtk.Image.new_from_icon_name(icon))
        area.append(Gtk.Label(label=text))
        widget.set_child(area)
    elif icon:
        widget.set_icon_name(icon)
    else:
        widget.set_label(text)
    widget.connect("clicked", handler)
    return widget


def scroller(widget, **kwargs):
    scroll = Gtk.ScrolledWindow(**kwargs)
    scroll.set_child(widget)
    return scroll


def display_size(size):
    return f"{size / GIB:.2f} Gio" if size >= GIB else f"{size / (1024**2):.1f} Mio"


def image_state(value, field):
    good = value not in ("absent", "inconnu", "obsolète", "insuffisant", "instrumental", "partiel", "à vérifier")
    if field == "lyrics" and good:
        return "Présent", "success"
    text = STATES.get(value, value)
    return text, "success" if good else "warning" if value in ("partiel", "à vérifier", "obsolète") else "muted"


class Row(GObject.Object):
    def __init__(self, data):
        super().__init__()
        self.data = data


def factory(render, create=None):
    result = Gtk.SignalListItemFactory()
    result.connect("setup", lambda _, item: item.set_child(create() if create else
                   Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)))
    result.connect("bind", lambda _, item: render(item.get_child(), item.get_item()))
    return result


def clear(area):
    while child := area.get_first_child():
        area.remove(child)


class Console:
    def __init__(self, maintenance, window):
        from _parole_maintenance import CATEGORIES

        self.maintenance = maintenance
        self.window = window
        self.seen = self.floor = -1
        self.lines = []
        self.import_state = ()
        self.import_stats_at = 0.
        self.import_summary = ""
        self.import_stats_pending = False
        self.index_metrics = Gtk.Label(xalign=0, wrap=True, css_classes=["muted"])
        self.panel = box(spacing=4, css_classes=["maintenance-console"])
        toolbar = box(Gtk.Orientation.HORIZONTAL, 5)
        self.reveal = Gtk.Revealer(reveal_child=True, transition_type=Gtk.RevealerTransitionType.SLIDE_UP)
        collapse = Gtk.ToggleButton(label="Maintenance", active=True)
        collapse.connect("toggled", lambda btn: self.reveal.set_reveal_child(btn.get_active()))
        toolbar.append(collapse)
        self.pause = Gtk.ToggleButton(label="Suspendre la maintenance automatique",
                                     active=maintenance.index.settings()["paused"])
        self.pause.connect("toggled", self.paused)
        toolbar.append(self.pause)
        self.category = Gtk.DropDown.new_from_strings(["Catégories", *CATEGORIES])
        self.category.connect("notify::selected", lambda *_: self.redraw())
        toolbar.append(self.category)
        self.level = Gtk.DropDown.new_from_strings(["Gravité", "INFO", "WARNING", "ERROR"])
        self.level.connect("notify::selected", lambda *_: self.redraw())
        toolbar.append(self.level)
        self.search = Gtk.SearchEntry(placeholder_text="Filtrer le journal", hexpand=True)
        self.search.connect("search-changed", lambda *_: self.redraw())
        toolbar.append(self.search)
        self.follow = Gtk.CheckButton(label="Suivre", active=True)
        toolbar.append(self.follow)
        toolbar.append(button("", self.copy, "edit-copy-symbolic", tooltip_text="Copier le journal affiché"))
        toolbar.append(button("", self.hide_events, "edit-clear-symbolic", tooltip_text="Effacer l’affichage"))
        toolbar.append(button("", self.history, "document-open-recent-symbolic", tooltip_text="Historique"))
        self.panel.append(toolbar)
        self.active = Gtk.Label(xalign=0, css_classes=["muted"], ellipsize=Pango.EllipsizeMode.END)
        self.panel.append(self.active)
        self.import_metrics = Gtk.Label(xalign=0, wrap=True, css_classes=["muted"])
        self.panel.append(self.import_metrics)
        self.panel.append(self.index_metrics)
        self.bar = Gtk.ProgressBar(visible=False)
        self.panel.append(self.bar)
        self.store = Gio.ListStore(item_type=Row)
        view = Gtk.ListView(model=Gtk.NoSelection.new(self.store), factory=factory(self.render))
        view.add_css_class("journal-lines")
        self.scroll = scroller(view, vexpand=True, min_content_height=75)
        self.reveal.set_child(self.scroll)
        self.panel.append(self.reveal)

    def paused(self, toggle):
        state = toggle.get_active()
        toggle.set_label("Reprendre la maintenance" if state else "Suspendre la maintenance automatique")
        self.maintenance.pause(state)

    @staticmethod
    def stringify(event):
        stamp, category, severity, line = event
        return f"[{stamp[11:]}] [{category}] [{severity}] {line}"

    def render(self, label, row):
        entry = row.data
        label.set_text(self.stringify(entry))
        color = CATEGORY_COLORS.get(entry[2], CATEGORY_COLORS.get(entry[1], "neutral"))
        label.set_css_classes([color, "journal-line"])

    def copy(self, _):
        self.window.get_display().get_clipboard().set("\n".join(self.stringify(e) for e in self.lines))

    def hide_events(self, _):
        self.floor = self.maintenance.journal.snapshot()[0]
        self.redraw()

    def history(self, widget):
        entries = self.maintenance.journal.history()
        show_details(widget, "Historique de maintenance", "\n".join(
            f"{entry[0][:10]} {self.stringify(entry)}" for entry in entries))

    def redraw(self, snapshot=None):
        serial, events = snapshot or self.maintenance.journal.snapshot()
        self.seen = serial
        if self.floor >= 0:
            events = events[-(serial - self.floor):] if serial > self.floor else ()
        category = self.category.get_selected_item().get_string()
        level = self.level.get_selected_item().get_string()
        query = key(self.search.get_text())
        self.lines = [e for e in events if (category == "Catégories" or e[1] == category)
                      and (level == "Gravité" or e[2] == level) and query in key(" ".join(e))]
        self.store.splice(0, self.store.get_n_items(), [Row(e) for e in self.lines])
        if self.follow.get_active():
            GLib.idle_add(self.end)

    def end(self):
        adj = self.scroll.get_vadjustment()
        adj.set_value(max(0, adj.get_upper() - adj.get_page_size()))
        return GLib.SOURCE_REMOVE

    def update(self):
        summary = self.window.album_import.imports_snapshot[1]
        state = (summary.get("active", 0), summary.get("waiting", 0), summary.get("errors", 0))
        if state != self.import_state:
            self.import_state = state
            if any(state):
                self.maintenance.journal.emit("DOWNLOAD", f"{state[0]} lot(s) actif(s) · {state[1]} en attente · {state[2]} avec échec")
        data = self.maintenance.journal.snapshot()
        if data[0] != self.seen:
            self.redraw(data)
        progress = self.maintenance.progress
        if not progress and state[0]:
            progress = ("DOWNLOAD", f"{state[0]} lot(s) en cours", summary.get("progress"))
        self.active.set_label(f"{progress[0]} · {progress[1]}" if progress else
                              self.maintenance.block or "En attente")
        if not self.import_stats_pending and monotonic() >= self.import_stats_at:
            self.import_stats_at = monotonic() + 5.
            self.import_stats_pending = True

            def display(future):
                self.import_stats_pending = False
                if not self.window.closed:
                    try:
                        self.import_summary = future.result()
                    except (OSError, sqlite3.Error, ValueError):
                        pass
                return GLib.SOURCE_REMOVE

            self.window.tag_reader.submit(self.enrichment_summary).add_done_callback(
                lambda result: GLib.idle_add(display, result))
        lyrics, lrc = self.maintenance.import_activity
        line = (f"{summary.get('downloads', 0)} piste(s) en réception · "
                f"{summary.get('download_slots', 0)}/{summary.get('download_ceiling', 0)} créneau(x) occupé(s) · "
                f"{lyrics} recherche(s) de texte · "
                f"{lrc} recherche(s) LRC · {summary.get('albums', 0)} album(s) audio complet(s)")
        if summary.get("pause_state") in {"suspending", "paused"}:
            line += " · " + ("Suspension des imports en cours…" if summary["pause_state"] == "suspending"
                             else "Imports et enrichissements suspendus")
        if rate := summary.get("audio_bytes_per_second"):
            line += f" · {rate / 1024**2:.2f} Mio/s mesurés"
        self.import_metrics.set_label(f"{line}\n{self.import_summary}" if self.import_summary else line)
        measured = self.maintenance.metrics
        if "index_pending" in measured and (auxiliary := measured.get("index_aux")) is not None:
            self.index_metrics.set_label(f"INDEX · {measured['index_pending']} pistes en file · "
                                         f"{auxiliary['errors']} erreurs média")
        self.bar.set_visible(bool(progress and progress[2] is not None))
        if progress and progress[2] is not None:
            self.bar.set_fraction(progress[2])

    def enrichment_summary(self):
        with self.maintenance.index.db(wait=.15) as db:
            tasks = {(kind, subject): (state, attempts) for kind, subject, state, attempts in db.execute("""
                SELECT kind,subject,state,attempts FROM tasks WHERE kind IN ('import_lyrics','import_cover')
            """)}
        groups = self.maintenance.import_queue.enrichment_groups() if self.maintenance.import_queue else []
        active = complete = partial = 0
        for group in groups:
            states = {tasks.get((kind, subject), ("review", 0))[0]
                      for kind, subjects in group.items() for subject in subjects}
            if states <= {"done"}:
                complete += 1
            elif states & {"queued", "running", "temporary", "suspended"}:
                active += 1
            else:
                partial += 1
        retries = sum(attempts > 1 for _, attempts in tasks.values())
        lines = [f"{active} album(s) en enrichissement · {complete} enrichi(s) · {partial} partiel(s) · "
                 f"{retries} tâche(s) relancée(s)"]
        if value := self.maintenance.import_lyrics_stats:
            timings = sorted(value["tracks_seconds"])
            if timings and value["seconds"] > 0:
                rate = 60 * value["lyrics"].get("found", 0) / value["seconds"]
                lines.append(f"Dernier lot · {rate:.1f} textes/min · {sum(timings) / len(timings):.2f} s par piste · "
                             f"P95 {timings[ceil(.95 * len(timings)) - 1]:.2f} s · {value['cache_hits']} recherches évitées")
            if value["providers"]:
                lines.append("Derniers fournisseurs sollicités · " + ", ".join(value["providers"]))
        limited = [source for source, value in import_metrics(self.maintenance.index).items()
                   if "limited_until" in value]
        if limited:
            lines.append("Fournisseurs suspendus · " + ", ".join(limited))
        return "\n".join(lines)


CARDS = (
    ("artists", "Artistes", "all"), ("albums", "Albums", "all"), ("total", "Chansons", "all"),
    ("lyrics", "Avec paroles", "lyrics"), ("lrc", "Avec LRC", "lrc"),
    ("incomplete", "Albums incomplets", "incomplete"), ("missing", "Albums manquants", "missing"),
    ("codex", "Recherches Codex", "codex"), ("sources", "Sources actives", "sources"),
    ("bytes", "Audio en bibliothèque", "storage"),
)
FILTERS = (("all", "Toutes les chansons"), ("lyrics", "Avec paroles"), ("no_lyrics", "Sans paroles"),
           ("lrc", "Avec LRC"), ("no_lrc", "Sans LRC"), ("bad_lrc", "LRC à vérifier"),
           ("incomplete", "Albums incomplets"),
           ("duplicates", "Flux audio identiques"))
SQL_FILTER = {
    "lyrics": "lyrics NOT IN ('absent','instrumental','inconnu')",
    "no_lyrics": "lyrics='absent'", "lrc": "lrc IN ('local','externe','généré','intégré')",
    "no_lrc": "lrc='absent'", "bad_lrc": "lrc='à vérifier'",
    "incomplete": "edition IN (SELECT subject FROM reviews WHERE kind='album' AND state='incomplete')",
    "duplicates": "audio!='' AND audio IN (SELECT audio FROM songs WHERE audio!='' GROUP BY audio HAVING count(*)>1)",
}


class ParoleView:
    def __init__(self, controller, maintenance):
        self.control = controller
        self.maintenance = maintenance
        self.index = maintenance.index
        self.page = 0
        self.scope = ()
        self.change = -1
        self.tv_state = ""
        self.tv_requested = 0.
        self.settings_dialog = None
        self.night_summary = None
        self.artist_search = None
        self.stats_at = 0.
        self.library_generation = 0
        self.library_pending = False
        self.library_page_requested = False
        self.child_models = OrderedDict()
        self.cards = {}
        gtkroot = self.root = box(spacing=8, css_classes=["parole"], margin_start=12, margin_end=12,
                                  margin_top=10, margin_bottom=8)
        split = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL, vexpand=True, wide_handle=True)
        gtkroot.append(split)
        main = box(vexpand=True)
        split.set_start_child(main)
        board = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, min_children_per_line=3,
                            max_children_per_line=6, row_spacing=6, column_spacing=6, homogeneous=True)
        board.set_activate_on_single_click(False)
        for label, caption, action in CARDS:
            value = Gtk.Label(label="—", xalign=0, css_classes=["card-value"])
            widget = button("", lambda _, which=action: self.card_action(which), css_classes=["stat-card"])
            tile = box(spacing=2)
            tile.append(value)
            tile.append(Gtk.Label(label=caption, xalign=0, css_classes=["muted"]))
            widget.set_child(tile)
            board.append(widget)
            self.cards[label] = value
        main.append(board)
        searchbar = box(Gtk.Orientation.HORIZONTAL)
        self.search = Gtk.SearchEntry(placeholder_text="Rechercher un artiste, un album ou un titre", hexpand=True)
        self.search.connect("search-changed", self.reset)
        searchbar.append(self.search)
        self.filter = Gtk.DropDown.new_from_strings([label for _, label in FILTERS])
        self.filter.connect("notify::selected", self.reset)
        searchbar.append(self.filter)
        searchbar.append(button("Tout afficher", self.all))
        searchbar.append(button("", lambda *_: self.maintenance.rescan(), "view-refresh-symbolic",
                                tooltip_text="Relancer l’inventaire"))
        main.append(searchbar)
        panes = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, vexpand=True, position=225,
                          wide_handle=True)
        main.append(panes)
        self.artists = Gio.ListStore(item_type=Row)
        tree = Gtk.TreeListModel.new(self.artists, False, False, self.children)
        self.tree_selection = Gtk.SingleSelection(model=tree, autoselect=False, can_unselect=True)
        self.tree_selection.connect("notify::selected-item", self.select_tree)
        tree_view = Gtk.ListView(model=self.tree_selection, factory=factory(self.render_tree, Gtk.TreeExpander))
        tree_view.add_css_class("library-tree")
        tree_view.connect("activate", self.open_tree)
        context = Gtk.Popover()
        context.set_parent(tree_view)
        context.set_child(button("Regrouper les artistes", lambda widget: (context.popdown(), self.artist_groups(widget))))
        click = Gtk.GestureClick(button=3)
        def show_context(_gesture, _count, x, y):
            rectangle = Gdk.Rectangle()
            rectangle.x, rectangle.y, rectangle.width, rectangle.height = int(x), int(y), 1, 1
            context.set_pointing_to(rectangle)
            context.popup()
        click.connect("pressed", show_context)
        tree_view.add_controller(click)
        artist_pane = box(vexpand=True)
        artist_pane.append(scroller(tree_view, min_content_width=160, vexpand=True))
        self.search_lyrics = button("Rechercher les paroles", self.search_artist_lyrics, sensitive=False)
        artist_pane.append(self.search_lyrics)
        panes.set_start_child(artist_pane)
        panes.set_shrink_start_child(False)
        self.songs = Gio.ListStore(item_type=Row)
        self.song_selection = Gtk.SingleSelection(model=self.songs, autoselect=False, can_unselect=True)
        self.song_selection.connect("notify::selected-item", self.select_song)
        table = Gtk.ColumnView(model=self.song_selection, show_column_separators=False, show_row_separators=True)
        for field, caption, expand in (
            ("title", "Titre", True), ("artist", "Artiste", True), ("album", "Album", True), ("duration", "Durée", False),
            ("lyrics", "Paroles", False), ("lrc", "LRC", False),
        ):
            col = Gtk.ColumnViewColumn.new(caption, factory(lambda widget, row, kind=field: self.render_cell(widget, row, kind)))
            col.set_expand(expand)
            col.set_resizable(True)
            if expand:
                col.set_fixed_width(135)
            table.append_column(col)
        table.connect("activate", lambda _, row: self.details(self.songs.get_item(row).data))
        panes.set_end_child(scroller(table, min_content_width=450))
        actions = box(Gtk.Orientation.HORIZONTAL, 6)
        self.row_count = Gtk.Label(xalign=0, hexpand=True, css_classes=["muted"])
        actions.append(self.row_count)
        self.previous = button("", lambda *_: self.move(-1), "go-previous-symbolic", tooltip_text="Page précédente")
        self.following = button("", lambda *_: self.move(1), "go-next-symbolic", tooltip_text="Page suivante")
        actions.append(self.previous)
        actions.append(self.following)
        self.detail_button = button("Détails", self.open_selected)
        actions.append(self.detail_button)
        manual = Gtk.MenuButton(icon_name="view-more-symbolic", tooltip_text="Actions sur la bibliothèque")
        popover = Gtk.Popover()
        menu = box(css_classes=["panel-content"])
        self.artist_codex = button("Rechercher l’artiste avec Codex", lambda *_: self.research_scope("discography"))
        self.album_codex = button("Rechercher l’album avec Codex", lambda *_: self.research_scope("album"))
        for widget in (self.artist_codex, self.album_codex,
                       button("Vérifier l’édition", lambda *_: self.review_scope("album")),
                       button("Vérifier la discographie", lambda *_: self.review_scope("discography")),
                       button("Regrouper les artistes", self.artist_groups),
                       button("Exclure cet artiste de la maintenance", self.exclude_artist),
                       button("Liste de lecture Strawberry", controller.show_library)):
            menu.append(widget)
        popover.set_child(menu)
        manual.set_popover(popover)
        actions.append(manual)
        main.append(actions)
        self.console = Console(maintenance, controller)
        split.set_end_child(self.console.panel)
        split.set_resize_end_child(False)
        split.set_shrink_end_child(False)
        split.set_position(585)

    def header(self):
        header = Gtk.HeaderBar()
        header.set_title_widget(Gtk.Box())
        wordmark = Gtk.Label(label="PAROLE", css_classes=["wordmark"])
        header.pack_start(wordmark)
        header.pack_end(button("", lambda *_: self.settings("general"), "emblem-system-symbolic", tooltip_text="Paramètres"))
        self.tv = button("", self.connect_tv, "video-display-symbolic", css_classes=["tv-button", "tv-off"],
                         tooltip_text="Connecter Kara à la télévision")
        self.tv.update_property([Gtk.AccessibleProperty.LABEL], ["Connecter Kara à la télévision"])
        header.pack_end(self.tv)
        header.pack_end(self.control.music_sync.button)
        header.pack_end(button("Importer des albums", self.control.open_import, "folder-download-symbolic",
                               css_classes=["primary-action"]))
        return header

    def connect_tv(self, _):
        self.tv_requested = monotonic()
        self.control.connect_display()
        if self.control.tv_error:
            Gtk.AlertDialog(message="Affichage Kara indisponible", detail=self.control.tv_error).show(self.control)
        self.update_tv()

    def update_tv(self):
        receiver = self.control.receiver
        seen = (receiver.secure_tv_seen if receiver.https_listener else receiver.tv_seen) if receiver else 0.
        now = monotonic()
        state = ("tv-ok" if seen and now - seen < 5 else
                 "tv-progress" if receiver and self.tv_requested and now - self.tv_requested < 20 else
                 "tv-error" if self.tv_requested and (not receiver or not seen) else "tv-off")
        if state == self.tv_state:
            return
        if self.tv_state:
            self.tv.remove_css_class(self.tv_state)
        self.tv_state = state
        self.tv.add_css_class(state)
        description = {"tv-progress": "Connexion télévision en cours", "tv-ok": "Télévision connectée",
                       "tv-off": "Télévision déconnectée", "tv-error": "Aucune réponse de la télévision"}[state]
        self.tv.update_property([Gtk.AccessibleProperty.DESCRIPTION], [description])
        if state != "tv-off" or seen:
            self.maintenance.journal.emit("TV", description, "WARNING" if state == "tv-error" else "INFO")

    @staticmethod
    def child_rows(db, parts):
        if len(parts) == 1:
            return [(*parts, r["edition"], f"{r['album'] or 'Sans album'}"
                    + (f" · {r['year']}" if r["year"] else ""))
                    for r in db.execute("SELECT edition,album,year FROM songs WHERE owner=? "
                                        "GROUP BY edition ORDER BY search,edition", parts)]
        return [(*parts, r["id"], r["title"]) for r in db.execute(
            "SELECT id,title FROM songs WHERE edition=? ORDER BY disc,track,title,id", (parts[1],))]

    @staticmethod
    def patch_tree(model, rows):
        current = [model.get_item(i).data for i in range(model.get_n_items())]
        # Keep unchanged list items, including their GtkTreeListRow expansion.
        for action, i, end, j, stop in reversed(SequenceMatcher(None, current, rows, autojunk=False).get_opcodes()):
            if action != "equal":
                model.splice(i, end - i, [Row(value) for value in rows[j:stop]])

    def children(self, row):
        parts = row.data
        if len(parts) == 5:
            return None
        if parts in self.child_models:
            self.child_models.move_to_end(parts)
            return self.child_models[parts]
        model = Gio.ListStore(item_type=Row)
        self.child_models[parts] = model
        if len(self.child_models) > 64:
            self.child_models.popitem(last=False)

        def load():
            with self.index.db(wait=.15) as db:
                return self.child_rows(db, parts)

        def display(future):
            if not self.control.closed:
                try:
                    self.patch_tree(model, future.result())
                except (sqlite3.Error, ValueError, OSError):
                    pass
            return GLib.SOURCE_REMOVE

        self.control.tag_reader.submit(load).add_done_callback(lambda result: GLib.idle_add(display, result))
        return model

    @staticmethod
    def render_tree(expander, raw):
        expander.set_list_row(raw)
        label = expander.get_child()
        if not label:
            label = Gtk.Label(xalign=0, ellipsize=Pango.EllipsizeMode.END)
            expander.set_child(label)
        data = raw.get_item().data
        label.set_label(data[-1])
        label.set_tooltip_text(data[-1])

    def select_tree(self, *_):
        selected = self.tree_selection.get_selected_item()
        self.scope = selected.get_item().data if selected else ()
        self.search_lyrics.set_sensitive(bool(self.scope))
        self.reset()

    def open_tree(self, _, pos):
        item = self.tree_selection.get_model().get_item(pos)
        if item.is_expandable():
            item.set_expanded(not item.get_expanded())
        elif len(item.get_item().data) == 5:
            song = self.index.song(item.get_item().data[3])
            if song:
                self.details(song)

    def select_song(self, *_):
        self.detail_button.set_sensitive(self.song_selection.get_selected_item() is not None)

    def all(self, *_):
        self.tree_selection.unselect_all()
        self.scope = ()
        self.search.set_text("")
        self.filter.set_selected(0)
        self.reset()

    def move(self, direction):
        self.page = max(0, self.page + direction)
        self.render_songs()

    def reset(self, *_):
        self.page = 0
        self.render_songs()

    def render_songs(self):
        self.library_generation += 1
        self.request_library(page=True)

    def page_query(self):
        conditions, args = [], []
        for term in key(self.search.get_text())[:180].split():
            conditions.append("instr(search,?)")
            args.append(term)
        if self.scope:
            conditions.append("owner=?")
            args.append(self.scope[0])
        if len(self.scope) >= 3:
            conditions.append("edition=?")
            args.append(self.scope[1])
        if len(self.scope) == 5:
            conditions.append("id=?")
            args.append(self.scope[3])
        if expression := SQL_FILTER.get(FILTERS[self.filter.get_selected()][0]):
            conditions.append(expression)
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        return where, tuple(args), self.page

    def load_library(self, page, artists, branches, stats):
        began = monotonic()
        result = {}
        with self.index.db(wait=.15) as db:
            # Use one SQLite snapshot for the visible page, dimension counts
            # and expanded nodes. No transaction spans a GTK callback.
            db.execute("BEGIN")
            if page is not None:
                where, args, number = page
                count = (db.execute("SELECT count(*) FROM songs" + where, args).fetchone()[0] if where
                         else db.execute("SELECT total FROM library_stats").fetchone()[0])
                number = min(number, max(0, (count-1) // PAGE))
                rows = db.execute("SELECT * FROM songs" + where + " ORDER BY owner,album,disc,track,title,id LIMIT ? OFFSET ?",
                                  (*args, PAGE, number * PAGE)).fetchall()
                result["page"] = number, count, rows
            if artists:
                result["artists"] = [tuple(r) for r in db.execute(
                    "SELECT owner FROM library_artists ORDER BY owner COLLATE NOCASE,owner")]
                result["branches"] = [(parts, self.child_rows(db, parts)) for parts in branches]
            if stats:
                counts = dict(db.execute("SELECT * FROM library_stats WHERE id=1").fetchone())
                counts.update(artists=db.execute("SELECT count(*) FROM library_artists").fetchone()[0],
                              albums=db.execute("SELECT count(*) FROM library_editions").fetchone()[0],
                              errors=db.execute("SELECT count(*) FROM songs WHERE json_extract(info,'$.probe_error')=1").fetchone()[0],
                              incomplete=db.execute("SELECT count(*) FROM reviews WHERE kind='album' AND state='incomplete'").fetchone()[0],
                              missing=db.execute("""SELECT count(DISTINCT json_extract(j.value,'$.id'))
                                FROM reviews r, json_each(r.value,'$.missing') j
                                WHERE r.kind='discography' AND NOT EXISTS(SELECT 1 FROM exclusions
                                    WHERE kind='release' AND subject=json_extract(j.value,'$.id'))""").fetchone()[0],
                              sources=db.execute("SELECT count(*) FROM sources WHERE state='active'").fetchone()[0],
                              codex=db.execute("SELECT count(*) FROM tasks WHERE kind='codex' AND state IN ('queued','running','temporary','suspended')").fetchone()[0])
                result["stats"] = counts
        return result, monotonic() - began

    def request_library(self, *, page=False):
        revision = self.maintenance.revision
        dirty = self.change != revision
        self.library_page_requested |= page
        if self.library_pending:
            return
        now = monotonic()
        stats = dirty or now >= self.stats_at + 3.
        page = self.library_page_requested or dirty
        if not (stats or page):
            return
        generation = self.library_generation
        self.library_page_requested = False
        self.library_pending = True
        branches = tuple(self.child_models) if dirty else ()

        def finish(future):
            self.library_pending = False
            if self.control.closed:
                return GLib.SOURCE_REMOVE
            try:
                snapshot, read_seconds = future.result()
            except (sqlite3.Error, ValueError, OSError):
                self.library_page_requested |= page
                return GLib.SOURCE_REMOVE
            began = monotonic()
            if "artists" in snapshot:
                self.patch_tree(self.artists, snapshot["artists"])
                for parts, rows in snapshot["branches"]:
                    if model := self.child_models.get(parts):
                        self.patch_tree(model, rows)
                self.change = revision
            if "page" in snapshot and generation == self.library_generation:
                self.display_page(*snapshot["page"])
            if "stats" in snapshot:
                self.stats_at = now
                self.display_stats(snapshot["stats"])
            self.maintenance.metrics.update(gtk_library_read=read_seconds, gtk_library_update=monotonic()-began)
            self.request_library()
            return GLib.SOURCE_REMOVE

        self.control.tag_reader.submit(self.load_library, self.page_query() if page else None, dirty, branches,
                                       stats).add_done_callback(lambda result: GLib.idle_add(finish, result))

    def display_page(self, number, count, rows):
        self.page = number
        old = self.song_selection.get_selected_item()
        old_id = old.data["id"] if old else None
        if self.update_song_rows(rows):
            self.song_selection.set_selected(next((i for i, r in enumerate(rows) if r["id"] == old_id),
                                                  Gtk.INVALID_LIST_POSITION))
        self.select_song()
        self.row_count.set_text(f"{count:,} chansons".replace(",", " ") +
                                (f" · page {self.page+1}/{(count+PAGE-1)//PAGE}" if count > PAGE else ""))
        self.previous.set_sensitive(self.page > 0)
        self.following.set_sensitive((self.page + 1) * PAGE < count)

    def update_song_rows(self, rows):
        current = [self.songs.get_item(i) for i in range(self.songs.get_n_items())]
        rendered = ("id", "title", "artist", "album", "duration", "lyrics", "lrc", "kara")
        before = [tuple(item.data[k] for k in rendered) for item in current]
        after = [tuple(row[k] for k in rendered) for row in rows]
        changed = False
        for action, i, end, j, stop in reversed(SequenceMatcher(None, before, after, autojunk=False).get_opcodes()):
            if action == "equal":
                # Keep the row's identity while supplying current provenance
                # and versions to the detail editor.
                for item, row in zip(current[i:end], rows[j:stop]):
                    item.data = row
            else:
                changed = True
                self.songs.splice(i, end - i, [Row(row) for row in rows[j:stop]])
        return changed

    @staticmethod
    def render_cell(widget, row, field):
        value = row.data[field]
        widget.set_css_classes(["song-cell"])
        if field == "duration":
            seconds = int(value)
            widget.set_text(f"{seconds // 60}:{seconds % 60:02}" if seconds else "—")
        elif field in ("lyrics", "lrc", "kara"):
            label, color = image_state(value, field)
            widget.set_text(label)
            widget.add_css_class(color)
        else:
            widget.set_text(value)
        widget.set_tooltip_text(widget.get_text())

    def card_action(self, action):
        if action in ("codex", "sources", "storage"):
            self.settings(action)
        elif action == "missing":
            self.missing()
        else:
            self.all()
            self.filter.set_selected(next(i for i, (kind, _) in enumerate(FILTERS) if kind == action))

    def refresh(self):
        if self.control.closed:
            return GLib.SOURCE_REMOVE
        if not self.control.get_visible():
            return GLib.SOURCE_CONTINUE
        try:
            self.refresh_state()
        except (sqlite3.Error, OSError, ValueError):
            pass
        return GLib.SOURCE_CONTINUE

    def refresh_state(self):
        self.update_tv()
        self.console.update()
        self.request_library()
        if self.night_summary is not None and self.night_summary.get_mapped():
            self.request_night_summary()

    def display_stats(self, stats):
        self.maintenance.metrics["index_aux"] = {"errors": stats["errors"]}
        for name, value in self.cards.items():
            data = stats[name]
            if data is None:
                formatted = "—"
            elif name in ("lyrics", "lrc"):
                formatted = f"{100*data/stats['total']:.1f} %" if stats["total"] else "—"
            elif name == "bytes":
                formatted = display_size(data)
            else:
                formatted = f"{data:,}".replace(",", " ")
            value.set_text(formatted)

    @staticmethod
    def activity_lines(label, counts):
        network_lyrics, network_lrc = counts.get("network_lyrics", 0), counts.get("network_lrc", 0)
        local_lyrics, local_lrc = counts.get("local_lyrics", 0), counts.get("local_lrc", 0)
        return (
            f"{label} · Réseau {network_lyrics} textes / {network_lrc} LRC · Bibliothèque {local_lyrics} textes / {local_lrc} LRC",
            f"{counts.get('memory', 0)} reports mémoire · {counts.get('quota', 0)} reports quota",
        )

    def request_night_summary(self):
        if monotonic() < getattr(self, "night_summary_at", 0.):
            return
        self.night_summary_at = monotonic() + 3.

        def load():
            return self.index.night_lrc_status(), self.index.closed_progress()

        def finish(future):
            if not self.control.closed and self.night_summary is not None:
                try:
                    self.refresh_night_summary(future.result())
                except (OSError, sqlite3.Error, ValueError):
                    pass
            return GLib.SOURCE_REMOVE

        self.control.tag_reader.submit(load).add_done_callback(lambda result: GLib.idle_add(finish, result))

    def refresh_night_summary(self, snapshot=None):
        lrc, closed = snapshot or (self.index.night_lrc_status(), self.index.closed_progress())
        lines = []
        night = self.maintenance.night
        state = night.desktop
        power = {True: "Secteur", False: "Batterie"}.get(night.power, "Alimentation inconnue")
        lid = "Capot inconnu" if state is None else ("Capot fermé" if state.lid else "Capot ouvert")
        screens = "Écrans inconnus" if state is None else ("Écrans éteints" if state.screen_off else "Écrans allumés")
        lock = ("État du verrou inconnu" if state is None else
                ("Session verrouillée" if state.locked else "Session déverrouillée"))
        inhibitor = "Inhibiteur de capot actif" if night.lid_fd is not None else "Inhibiteur de capot absent"
        lines.extend((f"{lid} · {power} · {screens} · {lock}", inhibitor))
        if night.reason:
            lines.append(night.reason)
        progress = 100 * lrc["present"] / lrc["total"] if lrc["total"] else 0.
        lines.extend((
            f"{lrc['total']} morceaux · {lrc['present']} avec LRC · {lrc['missing']} sans LRC",
            f"Progression {progress:.1f} % · {lrc['retrieved']} LRC récupérés cette nuit",
            "Disponibles · " + (", ".join(lrc["ready"]) or "aucun"),
            "Quotas en attente · " + (", ".join(
                f"{name} ({datetime.fromtimestamp(stamp):%d/%m %H:%M})"
                for name, stamp in lrc["waiting"]) or "aucun"),
            "Prochaine reprise · " + (f"{datetime.fromtimestamp(lrc['resume']):%d/%m %H:%M}"
                                      if lrc["resume"] is not None else "aucune échéance"),
        ))
        if closed:
            stamp = datetime.fromtimestamp(closed["started"]).strftime("%d/%m à %H:%M")
            done = closed["completed"].get("lrc", 0)
            lines.append(f"Capot fermé le {stamp} · {done} recherches LRC terminées · "
                         f"{closed['interrupted']} interruptions · {closed['deferred']} reports")
        self.night_summary.set_text("\n".join(lines))

    def selected(self):
        return self.song_selection.get_selected_item()

    def open_selected(self, _):
        if row := self.selected():
            self.details(row.data)

    def details(self, row):
        from _kara_files import lrc_token

        window = Gtk.Window(title=row["title"], transient_for=self.control, destroy_with_parent=True,
                            default_width=720, default_height=550)
        panel = box(css_classes=["panel-content"])
        window.set_child(panel)
        panel.append(Gtk.Label(label=row["title"], xalign=0, css_classes=["track-title"]))
        panel.append(Gtk.Label(label=f"{row['artist']} · {row['album']}", xalign=0, wrap=True, css_classes=["muted"]))
        metadata = json.loads(row["info"])
        identity = row["id"]
        with self.index.db() as db:
            checks = db.execute("""SELECT kind,state,attempts,detail,due FROM tasks WHERE subject=?
                                   AND kind NOT IN ('prepare', 'transcription') ORDER BY kind""", (identity,)).fetchall()
            identification = db.execute("SELECT value FROM reviews WHERE kind='metadata' AND subject=? AND state='identified'",
                                        (identity,)).fetchone()
            unison = db.execute("SELECT value FROM reviews WHERE kind='unison_candidate' AND subject=? AND state='found'",
                               (identity,)).fetchone()
            provenance = [dict(reference) for reference in db.execute("""
                SELECT provider,external_id,edition,recording,confidence,tags,evidence FROM canonical_references
                WHERE path=? AND song IN ('',?) ORDER BY provider,external_id LIMIT 30
            """, (row["path"], identity))]
        updated = datetime.fromtimestamp(row["checked"]).strftime("%d/%m/%Y à %H:%M")
        provider = metadata.get("lrc_provider")
        lrc = row["lrc"] + (f" ({provider})" if provider else "")
        details = [f"Paroles · {metadata.get('lyrics_provider') or row['lyrics']}", f"LRC source · {lrc}",
                   f"Vérifié le {updated}"]
        details.extend(f"{t['kind']} · {t['detail']} ({t['attempts']} tentatives)" for t in checks
                       if t["detail"] and t["state"] in ("review", "temporary", "suspended"))
        panel.append(Gtk.Label(label="   ·   ".join(details), xalign=0, wrap=True))
        view = Gtk.TextView(wrap_mode=Gtk.WrapMode.WORD_CHAR, monospace=True, top_margin=8, left_margin=8)
        view.set_editable(False)
        buffer = view.get_buffer()
        forms = Gtk.DropDown.new_from_strings(["Paroles", "LRC"])
        editbar = box(Gtk.Orientation.HORIZONTAL)
        panel.append(editbar)
        editbar.append(forms)
        if provenance:
            for reference in provenance:
                reference["tags"] = json.loads(reference["tags"])
                reference["evidence"] = json.loads(reference["evidence"])
            editbar.append(button("Provenance", lambda widget: show_details(
                widget, "Identité et provenance", json.dumps(provenance, ensure_ascii=False, indent=2))))
        path = Path(row["path"])
        from _parole_lyrics_lookup import recording_candidates

        candidates = recording_candidates(self.index.path, path, include_text=True)
        decisions = [{item["decision"] for item in group} for group in candidates.values()]
        if any("ambiguous" in group and "selected" not in group for group in decisions):
            panel.append(button("Correspondances à vérifier", lambda widget: show_details(
                widget, "Candidats conservés pour cet enregistrement",
                json.dumps(candidates, ensure_ascii=False, indent=2))))
        versions = {}
        def replace_text(value):
            # A format change or cancellation starts a fresh undo history.
            # Ctrl+Z must never restore another format into this buffer.
            buffer.begin_irreversible_action()
            try:
                buffer.set_text(value)
            finally:
                buffer.end_irreversible_action()

        def reload_text(*_):
            suffix = ".lrc" if forms.get_selected() else ".txt"
            version, value = sidecar(path, suffix)
            versions[suffix] = json.loads(version) if version and version != "invalid" else None
            if not value and suffix == ".txt" and version != "invalid":
                value = metadata.get("plain", "")
                if not value and row["lyrics"] == "LRC":
                    from _paroles_lrc import parse_lrc
                    value = "\n".join(line for _, line in parse_lrc(sidecar(path, ".lrc")[1]) if line)
            view.set_editable(False)
            forms.set_sensitive(True)
            edit.set_sensitive(version != "invalid")
            save.set_sensitive(False)
            cancel.set_sensitive(False)
            replace_text(value)
            status.set_text("Fichier illisible ou trop volumineux" if version == "invalid" else "")

        def start_edit(_):
            forms.set_sensitive(False)
            view.set_editable(True)
            view.grab_focus()
            save.set_sensitive(True)
            cancel.set_sensitive(True)

        status = Gtk.Label(xalign=0, wrap=True)
        edit = button("Corriger", start_edit)

        def commit(_):
            text = buffer.get_text(*buffer.get_bounds(), False)
            suffix = ".lrc" if forms.get_selected() else ".txt"
            try:
                save_correction(self.index, identity, suffix, versions[suffix], text,
                                expected_audio=row["token"])
            except (OSError, ValueError, sqlite3.Error, SystemExit) as error:
                status.set_text(str(error))
                return
            self.maintenance.index_wake.set()
            reload_text()
            status.set_text("Minutage validé" if suffix == ".lrc" else "Texte validé")

        save = button("Enregistrer", commit)
        cancel = button("Annuler la correction", reload_text)
        editbar.append(edit)
        editbar.append(save)
        editbar.append(cancel)
        if identification:
            found = json.loads(identification[0])
            if found.pop("token", None) == row["token"]:
                editbar.append(button("Identification", lambda widget: show_details(
                    widget, "Identification MusicBrainz", json.dumps(found, indent=2, ensure_ascii=False))))
        if unison:
            found = json.loads(unison["value"])
            if found["token"] == row["token"] and (candidate := found["candidate"]):
                links = box(Gtk.Orientation.HORIZONTAL)
                links.append(Gtk.LinkButton.new_with_label(candidate["url"], "Candidat de paroles sur Unison"))
                links.append(Gtk.LinkButton.new_with_label(candidate["profile"], "Contribution de " + candidate["contributor"]))
                panel.append(links)
        panel.append(scroller(view, vexpand=True))
        panel.append(status)
        forms.connect("notify::selected", reload_text)
        reload_text()
        window.present()

    def manual_codex(self, kind, subject, label=None):
        if not self.index.settings()["codex"]:
            if label:
                label.set_text("Active d’abord Codex dans les paramètres.")
            else:
                self.settings("codex")
            return
        queued = self.maintenance.queue_codex(kind, subject, manual=True)
        if label:
            label.set_text("Demande Codex enregistrée ou déjà présente." if queued else
                           "Catégorie de recherche désactivée.")

    def research_scope(self, kind):
        if self.scope and (kind != "album" or len(self.scope) >= 3):
            self.manual_codex(kind, self.scope[1] if kind == "album" else self.scope[0])

    def search_artist_lyrics(self, _widget):
        if not self.scope:
            return
        if self.artist_search and not self.artist_search["done"]:
            self.artist_search["window"].present()
            return
        if self.artist_search:
            self.artist_search["window"].destroy()
        artist, request, started = self.scope[0], uuid4().hex, time()
        window = Gtk.Window(title=f"Recherche de paroles · {artist}", transient_for=self.control,
                            destroy_with_parent=True, hide_on_close=True, default_width=520)
        content = box(spacing=12, margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        status = Gtk.Label(label="Inventaire des morceaux locaux…", xalign=0, wrap=True)
        counters = Gtk.Label(xalign=0, wrap=True)
        current = Gtk.Label(xalign=0, wrap=True)
        errors = Gtk.Label(xalign=0, wrap=True)
        progress = Gtk.ProgressBar(show_text=True)
        state = self.artist_search = {"window": window, "done": False, "paths": None,
                                      "results": {}, "polling": False, "cancelling": False}
        pool = self.control.tag_reader

        def stop(_button):
            state["cancelling"] = True
            cancel.set_sensitive(False)
            status.set_text("Annulation de la recherche…")
            pool.submit(self.maintenance.cancel_artist_lyrics, request).add_done_callback(
                lambda future: GLib.idle_add(stopped, future))

        def stopped(future):
            try:
                future.result()
            except (OSError, ValueError, sqlite3.Error):
                state["cancelling"] = False
                status.set_text("Annulation momentanément indisponible.")
                cancel.set_sensitive(True)
            else:
                finish("Recherche annulée. Les paroles enregistrées sont disponibles.")

        def finish(message):
            state["done"] = True
            status.set_text(message)
            cancel.set_sensitive(False)
            details.set_sensitive(bool(state["results"]))
            current.set_text("")

        def show_audit(widget):
            results = state["results"]
            outcomes, sources = artist_summary(results)
            report = {"morceaux": len(results), "bilan": {label: outcomes.get(key, 0)
                      for key, label in ARTIST_OUTCOMES.items()}, "fournisseurs": sources,
                      "détails": {str(Path(path).relative_to(self.index.root)): {
                          "état": ARTIST_OUTCOMES[track_outcome(row)], **row} for path, row in results.items()}}
            show_details(widget, "Bilan détaillé de la recherche", json.dumps(report, ensure_ascii=False, indent=2))

        cancel = button("Annuler la recherche", stop)
        details = button("Bilan détaillé", show_audit, sensitive=False)
        for widget in (status, progress, counters, current, errors, details, cancel):
            content.append(widget)
        window.set_child(content)

        def draw(future):
            state["polling"] = False
            if state["done"] or state["cancelling"]:
                return GLib.SOURCE_REMOVE
            try:
                results, running = future.result()
            except (OSError, ValueError, sqlite3.Error):
                status.set_text("État de la file momentanément indisponible.")
                return GLib.SOURCE_REMOVE
            state["results"].update(results)
            values = state["results"]
            total, completed = len(state["paths"]), len(values)
            counts, sources = artist_summary(values)
            lines = [" · ".join(f"{counts.get(key, 0)} {ARTIST_OUTCOMES[key].lower()}"
                                for key in group) for group in (("present", "lyrics", "lrc"),
                                                               ("review", "missing", "limited", "error"))]
            counters.set_text("\n".join(lines))
            if sources:
                errors.set_text("\n".join(f"{name} · {data.get('requests', 0)} requêtes · "
                                          f"{data.get('found', 0)} correspondances · "
                                          f"{data.get('published_lyrics', 0) + data.get('published_lrc', 0)} publications"
                                          for name, data in sources.items()))
            progress.set_fraction(completed / total if total else 1.)
            progress.set_text(f"{completed}/{total} morceaux analysés")
            current.set_text("Recherche en cours · " + ", ".join(Path(path).stem for path in running[:4])
                             if running else "")
            if completed == total and not running:
                finish("Recherche terminée.")
            else:
                status.set_text("Recherche en cours" if running else "En attente d’un créneau de recherche Parole")
            return GLib.SOURCE_REMOVE

        def inspect():
            paths = state["paths"]
            live = self.maintenance.artist_lyrics_live(set(paths.values()))
            durable = artist_results(self.index, paths, started)
            results = dict(durable)
            results.update({path: result for path, result in live.items()
                            if path in paths and path not in results and result is not None})
            # A live result can precede the committed full audit. Wait for
            # the receipt before declaring the window complete.
            running = [path for path in live if path in paths and path not in durable]
            return results, running

        def poll():
            if state["done"] or self.control.closed:
                return GLib.SOURCE_REMOVE
            if not state["polling"] and not state["cancelling"] and state["paths"] is not None:
                state["polling"] = True
                pool.submit(inspect).add_done_callback(lambda future: GLib.idle_add(draw, future))
            return GLib.SOURCE_CONTINUE

        def admitted(future):
            try:
                state["paths"] = future.result()
            except (OSError, ValueError, sqlite3.Error):
                finish("L’inventaire ou la file Parole est indisponible.")
                return GLib.SOURCE_REMOVE
            if state["done"] or state["cancelling"]:
                return GLib.SOURCE_REMOVE
            self.maintenance.prioritize_artist_lyrics(set(state["paths"].values()))
            poll()
            GLib.timeout_add(700, poll)
            return GLib.SOURCE_REMOVE

        window.present()
        pool.submit(queue_artist, self.index, artist, request).add_done_callback(
            lambda future: GLib.idle_add(admitted, future))

    def exclude_artist(self, widget):
        if not self.scope:
            return
        owner = self.scope[0]
        confirm = Gtk.AlertDialog(message=f"Exclure {owner} des vérifications automatiques ?",
                                  buttons=["Annuler", "Exclure"], cancel_button=0, default_button=0)

        def finish(dialog, result):
            try:
                allowed = dialog.choose_finish(result) == 1
            except GLib.Error:
                return
            if not allowed:
                return
            self.index.exclude("artist", owner)
            self.maintenance.rescan()

        confirm.choose(self.control, None, finish)

    def review_scope(self, kind):
        if not self.scope or kind == "album" and len(self.scope) < 3:
            return
        subject = self.scope[1] if kind == "album" else self.scope[0]
        with self.index.db() as db:
            data = db.execute("SELECT * FROM reviews WHERE kind=? AND subject=?", (kind, subject)).fetchone()
        if kind == "discography" and data:
            self.missing(subject)
            return
        if not data:
            self.index.enqueue(kind, subject, encoded_key(subject), priority=11, force=True)
            self.maintenance.wake.set()
            self.maintenance.journal.emit("ALBUM" if kind == "album" else "DISCOGRAPHIE", "Vérification demandée")
            return
        value = json.loads(data["value"])
        self.review_dialog(kind, subject, value.get("title") or self.scope[-1], data["state"], value)

    def review_dialog(self, kind, subject, title, state, value):
        window = Gtk.Window(title=title, transient_for=self.control, destroy_with_parent=True,
                            default_width=680, default_height=370)
        panel = box(css_classes=["panel-content"])
        window.set_child(panel)
        status = Gtk.Label(label=state, xalign=0, wrap=True)
        panel.append(status)
        notes = Gtk.TextView(editable=False, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        notes.get_buffer().set_text(json.dumps(value, indent=2, ensure_ascii=False))
        panel.append(scroller(notes, vexpand=True))
        actions = box(Gtk.Orientation.HORIZONTAL)
        panel.append(actions)

        def exclude(_):
            with self.index.db() as db:
                db.execute("INSERT OR IGNORE INTO exclusions VALUES(?,?)", (kind, subject))
                row = db.execute("SELECT job FROM import_permissions WHERE kind=? AND subject=?", (kind, subject)).fetchone()
            if row and row["job"] and self.maintenance.import_queue:
                self.maintenance.import_queue.transition(row["job"], "queued", "cancelled", "Édition exclue")
            self.maintenance.cancel(kind, subject)
            self.maintenance.revision += 1
            status.set_text("Exclusion enregistrée")

        actions.append(button("Exclure cette édition", exclude))
        if state in ("incomplete", "missing"):
            actions.append(button("Autoriser l’import…", lambda *_: self.allow_import(window, kind, subject, title)))
        if kind == "album":
            actions.append(button("Rechercher avec Codex", lambda *_: self.manual_codex(kind, subject, status)))
        window.present()

    def allow_import(self, parent, kind, subject, title):
        dialog = Gtk.Window(title="Autoriser une édition", transient_for=parent, modal=True,
                            destroy_with_parent=True, default_width=540)
        area = box(css_classes=["panel-content"])
        dialog.set_child(area)
        area.append(Gtk.Label(label=title, xalign=0, wrap=True))
        link = Gtk.Entry(placeholder_text="https://music.youtube.com/browse/…", max_length=250)
        area.append(link)
        consent = Gtk.CheckButton(label="J’ai vérifié l’édition et dispose des droits pour cet import.")
        area.append(consent)
        status = Gtk.Label(xalign=0, wrap=True)
        area.append(status)

        def approve(_):
            if not consent.get_active():
                status.set_text("Confirme l’édition et les droits de récupération.")
                return
            try:
                self.maintenance.admit_import(kind, subject, title, link.get_text().strip())
            except (OSError, ValueError, sqlite3.Error) as error:
                status.set_text(str(error))
            else:
                status.set_text("Autorisation enregistrée. La file commune traitera la demande si l’import automatique correspondant est activé.")

        area.append(button("Enregistrer l’autorisation", approve))
        dialog.present()

    def missing(self, owner=None):
        window = Gtk.Window(title="Publications manquantes", transient_for=self.control,
                            destroy_with_parent=True, default_width=690, default_height=490)
        with self.index.db() as db:
            rows = db.execute("""
                SELECT r.subject AS artist, j.value AS release FROM reviews r, json_each(r.value,'$.missing') j
                WHERE r.kind='discography' AND (? IS NULL OR r.subject=?)
                  AND NOT EXISTS(SELECT 1 FROM exclusions WHERE kind='release' AND subject=json_extract(j.value,'$.id'))
                GROUP BY json_extract(j.value,'$.id') ORDER BY r.subject,json_extract(j.value,'$.title')
            """, (owner, owner)).fetchall()
        store = Gio.ListStore(item_type=Row)
        store.splice(0, 0, [Row((r["artist"], json.loads(r["release"]))) for r in rows])
        view = Gtk.ListView(model=Gtk.SingleSelection.new(store), factory=factory(
            lambda widget, row: widget.set_text(f"{row.data[0]} · {row.data[1]['title']} · {row.data[1]['date']}")))
        view.connect("activate", lambda _, pos: self.open_missing(store.get_item(pos).data))
        panel = box(css_classes=["panel-content"])
        panel.append(Gtk.Label(label=f"{len(rows)} publications identifiées", xalign=0))
        panel.append(scroller(view, vexpand=True))
        window.set_child(panel)
        window.present()

    def open_missing(self, item):
        artist, release = item
        self.review_dialog("release", release["id"], release["title"], "missing", dict(release, artiste=artist))

    def settings(self, page):
        if self.settings_dialog and self.settings_dialog.get_visible():
            self.settings_dialog.get_child().get_end_child().set_visible_child_name(page)
            self.settings_dialog.present()
            return
        window = self.settings_dialog = Gtk.Window(title="Paramètres de Parole", transient_for=self.control,
                                                 destroy_with_parent=True, default_width=850, default_height=580)
        stack = Gtk.Stack()
        sidebar = Gtk.StackSidebar(stack=stack, width_request=158)
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, start_child=sidebar, end_child=stack,
                         shrink_start_child=False, resize_start_child=False)
        window.set_child(body)
        config = self.index.settings()

        def save(name, value):
            if name == "night_enabled":
                # Opt-in replaces legacy automatic preparation. Switching
                # off night must not immediately restart that same heavy work.
                self.index.configure(night_enabled=value)
                if value:
                    self.maintenance.rescan()
            else:
                self.index.configure(**{name: value})
            self.maintenance.wake.set()

        def toggles(area, specs):
            for name, text in specs:
                widget = Gtk.CheckButton(label=text, active=config[name])
                widget.connect("toggled", lambda w, field=name: save(field, w.get_active()))
                area.append(widget)

        def number(area, name, title, low, high, step=1):
            line = box(Gtk.Orientation.HORIZONTAL)
            line.append(Gtk.Label(label=title, xalign=0, hexpand=True))
            spin = Gtk.SpinButton.new_with_range(low, high, step)
            spin.set_value(config[name])
            spin.connect("value-changed", lambda w: save(name, w.get_value_as_int()))
            line.append(spin)
            area.append(line)

        new = lambda: box(css_classes=["panel-content"])
        general = new()
        general.append(button("Ouvrir BlackBox Studio", self.control.show_blackbox_studio))
        toggles(general, (("startup", "Inventaire automatique au lancement"), ("lyrics", "Rechercher les paroles manquantes"),
                          ("lrc", "Rechercher les LRC"),
                          ("albums", "Vérifier les albums"), ("discography", "Vérifier les discographies"),
                          ("cover", "Rechercher progressivement les pochettes"), ("metadata", "Vérifier les métadonnées manquantes"),
                          ("import_tracks", "Importer les pistes des éditions vérifiées autorisées"),
                          ("import_albums", "Importer les albums absents autorisés")))
        number(general, "scan_minutes", "Fréquence des inventaires (minutes)", 1, 1440)
        number(general, "review_days", "Fréquence des vérifications éditoriales (jours)", 1, 365)
        criteria = box(Gtk.Orientation.HORIZONTAL)
        criteria.append(Gtk.Label(label="Stratégie d’import", xalign=0, hexpand=True))
        codes = list(STRATEGIES)
        strategy = Gtk.DropDown.new_from_strings(list(STRATEGIES.values()))
        strategy.set_selected(codes.index(config["import_strategy"]) if config["import_strategy"] in codes else 0)
        strategy.connect("notify::selected", lambda w, _: save("import_strategy", codes[w.get_selected()]))
        criteria.append(strategy)
        general.append(criteria)
        preference = box(Gtk.Orientation.HORIZONTAL)
        preference.append(Gtk.Label(label="Fournisseur préféré", xalign=0, hexpand=True))
        names = ["Aucun", *AUTOMATIC]
        provider = Gtk.DropDown.new_from_strings(names)
        provider.set_selected(names.index(config["import_preferred"]) if config["import_preferred"] in AUTOMATIC else 0)
        provider.connect("notify::selected", lambda w, _: save(
            "import_preferred", names[w.get_selected()] if w.get_selected() else ""))
        preference.append(provider)
        general.append(preference)
        categories = box(Gtk.Orientation.HORIZONTAL, 5)
        for kind, title in (("Album", "Studio"), ("EP", "EP"), ("Mixtape/Street", "Mixtapes"),
                            ("Live", "Live"), ("Compilation", "Compilations"), ("Single", "Singles"),
                            ("Collaboration", "Collaborations"), ("Remix", "Remixes")):
            widget = Gtk.CheckButton(label=title, active=kind in config["releases"])
            def change(widget, category=kind):
                selected = set(self.index.settings()["releases"])
                (selected.add if widget.get_active() else selected.discard)(category)
                save("releases", sorted(selected))
                self.maintenance.rescan()
            widget.connect("toggled", change)
            categories.append(widget)
        general.append(categories)
        general.append(button("Regroupements et exclusions", self.associations))
        general.append(button("Alias d’artiste pour une édition", self.comparison_aliases))
        general.append(button("Liste de lecture Strawberry", self.control.show_library))
        general.append(button("Rechercher les pochettes manquantes", lambda b: self.control.search_missing(
            b, "--pochettes-manquantes", "Pochettes manquantes")))
        general.append(button("Rechercher les paroles manquantes", lambda b: self.control.search_missing(
            b, "--paroles-manquantes", "Paroles manquantes")))
        if self.control.music_sync.available():
            general.append(button("Synchroniser la bibliothèque sur USB", self.control.music_sync.open_settings))
        stack.add_titled(scroller(general), "general", "Maintenance")
        resources = new()
        for spec in (
            ("idle", "Inactivité Sway (secondes)", 30, 3600), ("cpu", "Charge CPU avant départ (%)", 1, 95),
            ("memory_mib", "Mémoire disponible au départ (Mio)", 512, 65536, 256),
            ("running_memory_mib", "Réserve mémoire pendant un traitement (Mio)", 512, 16384, 128),
            ("temperature", "Température maximale (°C)", 45, 95),
            ("gpu", "Charge GPU avant départ (%)", 0, 95), ("disk_busy", "Activité disque avant départ (%)", 1, 95),
            ("network_timeout", "Durée maximale par recherche classique (secondes)", 15, 300),
            ("network_workers", "Maximum de recherches indépendantes en parallèle", 1, 20),
        ):
            number(resources, *spec)
        resources.append(Gtk.Label(label="Un traitement de maintenance à la fois.", xalign=0, css_classes=["muted"]))
        stack.add_titled(scroller(resources), "resources", "Ressources")
        night = new()
        self.night_summary = Gtk.Label(xalign=0, wrap=True, selectable=True, css_classes=["muted"])
        night.append(self.night_summary)
        self.refresh_night_summary()
        toggles(night, (("night_enabled", "Activer la recherche nocturne des LRC manquants"),
                        ("night_screen_off", "Rechercher les LRC lorsque l’écran est éteint"),
                        ("night_lid", "Rechercher les LRC lorsque le capot est fermé"),
                        ("night_lid_qualified", "Refroidissement capot fermé vérifié sur cet ordinateur")))
        for spec in (
            ("night_idle", "Inactivité minimale (secondes)", 30, 3600),
            ("night_cpu_temperature", "Limite CPU (°C)", 45, 95),
            ("night_gpu_temperature", "Limite GPU (°C)", 45, 95),
            ("night_closed_temperature", "Limite capot fermé (°C)", 45, 95),
            ("night_minutes", "Maintien éveillé (minutes, 0 = illimité sur secteur)", 0, 720),
        ):
            number(night, *spec)
        night.append(Gtk.Label(label="Secteur obligatoire. Attendez l’inhibiteur de capot actif avant de fermer.",
                               xalign=0, wrap=True, css_classes=["muted"]))
        night.append(Gtk.Label(label="Seuls les LRC d’origine fournis par les sources sont enregistrés.",
                               xalign=0, wrap=True, css_classes=["muted"]))
        stack.add_titled(scroller(night), "night", "LRC nocturnes")
        codex = new()
        toggles(codex, (("codex", "Autoriser l’authentification ChatGPT existante dans l’espace isolé"),
                        ("codex_auto", "Rechercher automatiquement en dernier recours")))
        for spec in (("codex_daily", "Limite locale quotidienne", 1, 20),
                     ("codex_timeout", "Durée maximale (secondes)", 15, 600)):
            number(codex, *spec)
        model = Gtk.Entry(placeholder_text="Modèle par défaut de Codex CLI", text=config["codex_model"], max_length=80)
        model.connect("changed", lambda w: save("codex_model", w.get_text()))
        codex.append(model)
        efforts = ["low", "medium", "high"]
        effort = Gtk.DropDown.new_from_strings(efforts)
        effort.set_selected(efforts.index(config["codex_effort"]) if config["codex_effort"] in efforts else 0)
        effort.connect("notify::selected", lambda w, _: save("codex_effort", efforts[w.get_selected()]))
        codex.append(effort)
        codex.append(Gtk.Label(label="Quota distant restant indisponible. Les recherches utilisent le compte ChatGPT.",
                              xalign=0, wrap=True, css_classes=["muted"]))
        kinds = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, max_children_per_line=4)
        for kind, title in (("lyrics", "Paroles"), ("lrc", "LRC"), ("album", "Albums"),
                            ("discography", "Discographies"), ("cover", "Pochettes"), ("metadata", "Métadonnées"),
                            ("track", "Pistes"), ("identify", "Identification"), ("import_failure", "Échecs d’import")):
            widget = Gtk.CheckButton(label=title, active=kind in config["codex_categories"])
            def toggle_kind(control, category=kind):
                selection = set(self.index.settings()["codex_categories"])
                (selection.add if control.get_active() else selection.discard)(category)
                save("codex_categories", sorted(selection))
            widget.connect("toggled", toggle_kind)
            kinds.append(widget)
        codex.append(kinds)
        self.codex_controls(codex)
        stack.add_titled(scroller(codex), "codex", "Codex")
        source = new()
        self.source_controls(source)
        stack.add_titled(scroller(source), "sources", "Sources")
        lyrics_sources = new()
        self.lyrics_source_controls(lyrics_sources)
        stack.add_titled(scroller(lyrics_sources), "lyrics_sources", "Sources de paroles")
        storage = new()
        number(storage, "reserve_gib", "Réserve libre par partition (Gio)", 20, 150)
        self.storage_controls(storage)
        stack.add_titled(scroller(storage), "storage", "Stockage")
        stack.set_visible_child_name(page)
        window.present()

    def associations(self, widget):
        with self.index.db() as db:
            associations = db.execute("SELECT * FROM associations ORDER BY new_artist,album").fetchall()
            exclusions = db.execute("SELECT * FROM exclusions ORDER BY kind,subject").fetchall()
            aliases = db.execute("SELECT * FROM canonical_aliases ORDER BY album,first_key").fetchall()
        lines = [f"{r['old_artist']} → {r['new_artist']} · {r['album']} · {r['provenance']}" for r in associations]
        lines += [f"{r['first_name']} / {r['second_name']} · {r['album']} · alias de comparaison" for r in aliases]
        lines += [f"Exclusion · {r['kind']} · {r['subject']}" for r in exclusions]
        show_details(widget, "Regroupements validés et exclusions", "\n".join(lines))

    def artist_groups(self, widget):
        root = self.index.root
        artists = sorted((p.name for p in root.iterdir() if p.is_dir() and not p.is_symlink()
                          and p.name not in ("A_trier", "YouTube") and not p.name.startswith(".")), key=str.casefold)
        names = Gio.ListStore(item_type=Row)
        names.splice(0, 0, [Row(name) for name in artists])
        selection = Gtk.MultiSelection.new(names)
        window = Gtk.Window(title="Regrouper les artistes", transient_for=self.control, modal=True,
                            destroy_with_parent=True, default_width=790, default_height=575)
        area = box(css_classes=["panel-content"])
        pane = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL, position=230, vexpand=True, wide_handle=True)
        view = Gtk.ListView(model=selection, factory=factory(lambda label, row: label.set_label(row.data)))
        pane.set_start_child(scroller(view, min_content_width=200))
        file_list = Gtk.TextView(editable=False, cursor_visible=False, monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        pane.set_end_child(scroller(file_list, min_content_width=380))
        area.append(Gtk.Label(label="Sélectionne les artistes à regrouper", xalign=0))
        area.append(pane)
        target = Gtk.DropDown.new_from_strings([])
        line = box(Gtk.Orientation.HORIZONTAL)
        line.append(Gtk.Label(label="Nom à conserver"))
        line.append(target)
        area.append(line)
        actions = box(Gtk.Orientation.HORIZONTAL)
        status = Gtk.Label(wrap=True, xalign=0)
        state = self.maintenance.state_root or STATE
        prior = last_group(self.index)
        current_plan = None
        revision = 0

        def selected():
            bits = selection.get_selection()
            return [artists[bits.get_nth(i)] for i in range(bits.get_size())]

        def chosen():
            item = target.get_selected_item()
            return item.get_string() if item else ""

        def reset(*_args):
            nonlocal current_plan, revision
            revision += 1
            current_plan = None
            accept.set_sensitive(False)
            file_list.get_buffer().set_text("")
            preview_button.set_sensitive(len(selected()) >= 2 and bool(chosen()))

        def selection_changed(*_args):
            previous = chosen()
            values = selected()
            target.set_model(Gtk.StringList.new(values))
            target.set_selected(values.index(previous) if previous in values else 0)
            reset()

        def complete(future, generation):
            nonlocal current_plan
            if generation != revision:
                return GLib.SOURCE_REMOVE
            try:
                current_plan = future.result()
            except (OSError, ValueError) as error:
                status.set_text(str(error))
            else:
                lines = []
                for row in current_plan["moves"]:
                    flag = " · collision, édition séparée" if row["collision"] else ""
                    lines.append(f"{row['source']} → {row['destination']}{flag}")
                    lines.extend(f"    {name}" for name in row["file_names"])
                file_list.get_buffer().set_text("\n".join(lines))
                status.set_text(f"{len(current_plan['moves'])} dossier(s) d’édition à déplacer.")
                accept.set_sensitive(True)
            return GLib.SOURCE_REMOVE

        def inspect(_widget):
            reset()
            generation = revision
            status.set_text("Vérification des éditions…")
            future = self.control.tag_reader.submit(preview_group, root, chosen(), selected())
            future.add_done_callback(lambda result: GLib.idle_add(complete, result, generation))

        def finish(future, plan, reverse):
            try:
                identity = future.result()
            except (OSError, sqlite3.Error, ValueError) as error:
                status.set_text(str(error))
                return GLib.SOURCE_REMOVE
            message = ("Regroupement annulé" if reverse else "Artistes regroupés")
            amount = len(plan["moves"])
            collisions = sum(row["collision"] for row in plan["moves"])
            self.maintenance.journal.emit("ALBUM", f"{message} · {amount} éditions · {collisions} collisions · {identity}")
            for row in plan["moves"]:
                self.maintenance.journal.emit("ALBUM", f"{row['source']} → {row['destination']}")
            self.control.artist_group_changed(plan, reverse=reverse)
            status.set_text(f"{message} · {amount} éditions.")
            accept.set_sensitive(False)
            restore.set_sensitive(False)
            return GLib.SOURCE_REMOVE

        def run(plan, identity=None):
            if not plan or self.control.maintenance_busy() or self.control.album_import.running:
                status.set_text("Attends la fin de l’activité Kara et des imports actifs.")
                return
            accept.set_sensitive(False)
            restore.set_sensitive(False)
            status.set_text("Mise à jour de la bibliothèque…")
            def change():
                queue = self.maintenance.import_queue
                previous = (undo_group(self.index, identity, queue, state=state) if identity else
                            group_artists(self.index, plan, queue, state=state))
                return previous
            future = self.control.tag_reader.submit(change)
            future.add_done_callback(lambda result: GLib.idle_add(finish, result, plan, bool(identity)))

        def confirm_undo(_widget):
            if prior is None:
                return
            dialog = Gtk.MessageDialog(transient_for=window, modal=True, buttons=Gtk.ButtonsType.OK_CANCEL,
                                       text=f"Rétablir les dossiers de {len(prior[1]['moves'])} éditions ?")
            def respond(widget, response):
                widget.destroy()
                if response == Gtk.ResponseType.OK:
                    run(prior[1], prior[0])
            dialog.connect("response", respond)
            dialog.present()

        preview_button = button("Prévisualiser", inspect, sensitive=False)
        accept = button("Confirmer le regroupement", lambda _: run(current_plan), sensitive=False)
        restore = button("Annuler le dernier regroupement", confirm_undo, sensitive=prior is not None)
        for action in (preview_button, accept, restore):
            actions.append(action)
        selection.connect("selection-changed", selection_changed)
        target.connect("notify::selected", reset)
        area.append(actions)
        area.append(status)
        window.set_child(area)
        window.present()

    def comparison_aliases(self, widget):
        window = Gtk.Window(title="Alias d’artiste pour une édition", transient_for=widget.get_root(),
                            modal=True, destroy_with_parent=True, default_width=490)
        fields = [Gtk.Entry(placeholder_text=label, max_length=180) for label in
                  ("Album", "Première graphie de l’artiste", "Autre graphie de l’artiste")]
        area = box(css_classes=["panel-content"])
        for entry in fields:
            area.append(entry)
        status = Gtk.Label(wrap=True, xalign=0)
        actions = box(Gtk.Orientation.HORIZONTAL)

        def change(_button, enabled):
            try:
                set_alias(self.index, *(entry.get_text() for entry in fields), enabled=enabled)
            except (sqlite3.Error, ValueError) as error:
                status.set_text(str(error))
            else:
                status.set_text("Alias de comparaison enregistré." if enabled else "Association retirée.")
                self.maintenance.journal.emit("ALBUM", status.get_text())

        actions.append(button("Enregistrer l’alias", lambda w: change(w, True)))
        actions.append(button("Retirer l’alias", lambda w: change(w, False)))
        area.append(actions)
        area.append(status)
        window.set_child(area)
        window.present()

    def codex_controls(self, area):
        store = Gio.ListStore(item_type=Row)
        selection = Gtk.SingleSelection(model=store)
        view = Gtk.ListView(model=selection, factory=factory(
            lambda widget, row: widget.set_text(self.codex_label(row.data))))
        area.append(scroller(view, min_content_height=170, vexpand=True))
        actions = box(Gtk.Orientation.HORIZONTAL)
        area.append(actions)

        def refresh(_=None):
            with self.index.db() as db:
                rows = db.execute("SELECT * FROM tasks WHERE kind='codex' ORDER BY updated DESC LIMIT 150").fetchall()
            store.splice(0, store.get_n_items(), [Row(row) for row in rows])

        def action(widget, change):
            row = selection.get_selected_item()
            if not row:
                return
            task = row.data
            identity = task["subject"]
            if change == "cancel":
                self.maintenance.cancel("codex", identity)
            elif change == "patch":
                self.maintenance.queue_import_patch(task)
            elif change == "retry":
                payload = dict(json.loads(task["payload"]), manual=True)
                self.index.enqueue("codex", identity, task["revision"], 15, payload, force=True)
                self.maintenance.wake.set()
            else:
                with self.index.db() as db:
                    result = db.execute("SELECT value FROM reviews WHERE kind='codex' AND subject=?", (identity,)).fetchone()
                report = json.loads(result[0]) if result else {"état": task["state"], "diagnostic": task["detail"]}
                show_details(widget, "Résultat de la recherche", json.dumps(report, ensure_ascii=False, indent=2))
            refresh()

        for label, handler in (("Actualiser", refresh), ("Détails", lambda w: action(w, "details")),
                               ("Annuler", lambda w: action(w, "cancel")), ("Relancer", lambda w: action(w, "retry")),
                               ("Ébaucher un correctif", lambda w: action(w, "patch"))):
            actions.append(button(label, handler))
        refresh()

    @staticmethod
    def codex_label(row):
        request = json.loads(row["payload"])
        return f"{request['kind']} · {request['artist']} {request['title'] or request['album']} · {row['state']}"

    def lyrics_source_controls(self, area):
        store = Gio.ListStore(item_type=Row)
        selection = Gtk.SingleSelection(model=store)
        states = {"ready": "Disponible", "limited": "Temporairement indisponible",
                  "paused": "Quota local en pause", "disabled": "Désactivé",
                  "excluded": "Exclu", "pending": "À vérifier", "upstream": "Source amont indisponible"}

        def display(widget, row):
            value = row.data
            stats = value["lyrics_stats"]
            rate = f"{100 * value['successes'] / value['attempts']:.0f} %" if value["attempts"] else "—"
            available = states.get(value["availability"], value["availability"])
            if value["next_available"]:
                available += " jusqu’au " + datetime.fromtimestamp(value["next_available"]).strftime("%d/%m %H:%M")
            quota = value["remote_quota"]
            extra = (" · découverte attribuée" if value["id"] == "Unison" else
                     " · agrégateur" if value["id"] == "lyrics.ovh" else "")
            widget.set_text(
                f"{value['name']}{extra} · {available} · {value['used_today']} requêtes HTTP aujourd’hui\n"
                f"Quota local restant {value['remaining']} · quota distant "
                f"{quota if quota is not None else 'non communiqué'} · "
                f"{stats.get('plain', 0)} textes simples · {stats.get('lrc', 0)} LRC · Réussite {rate}\n"
                f"Dernière erreur {value['error'] or '—'}"
            )
        view = Gtk.ListView(model=selection, factory=factory(
            display, lambda: Gtk.Label(xalign=0, wrap=True, margin_top=5, margin_bottom=5)))
        area.append(scroller(view, min_content_height=280, vexpand=True))
        actions = box(Gtk.Orientation.HORIZONTAL, 4)
        area.append(actions)
        status = Gtk.Label(xalign=0, wrap=True)
        area.append(status)

        def refresh(_=None):
            selected = selection.get_selected_item()
            identity = selected.data["id"] if selected else None
            try:
                rows = lyrics_rows(self.index)
            except (sqlite3.Error, ValueError) as error:
                status.set_text(str(error))
                return
            store.splice(0, store.get_n_items(), [Row(row) for row in rows])
            if identity:
                for i, row in enumerate(rows):
                    if row["id"] == identity:
                        selection.set_selected(i)
                        break

        def action(widget, mode):
            selected = selection.get_selected_item()
            if not selected:
                return
            row = selected.data
            identity = row["id"]
            if mode in (-1, 1):
                move_lyrics_source(self.index, identity, mode)
            elif mode == "limits":
                self.source_rate_dialog(row, row["decoded"])
                return
            elif mode == "test":
                status.set_text(f"Test de {identity} en cours…")
                widget.set_sensitive(False)
                try:
                    # Use the normal subprocess entry point and shared quota;
                    # GTK never waits for the remote endpoint.
                    child = Gio.Subprocess.new(
                        [sys.executable, "-B", str(BIN / "_parole_work.py"), "lyrics_source_test", identity,
                         str(self.index.root)],
                        Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
                except GLib.Error as error:
                    status.set_text(error.message)
                    widget.set_sensitive(True)
                    return

                def complete(process, result):
                    widget.set_sensitive(True)
                    try:
                        _, stdout, _ = process.communicate_utf8_finish(result)
                        value = json.loads(stdout)
                        labels = {"found": "Correspondance vérifiée", "candidate": "Lien attribué",
                                  "not_found": "Enregistrement de test introuvable", "reachable": "Service joignable",
                                  "temporary": "Échec temporaire", "ambiguous": "Correspondance ambiguë"}
                        detail = labels.get(value["state"], states.get(value["state"], value["state"]))
                        if format_name := value.get("format"):
                            detail += f" ({format_name})"
                        if value.get("detail"):
                            detail += f" · {value['detail']}"
                        status.set_text(f"{identity} · {detail}")
                    except (GLib.Error, TypeError, ValueError, KeyError):
                        status.set_text(f"{identity} · test interrompu")
                    refresh()

                child.communicate_utf8_async(None, None, complete)
                return
            else:
                controls(self.index, identity, mode)
                self.maintenance.wake.set()
            refresh()

        for label, mode in (("Monter", -1), ("Descendre", 1), ("Activer", "active"),
                            ("Désactiver", "disabled"), ("Limites…", "limits"), ("Tester", "test")):
            actions.append(button(label, lambda widget, selected=mode: action(widget, selected)))
        actions.append(button("Actualiser", refresh))
        refresh()

    def source_controls(self, area):
        store = Gio.ListStore(item_type=Row)
        selection = Gtk.SingleSelection(model=store)

        def display(widget, row):
            source = row.data
            widget.set_text(f"{source['name']} · {source['category']} · {source['state']}  · "
                            f"{source['successes']}/{source['attempts']} correspondances")
        view = Gtk.ListView(model=selection, factory=factory(display))
        area.append(scroller(view, min_content_height=200, vexpand=True))
        actions = box(Gtk.Orientation.HORIZONTAL, 4)
        area.append(actions)

        def refresh(_=None):
            with self.index.db() as db:
                rows = db.execute("SELECT * FROM sources ORDER BY state,name").fetchall()
            store.splice(0, store.get_n_items(), [Row(row) for row in rows])

        def action(widget, mode):
            row = selection.get_selected_item()
            if not row:
                return
            source = row.data
            identity = source["id"]
            config = json.loads(source["config"])
            if mode == "details":
                from _parole_sources import recent_source

                with self.index.db() as db:
                    history = [dict(r) for r in db.execute("SELECT * FROM source_history WHERE source=? ORDER BY stamp DESC LIMIT 30",
                                                           (identity,))]
                try:
                    metrics = import_metrics(self.index).get(identity, {})
                    recent = recent_source(self.index, identity)
                except (sqlite3.Error, ValueError):
                    metrics = {}
                    recent = None
                stats = {}
                for name, label, unit, scale in (
                    (LOOKUP, "Recherche", "s", 1), (STARTUP, "Début du transfert", "s", 1),
                    (THROUGHPUT, "Débit", "Mio/s", 1 / 1024**2), (TOTAL, "Durée de l’import", "s", 1),
                    (SUCCEEDED, "Réussite de la récupération", "%", 100),
                ):
                    if value := metrics.get(name):
                        stats[label] = f"{value['mean'] * scale:.2f} {unit} · {value['samples']} observation(s)"
                if value := metrics.get(SUCCEEDED):
                    stats["Échecs de récupération"] = value["samples"] - int(value["total"])
                if "limited_until" in metrics:
                    expiry = datetime.fromtimestamp(metrics["limited_until"]).strftime("%d/%m %H:%M")
                    status = metrics["last_http"]
                    signal = "code API 6" if status == 6 else f"HTTP {status}"
                    stats["Limite temporaire"] = f"{signal} · jusqu’à {expiry}"
                show_details(widget, source["name"], json.dumps({**source, "config": config, "historique": history,
                                                                  "observations récentes": recent,
                                                                  "mesures d’import": stats},
                                                                ensure_ascii=False, indent=2))
            elif mode == "check":
                self.source_validation(source, config)
            elif mode == "limits":
                self.source_rate_dialog(source, config)
            elif mode == "adapter":
                if self.maintenance.queue_adapter(source):
                    self.settings("codex")
                elif not self.index.settings()["codex"]:
                    self.settings("codex")
            else:
                controls(self.index, identity, mode)
                self.maintenance.revision += 1
                refresh()

        for label, mode in (
            ("Détails", "details"), ("Activer", "active"), ("Désactiver", "disabled"),
            ("Vérifier…", "check"), ("Réinit. statistiques", "reset"), ("Exclure", "excluded"),
            ("Ébaucher l’adaptateur", "adapter"),
        ):
            actions.append(button(label, lambda w, m=mode: action(w, m)))
        area.append(button("Actualiser", refresh))
        area.append(button("Limites du fournisseur sélectionné…", lambda w: action(w, "limits")))

        query = Gtk.Entry(placeholder_text="Artiste, titre ou album", max_length=180, hexpand=True)
        discovery = box(Gtk.Orientation.HORIZONTAL, 4)
        discovery.append(query)

        def discover_providers(widget):
            try:
                if self.maintenance.queue_provider_discovery(query.get_text()):
                    self.settings("codex")
                else:
                    show_details(widget, "Découverte de fournisseurs", "Active Codex dans les paramètres pour lancer la recherche.")
            except (ValueError, OSError, sqlite3.Error) as error:
                show_details(widget, "Découverte de fournisseurs", str(error))

        query.connect("activate", discover_providers)
        discovery.append(button("Chercher des téléchargements avec Codex", discover_providers))
        area.append(discovery)
        refresh()

    def source_rate_dialog(self, source, config):
        daily, seconds = provider_limits(config)
        window = Gtk.Window(title=f"Limites · {source['name']}", transient_for=self.settings_dialog,
                            modal=True, destroy_with_parent=True, default_width=520)
        panel = box(css_classes=["panel-content"])
        daily_spin = Gtk.SpinButton.new_with_range(0, 2000, 1)
        seconds_spin = Gtk.SpinButton.new_with_range(0, 300, 1)
        for label, spin, value in (("Requêtes HTTP de maintenance par jour", daily_spin, daily),
                                   ("Intervalle minimal (secondes)", seconds_spin, seconds)):
            line = box(Gtk.Orientation.HORIZONTAL)
            line.append(Gtk.Label(label=label, xalign=0, hexpand=True))
            spin.set_value(value)
            line.append(spin)
            panel.append(line)
        panel.append(Gtk.Label(label="Un quota de 0 met les requêtes en pause. Le délai du fournisseur reste prioritaire.",
                               xalign=0, wrap=True, css_classes=["muted"]))

        def save(_):
            configure_limits(self.index, source["id"], daily_spin.get_value_as_int(), seconds_spin.get_value_as_int())
            self.maintenance.wake.set()
            window.close()

        panel.append(button("Enregistrer", save))
        window.set_child(panel)
        window.present()

    def source_validation(self, source, config):
        title = source["name"]
        parent = self.settings_dialog
        if source["adapter"] != "lrclib":
            if config.get("docs"):
                self.index.enqueue("source_probe", source["id"], encoded_key(source["id"]), 12, force=True)
                self.maintenance.wake.set()
            show_details(parent.get_child(), title,
                         "Source intégrée. Les statistiques proviennent des recherches réelles." if source["adapter"] == "builtin" else
                         "Un adaptateur exécutable doit être examiné avant sa mise en service. La proposition reste dans le résultat Codex associé.")
            return
        window = Gtk.Window(title=f"Vérifier {title}", transient_for=parent, modal=True,
                            destroy_with_parent=True, default_width=630, default_height=420)
        area = box(css_classes=["panel-content"])
        window.set_child(area)
        area.append(Gtk.Label(label="Configuration JSON avec host, path, terms et fixtures", xalign=0))
        sample = {"host": config["host"], "path": config.get("path", "/api/search"),
                  "terms": config.get("terms", ""), "fixtures": config.get("fixtures", [])}
        view = Gtk.TextView(monospace=True, wrap_mode=Gtk.WrapMode.WORD_CHAR)
        view.get_buffer().set_text(json.dumps(sample, indent=2, ensure_ascii=False))
        area.append(scroller(view, vexpand=True))
        consent = Gtk.CheckButton(label="J’ai vérifié que les conditions autorisent les requêtes automatisées.")
        area.append(consent)
        status = Gtk.Label(xalign=0, wrap=True)
        area.append(status)

        def confirm(_):
            buffer = view.get_buffer()
            try:
                raw = buffer.get_text(*buffer.get_bounds(), False)
                if not consent.get_active() or len(raw) > 12_000:
                    raise ValueError("Autorisation ou configuration invalide")
                payload = json.loads(raw)
                if set(payload) != {"host", "path", "terms", "fixtures"} or payload["host"] != config["host"]:
                    raise ValueError("Configuration différente du fournisseur")
                self.index.enqueue("source", source["id"], encoded_key(payload), 12, payload, force=True)
                self.maintenance.wake.set()
                status.set_text("Vérification mise en file")
            except (TypeError, ValueError, sqlite3.Error) as error:
                status.set_text(str(error))

        area.append(button("Vérifier les correspondances", confirm))
        window.present()

    def storage_controls(self, area):
        label = Gtk.Label(xalign=0, wrap=True, selectable=True)
        area.append(label)

        def refresh(_=None):
            storage = self.maintenance.resources.storage
            if not storage:
                label.set_text("Mesure en attente")
                return
            label.set_text("\n".join(f"Libre · {name} · {display_size(size)}"
                                     for name, size in storage["free"].items()))

        def cleanup(_):
            self.index.enqueue("cleanup", "workspace", "stage-v1", 12, force=True)
            self.maintenance.wake.set()
            self.maintenance.journal.emit("CACHE", "Nettoyage des brouillons demandé")

        area.append(button("Nettoyer les fichiers intermédiaires interrompus", cleanup))
        area.append(button("Actualiser", refresh))
        refresh()
