"""BlackBox sessions and active capture in a separate Parole window."""
import json
import subprocess
from time import monotonic

import gi
gi.require_foreign("cairo")
from gi.repository import GLib, Gtk

from _kara_blackbox import MARKERS, session_folders, validate_recording
from _kara_blackbox_preview import BINS, read_json, tiles
from _kara_paths import BIN


def button(text, callback):
    result = Gtk.Button(label=text)
    result.connect("clicked", callback)
    return result


def box(orientation=Gtk.Orientation.VERTICAL):
    return Gtk.Box(orientation=orientation, spacing=8, margin_top=10, margin_bottom=10,
                   margin_start=10, margin_end=10)


def text_panel():
    view = Gtk.TextView(editable=False, cursor_visible=False, monospace=True,
                        wrap_mode=Gtk.WrapMode.WORD_CHAR, left_margin=8, right_margin=8)
    scroll = Gtk.ScrolledWindow(hexpand=True, vexpand=True, child=view, min_content_height=130)
    return scroll, view.get_buffer()


def fill(buffer, value):
    buffer.set_text(value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))


class BlackBoxStudio(Gtk.Window):
    def __init__(self, owner):
        super().__init__(title="BlackBox Studio", transient_for=owner, destroy_with_parent=True,
                         hide_on_close=True, default_width=930, default_height=660)
        self.owner = owner
        self.root = owner.blackbox.root
        self.path = None
        self.manifest = None
        self.paths = []
        self.turns = []
        self.preview = None
        self.recalculated = None
        self.serial = 0
        self.catalogue_serial = 0
        self.timer = 0
        self.inflight = 0
        self.pages = Gtk.Notebook()
        self.set_child(self.pages)
        self.capture_page()
        self.session_page()
        self.timeline_page()
        self.report_page()

    def show_studio(self):
        self.present()
        if not self.timer:
            self.timer = GLib.timeout_add_seconds(1, self.tick)
        self.tick()
        self.refresh()

    def submit(self, work, success, failure=None):
        # Use Parole's existing background executor. GTK owns widgets and
        # rejects stale replies after the user changes the selected session.
        if self.inflight >= 2:
            (failure or self.error.set_text)("Deux opérations BlackBox en cours.")
            return
        self.inflight += 1

        def settled(future):
            def render():
                self.inflight -= 1
                if self.get_visible():
                    try:
                        success(future.result())
                    except (OSError, RuntimeError, ValueError, KeyError, EOFError, subprocess.SubprocessError) as error:
                        (failure or self.error.set_text)(str(error))
                return GLib.SOURCE_REMOVE

            GLib.idle_add(render)

        self.owner.tag_reader.submit(work).add_done_callback(settled)

    def capture_page(self):
        page = box()
        self.capture = Gtk.Label(xalign=0, wrap=True)
        self.preflight = Gtk.Label(xalign=0, wrap=True, selectable=True)
        self.control = button("Configurer et démarrer", lambda *_: self.owner.toggle_blackbox(None, parent=self))
        self.markers = ("problem", "return", "echo", "display", "dropout", "music-volume", "mic-volume", "speech")
        self.marker = Gtk.DropDown(model=Gtk.StringList.new([MARKERS[key] for key in self.markers]))
        self.note = Gtk.Entry(placeholder_text="Commentaire facultatif", max_length=240, hexpand=True)
        self.mark_button = button("Marquer ce moment", self.mark)
        for child in (self.capture, self.control, self.preflight, self.marker, self.note, self.mark_button):
            page.append(child)
        self.pages.append_page(page, Gtk.Label(label="Capture"))

    def tick(self):
        if not self.get_visible():
            self.timer = 0
            return GLib.SOURCE_REMOVE
        status = self.owner.blackbox.status(storage=True)
        state = {"deep_analysis": "Deep Analysis", "diagnostic_light": "Diagnostic léger",
                 "calibration": "Calibration", "benchmark": "Benchmark", "off": "Désactivée"}[status["mode"]]
        if status["finalizing"]:
            state = "Finalisation"
        self.capture.set_text(f"{state} · {status['elapsed_ns'] / 1e9:.0f} s · "
                              f"{status['bytes_written'] / 1024 ** 2:.1f} Mio · {status['dropped']} événements perdus" +
                              (f"\n{status['error']}" if status["error"] else ""))
        self.control.set_label("Arrêter la capture" if status["active"] else "Configurer et démarrer")
        self.control.set_sensitive(not status["finalizing"])
        self.mark_button.set_sensitive(status["active"])
        self.preflight.set_text("\n".join(f"{label} · {value}" for label, value in self.owner.blackbox_preflight().items()))
        return GLib.SOURCE_CONTINUE

    def mark(self, *_):
        position = self.owner.base_position + (monotonic() - self.owner.updated) * self.owner.rate
        self.owner.blackbox.mark(self.markers[self.marker.get_selected()], self.note.get_text(),
                                 position=position)
        self.note.set_text("")

    def session_page(self):
        page = box()
        toolbar = box(Gtk.Orientation.HORIZONTAL)
        self.sessions = Gtk.DropDown(hexpand=True)
        self.sessions.connect("notify::selected", self.select)
        self.error = Gtk.Label(xalign=0, wrap=True, selectable=True)
        scroll, self.session_details = text_panel()
        for child in (self.sessions, button("Actualiser", self.refresh), button("Vérifier l’intégrité", self.verify)):
            toolbar.append(child)
        for child in (toolbar, self.error, scroll):
            page.append(child)
        self.pages.append_page(page, Gtk.Label(label="Sessions"))

    def refresh(self, *_):
        self.catalogue_serial += 1
        serial = self.catalogue_serial

        def load():
            result = []
            for path in reversed(session_folders(self.root)[-200:]):
                try:
                    info = read_json(path / "manifest.json")
                except (OSError, ValueError):
                    continue
                result.append((path, info))
            return result

        def show(rows):
            if serial != self.catalogue_serial:
                return
            previous = self.path
            self.paths = rows
            self.sessions.set_model(Gtk.StringList.new([path.name + " · " + info["status"] for path, info in rows]))
            if rows:
                selected = next((i for i, (path, _) in enumerate(rows) if path == previous), 0)
                self.sessions.set_selected(selected)
                self.select()
            else:
                self.path = self.manifest = None
                fill(self.session_details, "Aucune session.")

        self.submit(load, show)

    def select(self, *_):
        index = self.sessions.get_selected()
        if index >= len(self.paths):
            return
        self.path, self.manifest = self.paths[index]
        data = self.manifest
        self.turns = [cycle["turn"] for cycle in data.get("cycles", {}).values()]
        self.cycles.set_model(Gtk.StringList.new(["Tous les tours", *self.turns]))
        self.start.set_value(0)
        self.error.set_text("")
        self.preview = None
        self.drawing.queue_draw()
        self.event_picker.set_model(None)
        fill(self.event_details, "")
        fill(self.report, "")
        fill(self.session_details, {key: data[key] for key in
                                    ("started", "ended", "status", "total_bytes", "capture", "cycles") if key in data})

    def verify(self, *_):
        if self.path is None:
            return
        path = self.path
        self.error.set_text("Vérification SHA-256…")
        self.submit(lambda: validate_recording(path, read_json(path / "manifest.json")),
                    lambda result: self.error.set_text("Intégrité vérifiée · " + result["status"]) if self.path == path else None)

    def timeline_page(self):
        page = box()
        controls = box(Gtk.Orientation.HORIZONTAL)
        self.cycles = Gtk.DropDown(hexpand=True)
        self.start = Gtk.SpinButton.new_with_range(0, 86400, 1)
        self.width = Gtk.SpinButton.new_with_range(2, 30, 1)
        self.width.set_value(15)
        for child in (self.cycles, Gtk.Label(label="Début (s)"), self.start, Gtk.Label(label="Fenêtre (s)"), self.width,
                      button("Afficher", self.refresh_timeline)):
            controls.append(child)
        self.drawing = Gtk.DrawingArea(content_height=185, hexpand=True)
        self.drawing.set_draw_func(self.draw)
        self.timeline_state = Gtk.Label(xalign=0, label="Temps de publication · décalage physique inconnu")
        self.event_picker = Gtk.DropDown()
        self.event_picker.connect("notify::selected", self.event_selected)
        scroll, self.event_details = text_panel()
        for child in (controls, self.timeline_state, self.drawing, self.event_picker, scroll):
            page.append(child)
        self.pages.append_page(page, Gtk.Label(label="Chronologie"))

    def refresh_timeline(self, *_):
        if self.path is None or self.manifest["status"] == "recording":
            return
        self.serial += 1
        serial, chosen = self.serial, self.cycles.get_selected()
        path = self.path
        turn = self.turns[chosen - 1] if 0 < chosen <= len(self.turns) else None
        origin = self.manifest.get("monotonic_ns", 0)
        start, width = self.start.get_value(), self.width.get_value()
        self.timeline_state.set_text("Lecture de la fenêtre…")

        def show(preview):
            if path != self.path or serial != self.serial:
                return
            self.preview = preview
            self.drawing.queue_draw()
            rows = preview["events"]
            self.timeline_state.set_text(f"Temps de publication · {start:g}–{start + width:g} s · "
                                         f"{len(rows)}/{preview['total']} événements listés")
            self.event_picker.set_model(Gtk.StringList.new([
                f"{(row['monotonic_ns'] - origin) / 1e9:7.2f} s · {row['component']} / {row['event']}"
                for row in rows]))
            self.event_selected()

        recalculated = (self.recalculated["alignment"] if self.recalculated
                        and self.recalculated["cycle"] == f"{path.name}/{turn}" else None)
        self.submit(lambda: tiles(path, turn, origin, start, width, recalculated), show, self.timeline_state.set_text)

    def event_selected(self, *_):
        index = self.event_picker.get_selected()
        rows = self.preview["events"] if self.preview else ()
        fill(self.event_details, rows[index] if index < len(rows) else "")

    def draw(self, _widget, ctx, width, height):
        ctx.set_source_rgb(.094, .094, .145)
        ctx.paint()
        if not self.preview:
            return
        tracks = {**self.preview["peaks"]}
        markers = self.preview["markers"]
        for row in markers:
            tier = row["data"]["tier"]
            tracks.setdefault(tier, [])
        if not tracks:
            return
        top = height / len(tracks)
        for number, (track, peaks) in enumerate(tracks.items()):
            center = (number + .55) * top
            ctx.set_source_rgb(.706, .745, .996)
            ctx.move_to(4, center)
            ctx.show_text(track)
            for i, (low, high) in enumerate(peaks):
                if low is None:
                    continue
                x = 85 + i * max(1, width - 85) / BINS
                ctx.move_to(x, center - max(-1, min(1, low)) * top / 2)
                ctx.line_to(x, center - max(-1, min(1, high)) * top / 2)
            ctx.stroke()
            for row in markers:
                if row["data"]["tier"] != track:
                    continue
                fraction = (row["monotonic_ns"] - self.preview["begin"]) / (self.preview["end"] - self.preview["begin"])
                x = 85 + fraction * max(1, width - 85)
                kind = row["event"]
                size = {"line": .46, "word-start": .33, "word-end": .21, "abstention": .26, "phone": .1,
                        "score-decision": .35}[kind]
                ctx.set_source_rgb(*((1., .58, .59) if kind == "abstention" else
                                    (.59, .87, .73) if kind == "word-end" else (.71, .75, 1.)))
                ctx.move_to(x, center - size * top)
                ctx.line_to(x, center + size * top)
                ctx.stroke()

    def report_page(self):
        page = box()
        controls = box(Gtk.Orientation.HORIZONTAL)
        for action, label in (("report", "Rapport"), ("export", "Aperçu expurgé"),
                              ("lyrics-reference", "Référence des paroles")):
            controls.append(button(label, lambda *_args, kind=action: self.run_report(kind)))
        page.append(controls)
        scroll, self.report = text_panel()
        page.append(scroll)
        self.pages.append_page(page, Gtk.Label(label="Rapport et rejeu"))

    def run_report(self, action):
        path = self.path
        if path is None:
            return
        selected = self.cycles.get_selected()
        turn = self.turns[selected - 1] if 0 < selected <= len(self.turns) else None
        if action == "lyrics-reference" and turn is None:
            fill(self.report, "Choisis un tour V3 dans la chronologie.")
            return
        fill(self.report, "Traitement…")

        def run():
            args = [str(BIN / "kara-blackbox"), "--root", str(self.root), action, path.name, "--limit", "50"]
            if turn and action == "lyrics-reference":
                args.extend(("--cycle", f"{path.name}/{turn}"))
            command = subprocess.run(args, capture_output=True, text=True, timeout=120)
            if command.returncode:
                raise ValueError(command.stderr.strip())
            return [json.loads(line) for line in command.stdout.splitlines()]

        def show(results):
            if self.path != path:
                return
            fill(self.report, "\n\n".join(json.dumps(result, indent=2, ensure_ascii=False) for result in results))

        self.submit(run, show, lambda message: fill(self.report, message))
