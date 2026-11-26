# SPDX-License-Identifier: MIT
"""GTK frontend for the separate Labfy synchronization process."""

from collections import Counter
import json
import os
from pathlib import Path
import signal

from gi.repository import Gio, GLib, Gtk

from _kara_paths import CACHE, DATA, STATE
from _musique_oz import INBOX, group_locations, grouped_path, index_revision

SIDES = ("PC", "Android")
INVALID = Gtk.INVALID_LIST_POSITION
PAGE_SIZE = 140
LOG_EVERY = 100
OPERATION_LABELS = {"add": "Fichier transféré", "modify": "Fichier actualisé", "copy": "Copie vérifiée",
                    "rename": "Fichier renommé", "delete": "Fichier mis en réserve"}
OPERATION_VERBS = {"Transfert": "copy", "Renommage": "rename", "Mise en réserve": "delete"}


class MusicSync:
    def __init__(self, parent, library, bin_directory):
        self.parent, self.library = parent, library
        configured = os.environ.get("LABFY_MUSIC_MANAGER")
        checkout = Path(configured).expanduser() if configured else bin_directory.parent.parent / "labfy-music-manager"
        self.launcher = checkout / "scripts/labfy-sync" if checkout.is_absolute() else None
        self.requested = ""
        self.closed = False
        self.probe = None
        self.connection = {}
        self.dialog = None
        self.button = Gtk.Button(css_classes=["sync-button"])
        content = Gtk.Box(spacing=6)
        content.append(Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic"))
        content.append(Gtk.Label(label="Synchroniser"))
        self.button.set_child(content)
        self.button.update_property([Gtk.AccessibleProperty.LABEL], ["Synchroniser"])
        self.button.connect("clicked", self.open)
        self.connected({"state": "checking", "message": "Vérification USB…"})
        self.timer = GLib.timeout_add_seconds(3, self.poll)
        self.poll()

    def available(self):
        return self.launcher is not None and os.access(self.launcher, os.X_OK) and self.launcher.is_file()

    def log(self, message, level="INFO"):
        if not self.closed and self.parent.maintenance is not None:
            self.parent.maintenance.journal.emit("SYNC", message, level)

    def command(self, mode):
        args = [str(self.launcher), mode, "--pc", str(self.library)]
        if mode in {"session", "resume"} and self.connection.get("exclude_dirs"):
            # On current engines the shared scope applies before planning,
            # so an active import can coexist with unrelated USB operations.
            roots = {INBOX}
            library = self.library.resolve()
            for directory in self.technical_dirs():
                if directory.is_relative_to(library) and directory != library:
                    roots.add(directory.relative_to(library).as_posix())
            args.extend(f"--exclude-dir={root}" for root in sorted(roots))
        return args

    @staticmethod
    def technical_dirs():
        return tuple(directory.resolve() for directory in (CACHE / "paroles-strawberry", STATE / "kara", DATA / "kara"))

    def album_moves(self):
        maintenance = self.parent.maintenance
        if maintenance is None:
            return {}
        database = maintenance.index.path
        moves = {}
        for source in group_locations(database, index_revision(database)):
            original = self.library / source
            destination = grouped_path(self.library, original, database=database)
            if destination != original and destination.is_dir():
                moves[source.as_posix()] = destination.relative_to(self.library).as_posix()
        return moves

    def poll(self):
        if self.probe is not None or self.closed or (self.dialog is not None and self.dialog.process is not None):
            return GLib.SOURCE_CONTINUE
        if not self.available():
            self.connected({"state": "unavailable", "message": "Lanceur Labfy introuvable ou non exécutable."})
            return GLib.SOURCE_CONTINUE
        try:
            self.probe = Gio.Subprocess.new(self.command("probe"), Gio.SubprocessFlags.STDOUT_PIPE |
                                           Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            self.connected({"state": "blocked", "message": error.message})
        else:
            self.probe.communicate_utf8_async(None, None, self.probed)
        return GLib.SOURCE_CONTINUE

    def probed(self, process, result):
        if process != self.probe:
            return
        self.probe = None
        try:
            _, data, _ = process.communicate_utf8_finish(result)
            state = json.loads(data)
            if (state.get("event") != "connection" or state.get("state") not in {"ready", "absent", "blocked"}
                    or not isinstance(state.get("message"), str)):
                raise ValueError("Réponse USB Labfy invalide.")
        except (GLib.Error, ValueError, AttributeError):
            state = {"state": "blocked", "message": "Le diagnostic USB Labfy a échoué."}
        self.connected(state)

    def connected(self, state):
        status, message = state["state"], state["message"]
        if status != "checking" and (status, message) != (self.connection.get("state"), self.connection.get("message")):
            self.log(message, "WARNING" if status in {"blocked", "unavailable"} else "INFO")
        self.connection = state
        self.requested = state.get("configured_android", self.requested)
        for style, enabled in (("sync-ready", status == "ready"), ("sync-unavailable", status != "ready")):
            (self.button.add_css_class if enabled else self.button.remove_css_class)(style)
        self.button.set_tooltip_text("Synchroniser la musique en USB\n" + message)
        self.button.update_property([Gtk.AccessibleProperty.DESCRIPTION], [message])
        if self.dialog and not self.dialog.closed:
            self.dialog.connected()

    def open(self, _button):
        created = self.dialog is None
        dialog = self.present_dialog()
        # Opening an overview must never run a recovery, which may restore a file.
        if dialog and created:
            if self.connection.get("state") != "ready":
                self.log(self.connection["message"], "WARNING")
            elif not self.connection.get("recover") and not self.connection.get("media_repair"):
                dialog.prepare(None)

    def open_settings(self, _button):
        if dialog := self.present_dialog():
            dialog.path.grab_focus()

    def present_dialog(self):
        if self.dialog is None:
            self.dialog = SyncWindow(self)
        if self.dialog.closed:
            return None
        self.dialog.present()
        return self.dialog

    def saved(self, path):
        if self.closed:
            return
        if self.probe is not None:
            self.probe.force_exit()
            self.probe = None
        self.connected({"state": "checking", "message": "Vérification USB…", "configured_android": path})
        self.poll()

    def close(self):
        self.closed = True
        GLib.source_remove(self.timer)
        if self.probe is not None:
            self.probe.force_exit()
            self.probe = None
        if self.dialog and not self.dialog.closed:
            self.dialog.close_request(self.dialog)


class SyncWindow(Gtk.Window):
    def __init__(self, controller):
        super().__init__(title="Synchronisation USB", transient_for=controller.parent,
                         modal=True, destroy_with_parent=True, default_width=700, default_height=620)
        self.controller = controller
        self.application = self.get_transient_for().get_application()
        self.process = None
        self.saving = None
        self.configured_path = controller.requested
        self.entries = []
        self.conflicts = []
        self.decisions = {}
        self.labels = []
        self.confirmed = False
        self.closed = False
        self.stopping = False
        self.suspending = False
        self.transferring = False
        self.android_available = None
        self.updating_choices = False
        self.terminal_event = False
        self.statistics = {}
        self.corrections = {}
        self.sections = {}
        self.page = 0
        self.connect("close-request", self.close_request)
        self.set_titlebar(Gtk.HeaderBar())
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(root)

        header = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["track"])
        root.append(header)
        self.connection_label = Gtk.Label(xalign=0, wrap=True, css_classes=["lyrics-info"])
        header.append(self.connection_label)
        pc = Gtk.Label(label=f"PC · {controller.library}", xalign=0, selectable=True, wrap=True)
        header.append(pc)
        path_row = Gtk.Box(spacing=8)
        path_row.append(Gtk.Label(label="Android"))
        self.path = Gtk.Entry(hexpand=True, text=controller.requested,
                              placeholder_text="Audio (détection à la racine)")
        self.path.set_tooltip_text("Chemin exact d’un dossier existant sous /storage/emulated/0")
        self.path.connect("changed", lambda *_: self.refresh_controls())
        self.path.connect("activate", self.save_settings)
        path_row.append(self.path)
        self.save_button = Gtk.Button(label="Enregistrer", tooltip_text="Mémoriser le dossier Android")
        self.save_button.connect("clicked", self.save_settings)
        path_row.append(self.save_button)
        self.preview_button = Gtk.Button(label="Aperçu")
        self.preview_button.connect("clicked", self.prepare)
        path_row.append(self.preview_button)
        header.append(path_row)

        self.summary = Gtk.Label(xalign=0, wrap=True, css_classes=["lyrics-info"])
        summary_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=10,
                              margin_bottom=8, margin_start=22, margin_end=22)
        root.append(summary_box)
        summary_box.append(self.summary)
        self.saved_progress = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["lyrics-info"])
        summary_box.append(self.saved_progress)
        self.progress = Gtk.ProgressBar(visible=False)
        summary_box.append(self.progress)
        self.usb_progress = Gtk.ProgressBar(visible=False, show_text=True)
        summary_box.append(self.usb_progress)
        self.file_progress = Gtk.ProgressBar(visible=False, show_text=True)
        summary_box.append(self.file_progress)
        self.transfer_status = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["lyrics-info"])
        summary_box.append(self.transfer_status)
        self.media_status = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["lyrics-info"])
        summary_box.append(self.media_status)
        self.media_progress = Gtk.ProgressBar(visible=False, show_text=True)
        summary_box.append(self.media_progress)
        self.power_status = Gtk.Label(xalign=0, wrap=True, visible=False, css_classes=["lyrics-info"])
        summary_box.append(self.power_status)

        self.navigation = Gtk.Box(spacing=8, margin_start=22, margin_end=22, margin_bottom=6, visible=False)
        self.category = Gtk.DropDown(hexpand=True)
        self.category.connect("notify::selected", self.change_category)
        self.navigation.append(self.category)
        self.previous = Gtk.Button(icon_name="go-previous-symbolic", tooltip_text="Page précédente")
        self.previous.connect("clicked", lambda *_: self.change_page(-1))
        self.navigation.append(self.previous)
        self.page_label = Gtk.Label(css_classes=["lyrics-info"])
        self.navigation.append(self.page_label)
        self.following = Gtk.Button(icon_name="go-next-symbolic", tooltip_text="Page suivante")
        self.following.connect("clicked", lambda *_: self.change_page(1))
        self.navigation.append(self.following)
        root.append(self.navigation)

        self.text = Gtk.TextView(editable=False, cursor_visible=False, wrap_mode=Gtk.WrapMode.CHAR,
                                 left_margin=12, right_margin=12, top_margin=8, bottom_margin=8)
        scroll = Gtk.ScrolledWindow(vexpand=True, child=self.text, margin_start=22, margin_end=22)
        root.append(scroll)
        self.edit_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8, margin_top=8,
                                margin_bottom=8, margin_start=22, margin_end=22)
        root.append(self.edit_box)
        self.conflict_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=5, visible=False)
        self.edit_box.append(self.conflict_box)
        self.conflict_box.append(Gtk.Label(label="Résoudre un conflit", xalign=0))
        self.conflict_paths = Gtk.DropDown(hexpand=True, enable_search=True)
        self.conflict_paths.connect("notify::selected", self.choose_conflict)
        self.conflict_box.append(self.conflict_paths)
        self.choice = Gtk.DropDown(hexpand=True)
        self.choice.connect("notify::selected", self.choose_version)
        self.conflict_box.append(self.choice)
        self.choice_detail = Gtk.Label(xalign=0, wrap=True, css_classes=["lyrics-info"])
        self.conflict_box.append(self.choice_detail)
        self.deletions = Gtk.CheckButton(label="Confirmer les suppressions proposées", visible=False)
        self.deletions.connect("toggled", lambda *_: self.update_summary())
        self.edit_box.append(self.deletions)

        footer = Gtk.Box(spacing=8, halign=Gtk.Align.END, css_classes=["lyrics-footer"])
        root.append(footer)
        self.repair_button = Gtk.Button(label="Réparer l’indexation", visible=False)
        self.repair_button.connect("clicked", self.repair_media)
        footer.append(self.repair_button)
        self.abandon_button = Gtk.Button(label="Abandonner le plan", visible=False,
                                         tooltip_text="Conserver les fichiers déjà publiés et refaire un aperçu")
        self.abandon_button.connect("clicked", self.abandon)
        footer.append(self.abandon_button)
        self.suspend_button = Gtk.Button(label="Suspendre", sensitive=False)
        self.suspend_button.connect("clicked", self.suspend)
        footer.append(self.suspend_button)
        self.cancel_button = Gtk.Button(label="Annuler")
        self.cancel_button.connect("clicked", self.cancel)
        footer.append(self.cancel_button)
        self.confirm = Gtk.Button(label="Démarrer", sensitive=False, css_classes=["suggested-action"])
        self.confirm.connect("clicked", self.apply)
        footer.append(self.confirm)
        self.connected()

    def connected(self):
        state = self.controller.connection
        if self.configured_path != self.controller.requested:
            previous = self.configured_path
            self.configured_path = self.controller.requested
            if self.path.get_text() == previous:
                self.path.set_text(self.configured_path)
        text = state.get("message", "Vérification USB…")
        if state.get("protocol"):
            text += f" · {state['protocol']}"
        if state.get("usb_mbps") is not None:
            text += f" · Liaison négociée à {state['usb_mbps']:g} Mbit/s"
        if state.get("android"):
            text += f" · {state['android']}"
            if self.android_available is not None:
                text += f" · {GLib.format_size(self.android_available)} disponibles"
        resume = state.get("resume")
        needs_helper = resume is None or resume["completed"] < resume["total"]
        if needs_helper and state.get("helper") is False:
            text += " · Exécuter scripts/build-android-helper avant d’appliquer."
        if state.get("android_override"):
            text += " · Le réglage LABFY_ANDROID_ROOT de cette session est prioritaire."
        if state.get("media_pending"):
            text += f" · {state['media_pending']} chemin(s) MediaStore en attente."
        if state.get("power_warning"):
            text += " · " + state["power_warning"]
        self.connection_label.set_text(text)
        self.saved_progress.set_visible(resume is not None)
        if resume is not None:
            if resume["total"]:
                saved = (f"Progression sauvegardée · {resume['completed']}/{resume['total']} opérations · "
                         f"{GLib.format_size(resume['bytes_done'])} / {GLib.format_size(resume['bytes_total'])}")
            else:
                saved = "Aucun fichier à transférer."
            if resume.get("recovered"):
                saved += (f"\n{resume['recovered']} fichiers déjà vérifiés sur Android · "
                          f"{GLib.format_size(resume['bytes_recovered'])}")
            if resume.get("in_flight"):
                phase = resume.get("file_phase")
                if phase == "verified":
                    saved += "\nL’intégrité du transfert interrompu a été vérifiée. La reprise contrôle la publication."
                elif phase == "transferred":
                    saved += "\nOctets reçus avant l’interruption. La reprise vérifie leur intégrité."
                else:
                    saved += "\nUne opération interrompue sera vérifiée."
            if resume.get("last_error") and self.process is None:
                saved += f"\nDernière erreur · {resume['last_error']}"
            self.saved_progress.set_text(saved)
        self.refresh_controls()

    def refresh_controls(self):
        state = self.controller.connection
        selected_path = self.path.get_text()
        changed_path = selected_path != self.controller.requested
        resumable = state.get("recover") and not changed_path
        saved = state.get("resume")
        media_only = saved is not None and saved["completed"] == saved["total"]
        has_plan = self.process is not None and self.confirmed
        idle = self.process is None and self.saving is None
        self.path.set_editable(idle)
        self.save_button.set_sensitive(idle and changed_path and state.get("state") != "unavailable")
        self.preview_button.set_label("Reprendre" if resumable else "Aperçu")
        self.preview_button.set_sensitive(idle and not changed_path and state.get("state") == "ready" and
                                          (not resumable or media_only or state.get("helper", True)))
        self.repair_button.set_visible(bool(state.get("media_repair")) and state.get("resume") is None)
        self.repair_button.set_sensitive(idle and not changed_path and state.get("state") == "ready")
        self.abandon_button.set_visible(bool(saved and state.get("abandon_plan")))
        self.abandon_button.set_sensitive(idle and not changed_path and state.get("state") == "ready")
        self.suspend_button.set_sensitive(self.transferring and not self.stopping)
        self.cancel_button.set_label("Annuler" if self.process is not None else "Fermer")
        self.cancel_button.set_sensitive(not self.stopping)
        self.edit_box.set_sensitive(has_plan)
        self.confirm.set_sensitive(has_plan and state.get("state") == "ready" and
                                   state.get("helper", True) and not self.deletion_requires_confirmation())

    def save_settings(self, _widget):
        if not self.save_button.get_sensitive():
            return
        value = self.path.get_text()
        try:
            self.saving = Gio.Subprocess.new(
                [*self.controller.command("configure"), f"--android={value}"],
                Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            self.summary.set_text(error.message)
            self.controller.log(error.message, "ERROR")
            return
        self.application.hold()
        self.saving.communicate_utf8_async(None, None, self.settings_saved)
        self.refresh_controls()

    def settings_saved(self, process, result):
        self.saving = None
        try:
            _, data, _ = process.communicate_utf8_finish(result)
            message = json.loads(data)
            if message["event"] == "configured":
                if not self.closed:
                    self.path.set_text(message["configured_android"])
                self.controller.saved(message["configured_android"])
                if not self.closed:
                    self.summary.set_text("Dossier Android enregistré.")
            elif not self.closed:
                self.summary.set_text(message["message"])
                self.controller.log(message["message"], "ERROR")
        except (GLib.Error, ValueError, KeyError):
            if not self.closed:
                message = "Impossible d’enregistrer le dossier Android."
                self.summary.set_text(message)
                self.controller.log(message, "ERROR")
        finally:
            self.process_released()
            self.application.release()

    def prepare(self, _button):
        if not self.preview_button.get_sensitive():
            return
        state = self.controller.connection
        if state.get("resume") is not None:
            args = [*self.controller.command("resume"), "--android", state["android"]]
            self.start_process(args, "Reprise de la synchronisation…")
        else:
            args = [*self.controller.command("session"), "--android", state["android"]]
            if state.get("recover"):
                args.append("--recover")
            self.start_process(args, "Préparation de l’aperçu…")

    def repair_media(self, _button):
        if not self.repair_button.get_sensitive():
            return
        args = [*self.controller.command("repair-media"), "--android", self.controller.connection["android"]]
        self.start_process(args, "Préparation de la réparation MediaStore…")

    def abandon(self, _button):
        if not self.abandon_button.get_visible() or not self.abandon_button.get_sensitive():
            return
        args = [*self.controller.command("abandon"), "--android", self.controller.connection["android"]]
        self.start_process(args, "Abandon du plan interrompu…")

    def start_process(self, args, message):
        self.terminal_event = False
        self.transferring = False
        self.suspending = False
        self.android_available = None
        self.operations = Counter()
        self.pending_operation = None
        self.copy_kinds = {}
        self.media_errors = 0
        self.usb_finished = False
        self.rescan_on_finish = args[1] == "resume" or "--recover" in args
        self.summary.set_text(message)
        self.controller.log(message)
        self.text.get_buffer().set_text("")
        self.progress.set_visible(True)
        self.usb_progress.set_visible(False)
        self.file_progress.set_visible(False)
        self.transfer_status.set_visible(False)
        self.media_status.set_visible(False)
        self.media_progress.set_visible(False)
        self.power_status.set_visible(False)
        self.navigation.set_visible(False)
        self.progress.pulse()
        self.entries, self.conflicts, self.decisions, self.corrections = [], [], {}, {}
        self.conflict_box.set_visible(False)
        self.deletions.set_visible(False)
        self.deletions.set_active(False)
        layout = None
        if args[1] == "session" and self.controller.connection.get("pc_layout") and (moves := self.controller.album_moves()):
            # The receipt supplies names only. Labfy still proves file
            # identity and checks both sides before its exclusive rename.
            args.append("--pc-layout")
            layout = {"action": "pc-layout", "moves": moves}
        try:
            self.process = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDIN_PIPE |
                                              Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            self.end_report(error.message, "ERROR")
            self.progress.set_visible(False)
        else:
            # Let the child finish its cooperative stop and persist its
            # journal state even when the main window has closed.
            self.application.hold()
            self.transferring = args[1] == "resume"
            stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)
            if layout is not None:
                self.send_request(layout)
        self.refresh_controls()

    def on_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
            if line is not None:
                self.on_event(json.loads(line))
                stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)
                return
        except (GLib.Error, ValueError, KeyError, TypeError) as error:
            self.end_report(f"Réponse Labfy invalide · {error}", "ERROR")
            self.process.send_signal(signal.SIGTERM)
        self.process.wait_async(None, self.finished)

    def finished(self, process, result):
        try:
            process.wait_finish(result)
        except GLib.Error as error:
            self.end_report(error.message, "ERROR")
        if not self.terminal_event:
            self.end_report("La session Labfy a été interrompue.", "ERROR")
        self.process = None
        self.confirmed = False
        self.stopping = False
        self.transferring = False
        if not self.closed:
            self.progress.set_visible(False)
            self.file_progress.set_visible(False)
            probe = self.controller.probe
            if probe is not None:
                self.controller.probe = None
                probe.force_exit()
            self.controller.poll()
        maintenance = self.controller.parent.maintenance
        if self.rescan_on_finish and maintenance is not None and not self.controller.closed:
            maintenance.rescan()
        self.process_released()
        self.application.release()

    def process_released(self):
        if not self.closed:
            self.refresh_controls()
        elif self.process is None and self.saving is None:
            self.controller.dialog = None

    def complete_operation(self, completed):
        # Labfy reports the index before execution. Only a later index or the
        # persisted cursor proves publication, even when all bytes were sent.
        if self.pending_operation is None or self.pending_operation[0] >= completed:
            return
        _, kind, side, path = self.pending_operation
        self.pending_operation = None
        self.operations[kind] += 1
        total = self.operations.total()
        if self.operations[kind] <= 3:
            self.controller.log(f"{OPERATION_LABELS[kind]} · {side} · {path}")
        elif total % LOG_EVERY == 0:
            self.controller.log(f"{total} opérations vérifiées dans cette session.")

    def end_report(self, message, level="INFO", *, pending=None):
        self.terminal_event = True
        if not self.closed:
            self.summary.set_text(message)
        counts = self.operations
        detail = (f"Bilan de la session · {counts['add']} transféré(s), {counts['modify']} actualisé(s), "
                  f"{counts['copy']} copie(s) reprise(s), {counts['rename']} renommé(s), "
                  f"{counts['delete']} mis en réserve, {self.media_errors} erreur(s) d’indexation")
        if level == "ERROR":
            detail += ", 1 erreur de synchronisation"
        if pending is not None:
            detail += f", {pending} chemin(s) à indexer"
        self.controller.log(message, level)
        self.controller.log(detail)

    def record_event(self, event, data):
        if event == "progress":
            message, count = data["message"], data["count"]
            verb, _, location = message.partition(" · ")
            if verb in OPERATION_VERBS:
                side, _, path = location.partition(" · ")
                self.complete_operation(count)
                kind = self.copy_kinds.get((side, path), OPERATION_VERBS[verb])
                self.pending_operation = (count, kind, side, path)
            elif message == "Synchronisation terminée.":
                self.complete_operation(count)
        elif event == "media" and data["errors"] > self.media_errors:
            self.media_errors = data["errors"]
            self.controller.log(f"Indexation MediaStore · {self.media_errors} erreur(s), "
                                f"{data['pending']} chemin(s) en attente", "WARNING")
        elif event == "error":
            if resume := data.get("resume"):
                self.complete_operation(resume["completed"])
            if self.suspending and resume:
                self.end_report("Synchronisation suspendue. La progression est sauvegardée.")
            else:
                self.end_report(data["message"], "WARNING" if self.stopping or self.closed else "ERROR")
        elif event == "done":
            if self.pending_operation is not None:
                self.complete_operation(self.pending_operation[0] + 1)
            pending = data.get("media_pending", 0)
            # The engine just closed the journal. Invalidate a previous probe
            # immediately so reopening the dialog cannot offer a stale resume.
            if not data.get("repair"):
                self.controller.connection.update(recover=False, resume=None)
            self.controller.connection.update(media_repair=bool(pending), media_pending=pending)
            if data.get("abandoned"):
                message = "Plan abandonné. Les fichiers déjà publiés sont conservés."
            elif data.get("repair"):
                message = "Réparation MediaStore à poursuivre." if pending else "Réparation MediaStore terminée."
            else:
                message = "Transferts terminés. Indexation Android incomplète." if pending else "Synchronisation terminée."
            if pending:
                message += f" {pending} chemin(s) à traiter via Réparer l’indexation."
            if "adb_commands" in data:
                self.controller.log(f"Mesures · {data['adb_commands']} commandes ADB · {data['elapsed']:.2f} s · "
                                    f"{GLib.format_size(data['verified_bytes'])} vérifiés dans le plan")
            if phases := data.get("phases"):
                self.controller.log(
                    f"Phases USB · transfert {phases['transfer_s']:.2f} s · vérification {phases['verify_s']:.2f} s · "
                    f"publication {phases['publish_s']:.2f} s · MediaStore {phases['index_s']:.2f} s · "
                    f"chevauchement USB/MediaStore {phases['overlap_s']:.2f} s · total {phases['total_s']:.2f} s")
            self.end_report(message, "WARNING" if pending else "INFO", pending=pending)

    def on_event(self, data):
        event = data["event"]
        self.record_event(event, data)
        if self.closed:
            return
        if self.stopping and event in {"progress", "preview", "transfer", "media"}:
            return
        if event == "progress":
            count, total = data["count"], data["total"]
            self.summary.set_text(f"{data['message']} · {count}/{total}" if total
                                  else f"{data['message']} · {count}")
            self.progress.set_visible(True)
            self.progress.set_show_text(False)
            if data["message"].startswith(("Renommage", "Mise en réserve")):
                self.file_progress.set_visible(False)
            if total:
                self.progress.set_fraction(min(1, count / total))
            else:
                self.progress.pulse()
        elif event == "preview" and not self.stopping:
            self.android_available = data.get("available_android")
            self.connected()
            self.statistics = data["statistics"]
            self.corrections = data["corrections"]
            self.show_plan(data["entries"], self.corrections)
            self.controller.log(f"Aperçu prêt · {len(self.entries)} proposition(s), {len(self.conflicts)} conflit(s)")
            if times := data.get("seconds"):
                self.controller.log(f"Inventaire PC {times['inventory_pc']:.2f} s · Android {times['inventory_android']:.2f} s · "
                                    f"Comparaison {times['compare']:.2f} s · {data['adb_commands']} commandes ADB")
            state = self.controller.connection
            if state.get("protocol") and state.get("concurrency"):
                self.controller.log(f"Transport {state['protocol']} sur USB · {state['concurrency']} transfert simultané")
            if deferred := self.statistics.get("deferred"):
                self.controller.log(f"{deferred} fichier(s) en cours de modification reporté(s).", "WARNING")
        elif event == "transfer" and not self.stopping:
            self.usb_progress.set_visible(True)
            self.usb_progress.set_fraction(min(1, data["transferred"] / data["total"]))
            self.usb_progress.set_text(f"USB · {GLib.format_size(data['transferred'])} / {GLib.format_size(data['total'])}")
            self.file_progress.set_visible(True)
            self.file_progress.set_fraction(min(1, data["file_bytes"] / data["file_total"]))
            self.file_progress.set_text(f"{GLib.format_size(data['file_bytes'])} / {GLib.format_size(data['file_total'])}")
            eta = data["eta"]
            estimate = "estimation en cours" if eta is None else f"env. {eta // 60} min {eta % 60:02d} s"
            self.transfer_status.set_visible(True)
            rate = GLib.format_size(data.get("instant_rate", data["rate"]))
            average = f" · {GLib.format_size(data['average_rate'])}/s en moyenne" if "average_rate" in data else ""
            self.transfer_status.set_text(f"{rate}/s instantané{average} · "
                                          f"{GLib.format_size(data['remaining'])} restants · {estimate}")
        elif event == "media":
            transferred, indexed, errors = data["transferred"], data["indexed"], data["errors"]
            verified = data.get("verified", transferred)
            remaining = data.get("remaining_tracks", data["pending"])
            finished = data.get("transfers_finished", False)
            if finished and not self.usb_finished and (transferred or self.usb_progress.get_visible()):
                # MediaStore may continue after the last published file.
                # Keep definitive USB progress and throughput on screen.
                self.usb_finished = True
                self.file_progress.set_visible(False)
                self.summary.set_text("Transfert USB terminé · indexation Android en cours"
                                      if data["pending"] else "Transfert USB terminé")
                self.usb_progress.set_fraction(1)
                self.usb_progress.set_text("Transfert USB terminé")
                self.usb_progress.set_visible(True)
            detail = f"{indexed} piste(s) indexée(s) · {remaining} piste(s) restant à indexer"
            eta = data.get("index_eta")
            if eta is not None:
                detail += f" · env. {eta // 60} min {eta % 60:02d} s"
            self.media_status.set_text(f"{transferred} fichier(s) transféré(s) · {verified} vérifié(s)\n"
                                       f"{detail} · {errors} erreur(s) d’indexation")
            self.media_status.set_visible(True)
            total = indexed + remaining
            self.media_progress.set_fraction(indexed / total if total else 1.0)
            self.media_progress.set_text("Indexation Android" if data["pending"] else "Indexation Android à jour")
            self.media_progress.set_visible(bool(total or data["pending"]))
        elif event == "power":
            self.power_status.set_text(data["message"])
            self.power_status.set_visible(bool(data.get("active") or data.get("pending")))
        elif event == "error" and data.get("recover"):
            self.controller.connection.update(recover=True, resume=data.get("resume"))
            self.connected()
        elif event == "done":
            self.connected()

    @staticmethod
    def describe(step):
        side, path = SIDES[step["side"]], step["path"]
        match step["kind"]:
            case "delete":
                return f"{side} · {path} → sauvegarde"
            case "rename":
                return f"{side} · {step['source']} → {path}"
            case _:
                return f"→ {side} · {path}"

    def show_plan(self, entries, corrections):
        self.entries = entries
        self.conflicts = [row for row in entries if row["kind"] == "conflict"]
        sections = {"Vers Android": [], "Vers le PC": [], "Modifications": [],
                    "Renommages": [], "Conflits": [], "Suppressions proposées": [],
                    "Corrections de chemins": [f"{source} → {target}" for source, target in corrections.items()],
                    "Chemins à corriger": []}
        for row in entries:
            kind, path = row["kind"], row["path"]
            if kind == "conflict":
                sections["Conflits"].append(path)
                continue
            if kind == "blocked":
                sections["Chemins à corriger"].append(f"{path} · {row['reason']}")
                continue
            step = row["choices"]["apply"][0]
            title = {"add": "Vers Android" if step["side"] == 1 else "Vers le PC",
                     "modify": "Modifications", "rename": "Renommages",
                     "delete": "Suppressions proposées"}[kind]
            sections[title].append(self.describe(step))
        self.sections = {name: rows for name, rows in sections.items() if rows}
        self.category.set_model(Gtk.StringList.new([f"{name} ({len(rows)})" for name, rows in self.sections.items()]))
        self.navigation.set_visible(bool(self.sections))
        self.change_category()
        self.conflict_paths.set_model(Gtk.StringList.new([row["path"] for row in self.conflicts]))
        if self.conflicts:
            self.conflict_paths.set_selected(0)
        self.choose_conflict()
        self.conflict_box.set_visible(bool(self.conflicts))
        self.progress.set_visible(False)
        self.confirmed = True
        self.update_summary()

    def change_category(self, *_):
        self.page = 0
        self.change_page(0)

    def change_page(self, offset):
        index = self.category.get_selected()
        if index == INVALID or index >= len(self.sections):
            self.text.get_buffer().set_text("Les bibliothèques sont déjà à jour.")
            return
        rows = list(self.sections.values())[index]
        pages = (len(rows) + PAGE_SIZE - 1) // PAGE_SIZE
        self.page = max(0, min(pages - 1, self.page + offset))
        start = self.page * PAGE_SIZE
        # Only a bounded slice enters Gtk.TextBuffer, even for a full library.
        self.text.get_buffer().set_text("\n".join(rows[start:start + PAGE_SIZE]))
        self.previous.set_sensitive(self.page > 0)
        self.following.set_sensitive(self.page + 1 < pages)
        self.page_label.set_text(f"{self.page + 1}/{pages}")

    def choose_conflict(self, *_):
        index = self.conflict_paths.get_selected()
        if index == INVALID:
            return
        row = self.conflicts[index]
        labels = ["Ignorer pour l’instant"]
        self.labels = ["skip", *row["choices"]]
        for choice, steps in row["choices"].items():
            if steps[0]["kind"] == "delete":
                label = f"Répercuter la suppression côté {SIDES[steps[0]['side']]}"
            else:
                label = {"pc": "Conserver la version PC", "android": "Conserver la version Android",
                         "both": "Conserver les deux versions"}[choice]
            labels.append(label)
        self.updating_choices = True
        self.choice.set_model(Gtk.StringList.new(labels))
        self.choice.set_selected(self.labels.index(self.decisions.get(row["path"], "skip")))
        self.updating_choices = False
        self.choose_version()

    def choose_version(self, *_):
        index = self.conflict_paths.get_selected()
        choice = self.choice.get_selected()
        if not self.updating_choices and index != INVALID and choice != INVALID:
            row = self.conflicts[index]
            version = self.labels[choice]
            if version == "skip":
                self.decisions.pop(row["path"], None)
            else:
                self.decisions[row["path"]] = version
            self.choice_detail.set_text("\n".join(self.describe(step) for step in row["choices"].get(version, ())))
            self.update_summary()

    def deletion_requires_confirmation(self):
        return not self.deletions.get_active() and any(
            step["kind"] == "delete" for row in self.conflicts
            for step in row["choices"].get(self.decisions.get(row["path"]), ()))

    def update_summary(self):
        counts = Counter()
        counts["blocked"] = sum(row["kind"] == "blocked" for row in self.entries)
        sizes = [0, 0]
        deletions = 0
        counted = set()
        for _, step in self.chosen_steps():
            token = step["side"], step["path"]
            if token in counted:
                continue
            counted.add(token)
            if step["kind"] == "delete":
                deletions += 1
                if not self.deletions.get_active():
                    continue
            counts[step["kind"]] += 1
            sizes[step["side"]] += step["bytes"]
        self.deletions.set_visible(deletions > 0)
        stats = self.statistics
        identical = f"{stats['identical']} déjà synchronisé(s) · " if "identical" in stats else ""
        self.summary.set_text(f"{stats.get('total_files', 0)} fichiers · {identical}{stats.get('transferable', 0)} transférables · "
                              f"{stats.get('blocked', 0)} incompatibles · {stats.get('conflicts', 0)} conflits\n"
                              f"{stats.get('corrected', 0)} chemins avec correspondance Android\n"
                              f"{counts['copy']} transfert(s) choisi(s) · {counts['rename']} renommage(s) · "
                              f"{counts['delete']} suppression(s) · {len(self.conflicts) - len(self.decisions)} conflit(s) ignoré(s)\n"
                              f"Vers Android {GLib.format_size(sizes[1])} · Vers le PC {GLib.format_size(sizes[0])}"
                              + (f" · {counts['blocked']} chemin(s) à corriger" if counts["blocked"] else ""))
        self.refresh_controls()

    def chosen_steps(self):
        for row in self.entries:
            choice = self.decisions.get(row["path"], "skip") if row["kind"] == "conflict" else "apply"
            for step in row["choices"].get(choice, ()):
                yield row, step

    def plan_problem(self, steps):
        # Labfy owns selection, validation and writes. Parole can withhold
        # approval when an operation would touch its import inbox or undo a
        # recorded album move that Labfy cannot prove from its USB baseline.
        root = self.controller.library
        technical = self.controller.technical_dirs()
        for _, step in steps:
            for field in ("source", "path"):
                path = Path(step[field])
                if path.parts[:1] and path.parts[0].casefold() == INBOX.casefold():
                    return f"Le dossier de réception {INBOX} figure dans le plan. Terminer le classement puis refaire l’aperçu."
                if any((root / path).is_relative_to(directory) for directory in technical):
                    return f"Un fichier technique de Kara figure dans le plan · {path}"
        maintenance = self.controller.parent.maintenance
        if maintenance is None:
            return None
        database = maintenance.index.path
        locations = group_locations(database, index_revision(database))
        if not locations:
            return None
        android_copies = {step["path"] for _, step in steps if step["side"] == 1 and step["kind"] == "copy"}
        android_deletes = {step["path"] for _, step in steps if step["side"] == 1 and step["kind"] == "delete"}
        pc_publications = {step["path"] for _, step in steps if step["side"] == 0 and step["kind"] != "delete"}
        for row in self.entries:
            path = row["path"]
            if row["kind"] == "rename" or Path(*Path(path).parts[:2]) not in locations:
                continue
            old = root / path
            new = grouped_path(root, old, database=database)
            if new == old:
                continue
            current = new.relative_to(root).as_posix()
            old_android = self.corrections.get(path, path)
            new_android = self.corrections.get(current, current)
            if path in pc_publications or (row["kind"] in {"add", "delete", "conflict", "blocked"}
                                           and new_android in android_copies and old_android not in android_deletes):
                return f"Regroupement à préserver · {path} → {current}. Vérifier l’ancien chemin Android avant de confirmer."
        return None

    def apply(self, _button):
        if not self.confirm.get_sensitive():
            return
        allow_delete = self.deletions.get_active()
        steps = [(row, step) for row, step in self.chosen_steps() if step["kind"] != "delete" or allow_delete]
        if problem := self.plan_problem(steps):
            self.summary.set_text(problem)
            self.controller.log(problem, "WARNING")
            return
        for row, step in steps:
            if step["kind"] != "copy":
                continue
            side, path = step["side"], step["path"]
            original = self.corrections.get(row["path"], row["path"]) if side == 1 else row["path"]
            replaces = row["kind"] == "modify" or (row["kind"] == "conflict" and path == original and
                       all(choice[0]["kind"] != "delete" for choice in row["choices"].values()))
            self.copy_kinds[SIDES[side], path] = "modify" if replaces else "add"
        self.confirmed = False
        self.transferring = True
        self.rescan_on_finish = True
        self.refresh_controls()
        self.controller.log("Plan de synchronisation confirmé.")
        self.send_request({"action": "apply", "conflicts": self.decisions, "delete": allow_delete})

    def send_request(self, request):
        data = json.dumps(request, separators=(",", ":")).encode() + b"\n"
        stream = self.process.get_stdin_pipe()
        stream.write_all_async(data, GLib.PRIORITY_DEFAULT, None, self.request_sent)

    def request_sent(self, stream, result):
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            if not self.closed:
                self.summary.set_text(error.message)
            self.controller.log(error.message, "ERROR")
            if self.process and stream == self.process.get_stdin_pipe():
                self.process.send_signal(signal.SIGTERM)

    def cancel(self, _button):
        if self.process is None:
            self.close()
            return
        self.confirmed = False
        self.stopping = True
        self.summary.set_text("Annulation en cours…")
        self.process.send_signal(signal.SIGTERM)
        self.refresh_controls()

    def suspend(self, _button):
        if not self.suspend_button.get_sensitive():
            return
        self.suspending = self.stopping = True
        self.summary.set_text("Sauvegarde de la progression…")
        # A cooperative signal releases ADB and persists its cursor. Stopping
        # the process with SIGSTOP would retain the USB and library locks.
        self.process.send_signal(signal.SIGINT)
        self.refresh_controls()

    def close_request(self, _window):
        self.closed = True
        self.process_released()
        if self.process:
            self.process.send_signal(signal.SIGTERM)
        return False
