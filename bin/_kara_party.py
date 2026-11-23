"""Joueurs du karaoké et choix des morceaux locaux dans Strawberry."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
import sqlite3
from tempfile import NamedTemporaryFile
from unicodedata import normalize
from urllib.parse import unquote, urlsplit

from gi.repository import GLib, Gtk, Pango


SCORE_COLORS = ((243, 139, 168), (249, 226, 175), (166, 227, 161))


def score_color(score):
    start, end = SCORE_COLORS[:2] if score < 50 else SCORE_COLORS[1:]
    fraction = (score if score < 50 else score - 50) / 50
    return "#" + "".join(f"{round(a + (b - a) * fraction):02x}" for a, b in zip(start, end))


def search_key(text):
    return "".join(c for c in normalize("NFKD", text.casefold()) if not (0x300 <= ord(c) <= 0x36f))


@dataclass(frozen=True)
class Song:
    path: Path
    title: str
    artist: str
    album: str


@dataclass(eq=False)
class Singer:
    name: str
    song: Song | None = None
    score: int = 0
    done: bool = False
    mobile_id: str | None = None  # Identifiant de session éphémère, jamais persisté.


def catalogue(database, library):
    paths = set()
    songs = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        for url, title, artist, album in db.execute(
            "SELECT url, title, artist, album FROM songs WHERE unavailable = 0 ORDER BY title COLLATE NOCASE"
        ):
            uri = urlsplit(url)
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                continue
            path = Path(unquote(uri.path)).resolve()
            if path in paths or not path.is_relative_to(library) or not path.is_file() or not path.with_suffix(".lrc").is_file():
                continue
            paths.add(path)
            song = Song(path, title or path.stem, artist or "", album or "")
            songs.append((song, search_key(" ".join((song.title, song.artist, song.album)))))
    return songs


class Party:
    def __init__(self, path):
        self.path = path
        self.players = []
        self.active = None
        self.error = ""
        try:
            names = json.loads(path.read_text())
            if not isinstance(names, list) or any(not isinstance(name, str) or not name.strip() for name in names):
                raise ValueError("Liste de joueurs invalide")
        except FileNotFoundError:
            return
        except (OSError, ValueError):
            self.error = "Impossible de relire les joueurs enregistrés."
            return
        self.players = [Singer(name) for name in names]

    def save(self, players):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.path.parent, delete=False) as draft:
                temp = Path(draft.name)
                json.dump([player.name for player in players if player.mobile_id is None],
                          draft, ensure_ascii=False, indent=2)
                draft.write("\n")
            os.replace(temp, self.path)
        finally:
            if temp is not None:
                temp.unlink(missing_ok=True)
        self.players = players

    def add(self, name):
        name = " ".join(name.split())
        if not name:
            raise ValueError("Saisis le nom du joueur.")
        if any(player.name.casefold() == name.casefold() for player in self.players):
            raise ValueError("Ce nom est déjà enregistré.")
        self.save([*self.players, Singer(name)])

    def remove(self, player):
        if player is self.active:
            raise ValueError("Termine le tour en cours avant de supprimer ce joueur.")
        self.save([other for other in self.players if other is not player])

    def next_player(self):
        return next((player for player in self.players if player.song and not player.done), None)

    def board(self):
        return [
            {"name": player.name, "song": player.song.title if player.song else "",
             "score": player.score, "active": player is self.active,
             "done": player.done, "mobile": player.mobile_id is not None}
            for player in self.players
        ]


class PlayersWindow(Gtk.Window):
    def __init__(self, controller, database, library):
        super().__init__(title="Joueurs du karaoké", transient_for=controller, destroy_with_parent=True,
                         hide_on_close=True, default_width=640, default_height=480)
        self.controller = controller
        self.party = controller.party
        self.database = database
        self.library = library
        self.score_labels = {}
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                       margin_top=16, margin_bottom=16, margin_start=18, margin_end=18)
        self.set_child(root)
        registration = Gtk.Box(spacing=8)
        self.name = Gtk.Entry(placeholder_text="Nom du joueur", hexpand=True, max_length=60)
        self.name.connect("activate", self.add)
        registration.append(self.name)
        self.add_button = Gtk.Button(label="Ajouter")
        self.add_button.connect("clicked", self.add)
        registration.append(self.add_button)
        root.append(registration)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER,
                                    min_content_height=190)
        self.rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        scroll.set_child(self.rows)
        root.append(scroll)

        controls = Gtk.Box(spacing=8)
        self.validate = Gtk.Button(label="Valider la partie", css_classes=["suggested-action"])
        self.validate.connect("clicked", controller.validate_party)
        controls.append(self.validate)
        self.next = Gtk.Button(label="Tour suivant")
        self.next.connect("clicked", self.start_next)
        controls.append(self.next)
        self.finish = Gtk.Button(label="Terminer le tour")
        self.finish.connect("clicked", lambda *_: controller.finish_turn(pause=True))
        controls.append(self.finish)
        root.append(controls)
        self.record_next = Gtk.CheckButton(label="Enregistrer le prochain tour")
        self.record_next.set_tooltip_text("Garde le signal du micro sur ce PC pour le diagnostic")
        root.append(self.record_next)
        self.align_progress = Gtk.ProgressBar(show_text=True, visible=False, css_classes=["alignment-progress"])
        root.append(self.align_progress)
        self.message = Gtk.Label(xalign=0, wrap=True, css_classes=["lyrics-info"])
        root.append(self.message)
        self.refresh()
        self.message.set_text(self.party.error)

    def add(self, _widget):
        try:
            self.party.add(self.name.get_text())
        except (OSError, ValueError) as error:
            self.message.set_text(f"Ajout impossible · {error}")
            return
        self.controller.party_ready = False
        self.name.set_text("")
        self.message.set_text("")
        self.refresh()

    def remove(self, _button, player):
        if player.mobile_id is not None:
            self.controller.remove_mobile(player)
            return
        try:
            self.party.remove(player)
        except (OSError, ValueError) as error:
            self.message.set_text(f"Suppression impossible · {error}")
            return
        self.controller.party_ready = False
        self.message.set_text("")
        self.refresh()

    def start_next(self, _button):
        player = self.party.next_player()
        if player is not None:
            self.controller.start_turn(player)

    def refresh(self):
        editing = self.party.active is None and not self.controller.validating
        ready = editing and self.controller.party_ready
        self.record_next.set_sensitive(editing)
        self.name.set_sensitive(editing)
        self.add_button.set_sensitive(editing)
        self.validate.set_sensitive(editing and not ready and bool(self.party.players)
                                    and all(player.song is not None for player in self.party.players))
        while child := self.rows.get_first_child():
            self.rows.remove(child)
        self.score_labels.clear()
        if not self.party.players:
            self.rows.append(Gtk.Label(label="Ajoute les joueurs de la partie.", xalign=0,
                                       css_classes=["lyrics-info"]))
        for player in self.party.players:
            row = Gtk.Box(spacing=8, css_classes=["party-player"])
            if player is self.party.active:
                row.add_css_class("party-current")
            labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, hexpand=True)
            labels.append(Gtk.Label(label=("📱 " if player.mobile_id else "") + player.name,
                                    xalign=0, ellipsize=Pango.EllipsizeMode.END,
                                    css_classes=["party-name"]))
            song = player.song
            detail = " · ".join(part for part in (song.title, song.artist) if part) if song else "Choisis une chanson"
            labels.append(Gtk.Label(label=detail, tooltip_text=detail, xalign=0,
                                    ellipsize=Pango.EllipsizeMode.END, css_classes=["lyrics-info"]))
            row.append(labels)
            score = Gtk.Label(width_chars=6, xalign=1)
            self.score_labels[player] = score
            row.append(score)
            choose = Gtk.Button(label="Choisir")
            choose.set_sensitive(editing)
            choose.connect("clicked", self.choose_song, player)
            row.append(choose)
            sing = Gtk.Button(label="Chanter")
            sing.set_sensitive(ready and song is not None)
            sing.connect("clicked", lambda _button, singer=player: self.controller.start_turn(singer))
            row.append(sing)
            remove = Gtk.Button(icon_name="user-trash-symbolic", tooltip_text=f"Retirer {player.name}")
            remove.set_sensitive(editing)
            remove.connect("clicked", self.remove, player)
            row.append(remove)
            self.rows.append(row)
        self.next.set_sensitive(ready and self.party.next_player() is not None)
        self.finish.set_sensitive(self.party.active is not None)
        self.refresh_scores()

    def refresh_scores(self):
        for player, label in self.score_labels.items():
            label.set_markup(f'<span weight="bold" foreground="{score_color(player.score)}">{player.score} %</span>')

    def choose_song(self, _button, player):
        picker = Gtk.Window(title=f"Chanson pour {player.name}", transient_for=self, destroy_with_parent=True,
                            modal=True, default_width=560, default_height=480)
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                       margin_top=16, margin_bottom=16, margin_start=16, margin_end=16)
        picker.set_child(body)
        search = Gtk.SearchEntry(placeholder_text="Titre, artiste ou album")
        body.append(search)
        notice = Gtk.Label(label="Chargement de la collection…", xalign=0, wrap=True, css_classes=["lyrics-info"])
        body.append(notice)
        results = Gtk.ListBox(activate_on_single_click=True, selection_mode=Gtk.SelectionMode.BROWSE)
        scroll = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        scroll.set_child(results)
        body.append(scroll)
        songs = []
        shown = []

        def render(_entry=None):
            parts = search_key(search.get_text()).split()
            matches = [song for song, key in songs if all(part in key for part in parts)]
            while child := results.get_first_child():
                results.remove(child)
            shown[:] = matches[:100]
            for song in shown:
                summary = " · ".join(part for part in (song.title, song.artist, song.album) if part)
                results.append(Gtk.Label(label=summary, tooltip_text=summary, xalign=0,
                                         ellipsize=Pango.EllipsizeMode.END, margin_top=8, margin_bottom=8))
            if len(matches) > len(shown):
                notice.set_text(f"{len(matches)} chansons minutées · affine la recherche pour voir les autres.")
            elif matches:
                notice.set_text(f"{len(matches)} chanson(s) avec paroles minutées")
            else:
                notice.set_text("Aucune chanson avec paroles minutées ne correspond.")

        def select(_list, row):
            song = shown[row.get_index()]
            try:
                vocabulary = self.controller.prepare_song(song)
            except ValueError as error:
                notice.set_text(str(error))
                return
            player.song = song
            self.controller.party_ready = False
            player.score, player.done = 0, False
            self.message.set_text(f"Paroles chargées pour {player.name} · {len(vocabulary.lines)} repères")
            self.refresh()
            picker.close()

        def loaded(future):
            if not picker.get_visible():
                return GLib.SOURCE_REMOVE
            try:
                songs.extend(future.result())
            except (OSError, sqlite3.Error, ValueError) as error:
                notice.set_text(f"Collection Strawberry indisponible · {error}")
            else:
                search.connect("search-changed", render)
                render()
            return GLib.SOURCE_REMOVE

        results.connect("row-activated", select)
        picker.present()
        search.grab_focus()
        self.controller.tag_reader.submit(catalogue, self.database, self.library).add_done_callback(
            lambda future: GLib.idle_add(loaded, future)
        )
