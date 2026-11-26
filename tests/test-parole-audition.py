#!/usr/bin/python3 -B
"""Écoute bornée sur PCM local, sans connexion au serveur audio du bureau."""
from array import array
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import wave

from gi.repository import Gio, GLib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _parole_audition as review
from _kara_prepare import file_token
from _parole_index import document


class Audition(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.song = self.root / "titre.wav"
        self.owner = object()
        self.messages = []
        self.allowed = True
        self.audition = review.DraftAudition(lambda: self.allowed)
        self.addCleanup(self.audition.stop)
        with wave.open(str(self.song), "wb") as stream:
            stream.setparams((2, 2, review.RATE, 0, "NONE", "not compressed"))
            # Add a marker outside the protected interval, followed by a
            # different signal which the excerpt must contain exclusively.
            frames = array("h", [100, 100] * (review.RATE // 2) + [200, 200] * review.RATE)
            if sys.byteorder != "little":
                frames.byteswap()
            stream.writeframes(frames.tobytes())
        self.version = document(file_token(self.song))

    def start(self, report=None, **options):
        self.audition.start(self.owner, self.root, self.song, self.version, .5, .75,
                            report or self.messages.append, **options)

    def wait_for(self, action):
        loop = GLib.MainLoop()
        self.reported = False

        def report(value):
            self.messages.append(value)
            if value.endswith("."):
                self.reported = True
                loop.quit()

        def timeout():
            loop.quit()
            return GLib.SOURCE_CONTINUE

        source = GLib.timeout_add_seconds(5, timeout)
        try:
            action(report)
            if not self.reported:
                loop.run()
        finally:
            GLib.source_remove(source)
        self.assertTrue(self.reported, self.messages)

    def test_audio_decodes_only_the_requested_frames(self):
        sink = self.root / "pcm.raw"
        command = (sys.executable, "-c", "import sys; open(sys.argv[1], 'wb').write(sys.stdin.buffer.read())",
                   str(sink))
        with patch.object(review.DraftAudition, "player",
                          side_effect=lambda: Gio.Subprocess.new(command, Gio.SubprocessFlags.STDIN_PIPE)):
            self.wait_for(self.start)
        self.assertEqual(self.messages[-1], "Extrait terminé.")
        self.assertEqual(sink.read_bytes(), b"\xc8\x00\xc8\x00" * (review.RATE // 4))
        self.assertIsNone(self.audition.process)
        self.assertEqual(self.audition.timer, 0)

    def test_blocked_missing_stale_and_unmapped_previews(self):
        with patch.object(review.DraftAudition, "player") as player:
            self.allowed = False
            self.start()
            self.assertIn("au repos", self.messages[-1])
            self.allowed = True
            self.start(isolated=True)
            self.assertIn("indisponible", self.messages[-1])
            self.start()
            self.audition.stop(object())
            self.assertIsNotNone(self.audition.process)
            self.audition.stop(self.owner)
            self.assertIsNone(self.audition.process)
            player.assert_not_called()
            self.song.write_bytes(b"changed")
            self.start()
            self.assertIn("a changé", self.messages[-1])

    def test_playback_is_released_when_kara_takes_priority(self):
        command = (sys.executable, "-c", "import sys, time; sys.stdin.buffer.read(); time.sleep(4)")

        def start(report):
            def speak(value):
                if value.startswith("Écoute en cours"):
                    self.allowed = False
                report(value)

            self.start(speak)

        with patch.object(review.DraftAudition, "player",
                          side_effect=lambda: Gio.Subprocess.new(command, Gio.SubprocessFlags.STDIN_PIPE)):
            self.wait_for(start)
        self.assertEqual(self.messages[-1], "Écoute interrompue.")
        self.assertIsNone(self.audition.process)
        self.assertEqual(self.audition.timer, 0)

    def test_cached_voice_is_bound_to_the_current_source_and_model(self):
        signature, token = review.source_signature(self.song)
        folder = self.root / token
        folder.mkdir()
        voice = folder / "vocals.flac"
        subprocess.run(("ffmpeg", "-v", "error", "-i", str(self.song), str(voice)), check=True, timeout=5)
        processing = {"checkpoint": [1, 2]}
        (folder / "meta.json").write_text(document({"source": signature, "processing": processing,
                                                  "audio": "instrumental-fixture"}))
        with patch.object(review, "STEM_ROOT", self.root), patch.object(review, "model_signature",
                                                                       return_value=processing) as model:
            self.assertEqual(review.vocal_file(self.song), voice)
            model.return_value = {}
            self.assertIsNone(review.vocal_file(self.song))


if __name__ == "__main__":
    unittest.main()
