#!/usr/bin/python3 -B
"""Parole GTK and real Labfy IPC with temporary libraries and simulated ADB."""

from contextlib import ExitStack
import os
from pathlib import Path
import runpy
import shutil
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
LABFY = Path(os.environ.get("LABFY_MUSIC_MANAGER") or ROOT.parent / "labfy-music-manager").resolve()


def wait_for(predicate):
    from gi.repository import GLib

    context = GLib.MainContext.default()
    until = monotonic() + 25
    while monotonic() < until:
        while context.pending():
            context.iteration(False)
        if predicate():
            return
        sleep(0.02)
    raise AssertionError("GTK n’a pas atteint l’état attendu.")


@unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "Session GTK isolée requise")
class SyncTests(unittest.TestCase):
    def test_header_preview_and_guarded_sync(self):
        sys.path.insert(0, str(ROOT / "bin"))
        with ExitStack() as resources:
            base = Path(resources.enter_context(TemporaryDirectory(prefix="parole-sync-")))
            pc, device, labfy = base / "pc", base / "device", base / "labfy"
            phone = device / "storage/Music"
            for directory in (pc, phone, device / "shell", labfy / "scripts", labfy / "build", base / "bin"):
                directory.mkdir(parents=True)
            # The production CLI and engine run as subprocesses. An executable
            # marker suffices for the helper, whose calls fake-adb emulates.
            shutil.copytree(LABFY / "core/labfy_sync", labfy / "core/labfy_sync", ignore=shutil.ignore_patterns("__pycache__"))
            shutil.copy2(LABFY / "scripts/labfy-sync", labfy / "scripts/labfy-sync")
            fake_adb = LABFY / "tests/fixtures/fake-adb"
            (base / "bin/adb").symlink_to(fake_adb)
            (labfy / "build/rename-android-arm64").symlink_to(fake_adb)
            resources.enter_context(patch.dict(os.environ, {
                "PATH": f"{base / 'bin'}:{os.environ['PATH']}", "LABFY_TEST_DEVICE": str(device),
                "LABFY_MUSIC_MANAGER": str(labfy), "LABFY_ANDROID_ROOT": "/storage/emulated/0/Music",
                "KARA_MUSIC_LIBRARY": str(pc), "KARA_SILENT": "1", "XDG_CONFIG_HOME": str(base / "config"),
                "XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
                "XDG_CACHE_HOME": str(pc / "cache"),
            }))
            offline = device / "offline"
            offline.touch()
            source = runpy.run_path(str(ROOT / "bin/paroles-strawberry"), run_name="parole_sync_fixture")
            from gi.repository import Gio, Gtk
            from _musique_oz import shared_mutation
            from _parole_artist_groups import apply, preview

            app = Gtk.Application(application_id="fr.labfy.TestParoleSync", flags=Gio.ApplicationFlags.NON_UNIQUE)
            self.assertTrue(app.register(None))
            with patch.object(source["Maintenance"], "start"):
                window = source["LyricsWindow"](app)
                resources.callback(window.destroy)
                resources.callback(window.close_window, window)
                window.open_parole()

            sync = window.music_sync
            header = window.get_titlebar()
            wait_for(lambda: sync.connection.get("state") in {"absent", "blocked"} and sync.button.get_width() > 0)

            def children(widget):
                yield widget
                child = widget.get_first_child()
                while child is not None:
                    yield from children(child)
                    child = child.get_next_sibling()

            import_label = next(widget for widget in children(header) if isinstance(widget, Gtk.Label)
                                and widget.get_text() == "Importer des albums")
            import_button = import_label.get_ancestor(Gtk.Button)
            positions = []
            for button in (import_button, sync.button, window.parole_ui.tv):
                found, bounds = button.compute_bounds(header)
                self.assertTrue(found)
                positions.append(bounds.get_x())
            self.assertTrue(positions[0] < positions[1] < positions[2], positions)
            self.assertEqual(sync.button.get_child().get_last_child().get_text(), "Synchroniser")
            self.assertTrue(sync.button.has_css_class("sync-unavailable"))
            self.assertIn(sync.connection["message"], sync.button.get_tooltip_text())
            sync.button.emit("clicked")
            self.assertIsNone(sync.dialog.process)
            self.assertFalse(sync.dialog.preview_button.get_sensitive())
            sync.dialog.close()

            album = Path("Ancien/Album")
            track = album / "Piste.flac"
            cover = album / "cover.jpg"
            lyrics = track.with_suffix(".lrc")
            removed = Path("Piste conservée.opus")
            assets = {track: b"audio et tags fictifs", cover: b"pochette", lyrics: b"[00:01.000]Paroles",
                      removed: b"exemplaire Android a conserver"}
            for library in (pc, phone):
                (library / album).mkdir(parents=True)
                for path, data in assets.items():
                    (library / path).write_bytes(data)
            for path in ("A_trier/.kara-reprise/draft.opus", ".cache/demucs/voice.wav", ".state/blackbox/frame.jpg"):
                (pc / path).parent.mkdir(parents=True, exist_ok=True)
                (pc / path).write_bytes(b"travail interne")
            (pc / track.with_suffix(".txt")).write_text("Paroles locales en texte seul")
            offline.unlink()
            sync.poll()
            wait_for(lambda: sync.connection.get("state") == "ready")
            self.assertTrue(sync.button.has_css_class("sync-ready"))
            self.assertIsNone(sync.dialog)

            def open_preview():
                sync.button.emit("clicked")
                dialog = sync.dialog
                if dialog.process is None:
                    dialog.preview_button.emit("clicked")
                wait_for(dialog.confirm.get_sensitive)
                return dialog

            def confirm(dialog):
                dialog.confirm.emit("clicked")
                wait_for(lambda: dialog.process is None)

            dialog = open_preview()
            self.assertFalse(dialog.entries)
            confirm(dialog)
            dialog.close()

            (pc / "Conservé").mkdir()
            index = window.maintenance.index
            apply(index, preview(pc, "Conservé", ["Ancien"]), state=base / "state")
            (pc / removed).unlink()
            new = Path("Nouvelle piste.opus")
            (pc / new).write_bytes(b"nouvelle piste")
            dialog = open_preview()
            self.assertEqual(len(dialog.sections["Renommages"]), 3)
            self.assertIn("Suppressions proposées", dialog.sections)
            self.assertFalse(dialog.deletions.get_active())
            self.assertFalse((phone / new).exists())
            self.assertTrue((phone / track).exists())

            with shared_mutation(pc, wait=True):
                confirm(dialog)
                self.assertIn("bibliothèque est déjà utilisée", dialog.summary.get_text())
                self.assertFalse((phone / new).exists())
                self.assertTrue((phone / track).exists())
            wait_for(lambda: sync.probe is None)
            dialog.preview_button.emit("clicked")
            wait_for(dialog.confirm.get_sensitive)
            confirm(dialog)
            self.assertEqual(dialog.file_progress.get_fraction(), 1)
            self.assertEqual((phone / new).read_bytes(), (pc / new).read_bytes())
            for original in (track, cover, lyrics):
                final = Path("Conservé/Album") / original.name
                self.assertEqual((phone / final).read_bytes(), assets[original])
                self.assertFalse((phone / original).exists())
            self.assertEqual((pc / "Conservé/Album/Piste.txt").read_text(), "Paroles locales en texte seul")
            self.assertTrue((phone / removed).exists())
            self.assertFalse((pc / removed).exists())
            self.assertFalse((phone / "A_trier").exists())
            dialog.close()

            updated = Path("Conservé/Album/cover.jpg")
            (pc / updated).write_bytes("pochette actualisée".encode())
            dialog = open_preview()
            self.assertIn("Modifications", dialog.sections)
            confirm(dialog)
            self.assertEqual((phone / updated).read_bytes(), (pc / updated).read_bytes())
            dialog.close()

            # A stale Android directory that has no counterpart in the USB
            # baseline must not recreate the album under the former artist.
            (phone / track).parent.mkdir(parents=True)
            (phone / track).write_bytes(assets[track])
            dialog = open_preview()
            dialog.confirm.emit("clicked")
            self.assertIn("Regroupement à préserver", dialog.summary.get_text())
            self.assertFalse((pc / track).exists())
            dialog.cancel_button.emit("clicked")
            wait_for(lambda: dialog.process is None)
            dialog.close()
            (phone / track).unlink()

            inbox_file = pc / "A_trier/Audio en attente.opus"
            inbox_file.write_bytes(b"hors collection")
            cache_file = pc / "cache/paroles-strawberry/stems/voice.wav"
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_bytes(b"cache hors collection")
            publishable = Path("Paroles prêtes.lrc")
            (pc / publishable).write_text("[00:01.00] Paroles publiées", encoding="utf-8")
            dialog = open_preview()
            self.assertEqual({row["path"] for row in dialog.entries}, {publishable.as_posix(), removed.as_posix()})
            self.assertFalse(dialog.deletions.get_active())
            confirm(dialog)
            self.assertEqual((phone / publishable).read_bytes(), (pc / publishable).read_bytes())
            self.assertFalse((phone / "A_trier").exists())
            self.assertFalse((phone / "cache").exists())
            dialog.close()
            inbox_file.unlink()
            cache_file.unlink()

            retry = Path("Piste à reprendre.opus")
            (pc / retry).write_bytes(b"contenu complet apres reprise")
            dialog = open_preview()
            unplug = device / "unplug-on-transfer"
            unplug.touch()
            confirm(dialog)
            self.assertFalse((phone / retry).exists())
            self.assertNotIn("Fichier transféré · Android · " + retry.name,
                             [entry[-1] for entry in window.maintenance.journal.snapshot()[1]])
            dialog.close()
            wait_for(lambda: sync.connection.get("state") in {"absent", "blocked"})
            unplug.unlink()
            offline.unlink()
            (device / "transport").write_text("12")
            sync.poll()
            wait_for(lambda: sync.connection.get("state") == "ready" and sync.connection.get("resume"))
            sync.button.emit("clicked")
            dialog = sync.dialog
            self.assertIsNone(dialog.process)
            self.assertFalse((phone / retry).exists())
            self.assertTrue(dialog.preview_button.get_sensitive(), dialog.connection_label.get_text())
            dialog.preview_button.emit("clicked")
            wait_for(lambda: dialog.process is None)
            self.assertEqual(dialog.summary.get_text(), "Synchronisation terminée.")
            self.assertEqual((phone / retry).read_bytes(), (pc / retry).read_bytes())
            self.assertFalse((device / "mock-power.json").exists())
            dialog.close()

            console = window.parole_ui.console
            categories = console.category.get_model()
            console.category.set_selected(next(i for i in range(categories.get_n_items())
                                               if categories.get_string(i) == "SYNC"))
            lines = [console.stringify(entry) for entry in console.lines]
            for fragment in ("Aperçu prêt", "Fichier transféré · Android · Nouvelle piste.opus",
                             "Fichier actualisé · Android · Conservé/Album/cover.jpg", "Fichier renommé",
                             "[ERROR] La bibliothèque est déjà utilisée", "Bilan de la session",
                             "Copie vérifiée · Android · Piste à reprendre.opus"):
                self.assertTrue(any(fragment in line for line in lines), fragment)
            self.assertTrue(all("[SYNC]" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
