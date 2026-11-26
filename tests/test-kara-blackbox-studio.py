"""BlackBox Studio smoke test with a private synthesized session."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
from types import SimpleNamespace
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_blackbox import BlackBox
from _kara_blackbox_preview import tiles, read_json


class StudioTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.box = BlackBox(Path(temp.name) / "blackbox")
        self.box.start(consent=True, voice=True, tracks=["wifi"])
        context = {"turn": "a" * 16, "player": "Fixture", "song": "Synthétique"}
        self.box.emit("party", "turn-start", **context)
        self.box.pcm("wifi", 48000, 1, (b"\x00\x40\x00\xc0") * 480, **context)
        self.box.mark("problem", "fixture", **context)
        self.box.emit("party", "turn-end", **context)
        self.box.stop(wait=True)
        self.folder = self.box.root / self.box.status()["latest"]["session"]

    def test_waveforms_have_bounded_values_and_preserve_gaps(self):
        result = tiles(self.folder, None, read_json(self.folder / "manifest.json")["monotonic_ns"], 0, 2)
        peaks = result["peaks"]["wifi"]
        self.assertIn((-.5, .5), peaks)
        self.assertIn((None, None), peaks)
        self.assertTrue(any(row["event"] == "marker" for row in result["events"]))

    @unittest.skipUnless(os.environ.get("PAROLE_TEST_GTK") == "1", "requires isolated GTK display")
    def test_studio_displays_session_and_timeline(self):
        import gi
        gi.require_version("Gtk", "4.0")
        gi.require_foreign("cairo")
        from gi.repository import GLib, Gtk
        from _kara_blackbox_studio import BlackBoxStudio

        Gtk.init()
        owner = Gtk.Window()
        owner.blackbox = self.box
        owner.tag_reader = self.enterContext(ThreadPoolExecutor(2))
        owner.blackbox_preflight = lambda: {"Paire AEC": "À mesurer"}
        owner.toggle_blackbox = lambda *_args, **_options: None
        owner.game_score = SimpleNamespace(diagnostic_turn={})
        owner.base_position = owner.updated = owner.rate = 0.
        studio = BlackBoxStudio(owner)
        self.addCleanup(studio.destroy)
        self.addCleanup(owner.destroy)

        def until(predicate):
            deadline = monotonic() + 3
            context = GLib.MainContext.default()
            while not predicate() and monotonic() < deadline:
                while context.pending():
                    context.iteration(False)
                sleep(.01)
            self.assertTrue(predicate())

        studio.show_studio()
        until(lambda: studio.path == self.folder)
        self.assertEqual(studio.pages.get_n_pages(), 4)
        studio.pages.set_current_page(2)
        studio.refresh_timeline()
        until(lambda: studio.preview is not None)
        self.assertIn("wifi", studio.preview["peaks"])
        studio.verify()
        until(lambda: studio.error.get_text().startswith("Intégrité vérifiée"))


if __name__ == "__main__":
    unittest.main()
