# SPDX-License-Identifier: MIT
"""GTK frontend for the separate Labfy synchronization process."""

from collections import Counter
import json
import os
from pathlib import Path
import signal

from gi.repository import Gio, GLib, Gtk

SIDES = ("PC", "Android")
INVALID = Gtk.INVALID_LIST_POSITION
PAGE_SIZE = 140


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
        self.button = Gtk.Button(icon_name="emblem-synchronizing-symbolic", visible=False,
                                 tooltip_text="Synchroniser la musique en USB")
        self.button.update_property([Gtk.AccessibleProperty.LABEL], ["Synchroniser la musique en USB"])
        self.button.connect("clicked", self.open)
        self.timer = GLib.timeout_add_seconds(3, self.poll)
        self.poll()

    def command(self, mode):
        return [str(self.launcher), mode, "--pc", str(self.library)]

    def poll(self):
        if self.probe is not None or self.launcher is None or not self.launcher.is_file():
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
        except (GLib.Error, ValueError):
            state = {"state": "blocked", "message": "Le diagnostic USB Labfy a échoué."}
        self.connected(state)

    def connected(self, state):
        self.connection = state
        self.requested = state.get("configured_android", self.requested)
        if state["state"] != "checking":
            self.button.set_visible(state["state"] != "absent")
        self.button.set_tooltip_text("Synchroniser la musique en USB\n" + state["message"])
        if self.dialog:
            self.dialog.connected()

    def open(self, _button):
        created = self.dialog is None
        dialog = self.present_dialog()
        # Opening an overview must never run a recovery, which may restore a file.
        if created and not self.connection.get("recover") and not self.connection.get("media_repair"):
            dialog.prepare(None)

    def open_settings(self, _button):
        self.present_dialog().path.grab_focus()

    def present_dialog(self):
        if self.dialog is None:
            self.dialog = SyncWindow(self)
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
        if self.dialog:
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
        self.updating_choices = False
        self.terminal_event = False
        self.statistics = {}
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
        self.cancel_button = Gtk.Button(label="Annuler")
        self.cancel_button.connect("clicked", self.cancel)
        footer.append(self.cancel_button)
        self.confirm = Gtk.Button(label="Confirmer", sensitive=False, css_classes=["suggested-action"])
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
        if state.get("android"):
            text += f" · {state['android']}"
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
        self.save_button.set_sensitive(idle and changed_path)
        self.preview_button.set_label("Reprendre la synchronisation" if resumable else "Aperçu")
        self.preview_button.set_sensitive(idle and not changed_path and state.get("state") == "ready" and
                                          (not resumable or media_only or state.get("helper", True)))
        self.repair_button.set_visible(bool(state.get("media_repair")) and state.get("resume") is None)
        self.repair_button.set_sensitive(idle and not changed_path and state.get("state") == "ready")
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
        except (GLib.Error, ValueError, KeyError):
            if not self.closed:
                self.summary.set_text("Impossible d’enregistrer le dossier Android.")
        finally:
            if not self.closed:
                self.refresh_controls()
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

    def start_process(self, args, message):
        self.terminal_event = False
        self.summary.set_text(message)
        self.text.get_buffer().set_text("")
        self.progress.set_visible(True)
        self.file_progress.set_visible(False)
        self.transfer_status.set_visible(False)
        self.media_status.set_visible(False)
        self.media_progress.set_visible(False)
        self.power_status.set_visible(False)
        self.navigation.set_visible(False)
        self.progress.pulse()
        self.entries, self.conflicts, self.decisions = [], [], {}
        self.conflict_box.set_visible(False)
        self.deletions.set_visible(False)
        self.deletions.set_active(False)
        try:
            self.process = Gio.Subprocess.new(args, Gio.SubprocessFlags.STDIN_PIPE |
                                              Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error as error:
            self.summary.set_text(error.message)
            self.progress.set_visible(False)
        else:
            # Let the child finish its cooperative stop and persist its
            # journal state even when the main window has closed.
            self.application.hold()
            stream = Gio.DataInputStream.new(self.process.get_stdout_pipe())
            stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)
        self.refresh_controls()

    def on_line(self, stream, result):
        try:
            line, _ = stream.read_line_finish_utf8(result)
            if line is not None:
                self.on_event(json.loads(line))
                stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.on_line)
                return
        except (GLib.Error, ValueError) as error:
            self.summary.set_text(str(error))
            self.terminal_event = True
            if self.process:
                self.process.send_signal(signal.SIGTERM)
        self.process.wait_async(None, self.finished)

    def finished(self, process, result):
        try:
            process.wait_finish(result)
        except GLib.Error as error:
            self.summary.set_text(error.message)
        if not self.closed and not self.terminal_event:
            self.summary.set_text("La session Labfy a été interrompue.")
        self.process = None
        self.confirmed = False
        self.stopping = False
        self.progress.set_visible(False)
        self.file_progress.set_visible(False)
        self.refresh_controls()
        if not self.closed:
            probe = self.controller.probe
            if probe is not None:
                self.controller.probe = None
                probe.force_exit()
            self.controller.poll()
        self.application.release()

    def on_event(self, data):
        if self.closed:
            return
        event = data["event"]
        if self.stopping and event in {"progress", "preview", "transfer"}:
            return
        if event == "progress":
            count, total = data["count"], data["total"]
            self.summary.set_text(f"{data['message']} · {count}/{total}" if total
                                  else f"{data['message']} · {count}")
            self.progress.set_visible(True)
            self.progress.set_show_text(False)
            if not data["message"].startswith("Transfert"):
                self.file_progress.set_visible(False)
                self.transfer_status.set_visible(False)
            if total:
                self.progress.set_fraction(min(1, count / total))
            else:
                self.progress.pulse()
        elif event == "preview" and not self.stopping:
            self.connection_label.set_text(f"Galaxy S21 FE connecté par USB · {data['android']}")
            self.statistics = data["statistics"]
            self.show_plan(data["entries"], data["corrections"])
        elif event == "transfer" and not self.stopping:
            self.progress.set_fraction(min(1, data["transferred"] / data["total"]))
            self.progress.set_show_text(True)
            self.progress.set_text(f"{GLib.format_size(data['transferred'])} / {GLib.format_size(data['total'])}")
            self.file_progress.set_visible(True)
            self.file_progress.set_fraction(min(1, data["file_bytes"] / data["file_total"]))
            self.file_progress.set_text(f"{GLib.format_size(data['file_bytes'])} / {GLib.format_size(data['file_total'])}")
            eta = data["eta"]
            estimate = "estimation en cours" if eta is None else f"env. {eta // 60} min {eta % 60:02d} s"
            self.transfer_status.set_visible(True)
            self.transfer_status.set_text(
                f"{GLib.format_size(data['rate'])}/s · {GLib.format_size(data['remaining'])} restants · {estimate}")
        elif event == "media":
            transferred, indexed = data["transferred"], data["indexed"]
            errors = data["errors"]
            file_plural = "s" if transferred != 1 else ""
            track_plural = "s" if indexed != 1 else ""
            error_plural = "s" if errors != 1 else ""
            self.media_status.set_text(
                f"{transferred} fichier{file_plural} transféré{file_plural} · "
                f"{indexed} piste{track_plural} indexée{track_plural}\n"
                f"{data['pending']} en attente d’indexation · {errors} erreur{error_plural} d’indexation")
            self.media_status.set_visible(True)
            pending = data["pending"]
            total = indexed + pending
            self.media_progress.set_fraction(indexed / total if total else 1.0)
            self.media_progress.set_text(f"Indexation : {indexed} validées · {pending} restantes")
            self.media_progress.set_visible(bool(total))
        elif event == "power":
            self.power_status.set_text(data["message"])
            self.power_status.set_visible(bool(data.get("active") or data.get("pending")))
        elif event == "error":
            self.terminal_event = True
            self.summary.set_text(data["message"])
            if data.get("recover"):
                self.controller.connection.update(recover=True, resume=data.get("resume"))
                self.connected()
        elif event == "done":
            self.terminal_event = True
            pending = data.get("media_pending", 0)
            status = (f" · {pending} chemin(s) à traiter via Réparer l’indexation." if pending else "")
            message = "Réparation MediaStore terminée." if data.get("repair") else "Synchronisation terminée."
            self.summary.set_text(message + status)

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
        sizes = [0, 0]
        deletions = 0
        counted = set()
        for row in self.entries:
            if row["kind"] == "blocked":
                counts["blocked"] += 1
            key = self.decisions.get(row["path"], "skip") if row["kind"] == "conflict" else "apply"
            for step in row["choices"].get(key, ()):
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
        self.summary.set_text(f"{stats.get('total_files', 0)} fichiers · {stats.get('transferable', 0)} transférables · "
                              f"{stats.get('blocked', 0)} incompatibles · {stats.get('conflicts', 0)} conflits\n"
                              f"{stats.get('corrected', 0)} chemins avec correspondance Android\n"
                              f"{counts['copy']} transfert(s) choisi(s) · {counts['rename']} renommage(s) · "
                              f"{counts['delete']} suppression(s) · {len(self.conflicts) - len(self.decisions)} conflit(s) ignoré(s)\n"
                              f"Vers Android {GLib.format_size(sizes[1])} · Vers le PC {GLib.format_size(sizes[0])}"
                              + (f" · {counts['blocked']} chemin(s) à corriger" if counts["blocked"] else ""))
        self.refresh_controls()

    def apply(self, _button):
        self.confirmed = False
        self.refresh_controls()
        data = json.dumps({"action": "apply", "conflicts": self.decisions,
                           "delete": self.deletions.get_active()}).encode() + b"\n"
        stream = self.process.get_stdin_pipe()
        stream.write_all_async(data, GLib.PRIORITY_DEFAULT, None, self.approval_sent)

    def approval_sent(self, stream, result):
        try:
            stream.write_all_finish(result)
        except GLib.Error as error:
            if not self.closed:
                self.summary.set_text(error.message)
            if self.process:
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

    def close_request(self, _window):
        self.closed = True
        self.controller.dialog = None
        if self.process:
            self.process.send_signal(signal.SIGTERM)
        return False
