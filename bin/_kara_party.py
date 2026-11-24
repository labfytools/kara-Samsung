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
ORIGINAL_VOICE_LEVELS = (0, 20, 50, 80)


def score_color(score):
    if score is None:
        return "#a6adc8"
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
    duration: float | None = None


@dataclass(eq=False)
class Singer:
    name: str
    song: Song | None = None
    score: int | None = None
    done: bool = False
    result: dict | None = None
    mobile_id: str | None = None  # Identifiant de session éphémère, jamais persisté.
    music_volume: int | None = None
    training: bool = False
    original_voice: int = 0


def catalogue(database, library):
    paths = set()
    songs = []
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2) as db:
        for url, title, artist, album, duration in db.execute(
            "SELECT url, title, artist, album, length FROM songs WHERE unavailable = 0 ORDER BY title COLLATE NOCASE"
        ):
            uri = urlsplit(url)
            if uri.scheme != "file" or uri.netloc not in ("", "localhost"):
                continue
            path = Path(unquote(uri.path)).resolve()
            if path in paths or not path.is_relative_to(library) or not path.is_file():
                continue
            paths.add(path)
            song = Song(path, title or path.stem, artist or "", album or "",
                        duration / 1_000_000_000 if isinstance(duration, (int, float)) and duration > 0 else None)
            songs.append((song, search_key(" ".join((song.title, song.artist, song.album)))))
    return songs


class Party:
    def __init__(self, path):
        self.path = path
        self.players = []
        self.active = None
        self.excluded = set()
        self.unscored = set()
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
        return next((player for player in self.players
                     if player.song and player.song.path not in self.excluded and not player.done), None)

    def board(self):
        return [
            {"name": player.name, "song": player.song.title if player.song else "",
             "score": player.score, "active": player is self.active,
             "done": player.done, "mobile": player.mobile_id is not None,
             "coverage": ((player.result or {}).get("summary") or {}).get("coverage")}
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
        self.validate = Gtk.Button(label="Préparer la partie", css_classes=["suggested-action"])
        self.validate.connect("clicked", controller.validate_party)
        controls.append(self.validate)
        self.skip = Gtk.Button(label="Écarter les morceaux en échec", visible=False)
        self.skip.connect("clicked", controller.skip_failed)
        controls.append(self.skip)
        self.free = Gtk.Button(label="Autoriser le chant sans note", visible=False)
        self.free.connect("clicked", controller.allow_unscored)
        controls.append(self.free)
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
        if self.controller.remote_party.locked:
            return
        player = self.party.next_player()
        if player is not None:
            self.controller.start_turn(player)

    def refresh(self):
        editing = (self.party.active is None and not self.controller.validating
                   and not self.controller.remote_party.locked)
        ready = editing and self.controller.party_ready
        self.record_next.set_sensitive(editing)
        self.name.set_sensitive(editing)
        self.add_button.set_sensitive(editing)
        self.validate.set_sensitive(editing and not ready and bool(self.party.players)
                                    and all(player.song is not None for player in self.party.players))
        self.skip.set_visible(editing and self.controller.can_skip_failed())
        self.free.set_visible(editing and bool(self.controller.unscored_choices()))
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
            if song and song.path in self.party.excluded:
                detail += " · Écarté"
            elif song and song.path in self.party.unscored:
                detail += " · Chant sans note"
            elif song:
                status = self.controller.preparation_status(song.path)
                if status:
                    detail += f" · {status}"
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
            sing = Gtk.Button(label="Chanter sans note" if song and song.path in self.party.unscored else "Chanter")
            sing.set_sensitive(ready and song is not None and song.path not in self.party.excluded)
            sing.connect("clicked", lambda _button, singer=player: self.controller.start_turn(singer))
            row.append(sing)
            if player.result:
                details = Gtk.Button(label="Détail")
                details.connect("clicked", self.show_result, player)
                row.append(details)
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
            points = "—" if player.score is None else f"{player.score} %"
            summary = (player.result or {}).get("summary")
            coverage = f" · {summary['coverage']} % évalués" if summary else ""
            label.set_markup(f'<span weight="bold" foreground="{score_color(player.score)}">{points}</span>{coverage}')
            label.set_tooltip_text(f"{summary['verified']}/{summary['total']} mots évalués" if summary else None)

    def show_result(self, _button, player):
        result = player.result
        summary = result.get("summary")
        if summary is None:
            return
        detail = Gtk.Window(title=f"Résultat · {player.name}", transient_for=self,
                            default_width=580, default_height=450, destroy_with_parent=True)
        scroll = Gtk.ScrolledWindow()
        detail.set_child(scroll)
        view = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.WORD,
                            left_margin=16, right_margin=16, top_margin=12)
        scroll.set_child(view)
        timing = f"{summary['timing']} %" if summary["timing"] is not None else "—"
        score = "Score indisponible" if summary["score"] is None else f"Score {summary['score']} %"
        lyrics = f"{summary['lyrics']} %" if summary["verified"] else "—"
        text = [f"{score} · Paroles {lyrics} · Timing {timing}",
                f"Couverture du tour {summary['coverage']} % · {summary['verified']}/{summary['total']} mots évalués",
                f"Référence {summary['reference_coverage']} % · {summary['eligible']}/{summary['total']} mots évaluables",
                f"{summary['correct']} corrects · {summary['incorrect']} incorrects",
                f"{summary['unevaluable']} non évalués au total ({summary['unreliable_reference']} sans repère, "
                f"{summary['uncertain']} incertains ou fenêtre écartée, {summary['not_assessed']} non traités)",
                f"{summary['extras']} supplémentaires reconnus"]
        labels = {"correct": "correct", "early": "en avance", "late": "en retard",
                  "missing": "manquant", "incorrect": "incorrect", "extra": "en plus", "uncertain": "incertain"}
        reasons = {"reference": "repère indisponible", "confidence": "confiance faible",
                   "backing": "accompagnement dominant", "silence": "signal faible",
                   "unverified": "source non vérifiée", "capture": "capture incomplète", "decoder": "échec de décodage"}
        for index, line in sorted(result["lines"].items()):
            text.append(f"\nLigne {index + 1}")
            for word in line:
                label = word["expected"] or word.get("heard", "")
                status = labels[word["status"]]
                if "heard" in word and word["expected"] and (word["status"] == "incorrect" or word["status"] == "uncertain"):
                    status += f" ({word['heard']})"
                start = word.get("expected_start")
                if start is not None:
                    status += f" · attendu {start:.2f} s"
                if "start" in word:
                    status += f" · reconnu {word['start']:.2f} s · confiance {word['confidence']:.2f}"
                if "delay" in word:
                    status += f" {word['delay']:+.2f} s"
                if word.get("reason") in reasons:
                    status += f" · {reasons[word['reason']]}"
                text.append(f"{label} · {status}")
        view.get_buffer().set_text("\n".join(text))
        detail.present()

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
                notice.set_text(f"{len(matches)} chansons · affine la recherche pour voir les autres.")
            elif matches:
                notice.set_text(f"{len(matches)} chanson(s)")
            else:
                notice.set_text("Aucune chanson ne correspond.")

        def select(_list, row):
            if self.controller.validating or self.party.active is not None:
                return
            song = shown[row.get_index()]
            player.song = song
            self.controller.party_ready = False
            player.score, player.done, player.result = None, False, None
            self.message.set_text(f"{song.title} sélectionné pour {player.name}")
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
