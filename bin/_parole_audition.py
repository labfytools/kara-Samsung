"""Lecture d’un passage privé sans modifier le lecteur Strawberry."""
from math import isfinite
from time import monotonic

from gi.repository import Gio, GLib

from _kara_prepare import file_token
from _kara_stems import ROOT as STEM_ROOT, model_signature, read_metadata, source_signature
from _parole_index import document

RATE = 48_000
FRAME_BYTES = 4
MAX_SECONDS = 12


def vocal_file(song):
    try:
        identity, token = source_signature(song)
        directory = STEM_ROOT / token
        data = read_metadata(directory)
        voice = directory / "vocals.flac"
        # An audition uses only completed generations with current source
        # and model receipts. It never starts Demucs from the editor.
        if (data.get("source") == identity and data.get("audio") and data.get("processing") == model_signature()
                and voice.resolve(strict=True) == voice and voice.is_file() and voice.stat().st_size > 4096):
            return voice
    except (OSError, TypeError, ValueError, AttributeError):
        pass
    return None


class DraftAudition:
    def __init__(self, permitted):
        self.permitted = permitted
        self.process = None
        self.owner = None
        self.timer = 0

    def stop(self, owner=None):
        if owner is not None and self.owner is not owner:
            return
        # Clear ownership before terminating. The old async replies may be
        # delivered after another passage or window has taken the slot.
        process, self.process = self.process, None
        self.owner = None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = 0
        if process is not None:
            process.force_exit()

    def watch(self, deadline, report):
        allowed = self.permitted()
        if allowed and monotonic() < deadline:
            return GLib.SOURCE_CONTINUE
        self.timer = 0
        self.stop()
        report("Délai de lecture dépassé." if allowed else "Écoute interrompue.")
        return GLib.SOURCE_REMOVE

    def start(self, owner, root, song, expected, start, end, report, *, isolated=False):
        self.stop()
        if not self.permitted():
            report("L’écoute est disponible lorsque Kara et Strawberry sont au repos.")
            return
        if not (isfinite(start) and isfinite(end) and 0 <= start < end <= 3600):
            report("Passage audio invalide.")
            return

        def original():
            try:
                return (song.is_relative_to(root) and song.resolve(strict=True) == song
                        and document(file_token(song)) == expected)
            except (OSError, ValueError):
                return False

        if not original():
            report("Le fichier a changé depuis l’ouverture du brouillon.")
            return
        source = vocal_file(song) if isolated else song
        if source is None:
            report("Voix séparée indisponible pour cette version.")
            return
        seconds = min(end - start, MAX_SECONDS)
        # FFmpeg decodes a bounded, fixed-format PCM interval over a private
        # pipe. No original metadata or source URI reaches the audio sink.
        command = ("ffmpeg", "-v", "error", "-xerror", "-nostdin", "-protocol_whitelist", "file,pipe",
                   "-ss", f"{start:.6f}", "-threads", "1", "-i", str(source), "-t", f"{seconds:.6f}",
                   "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "2", "-ar", str(RATE),
                   "-f", "s16le", "pipe:1")
        try:
            self.process = Gio.Subprocess.new(command, Gio.SubprocessFlags.STDOUT_PIPE |
                                               Gio.SubprocessFlags.STDERR_SILENCE)
        except GLib.Error:
            report("Décodeur audio indisponible.")
            return
        self.owner = owner
        self.timer = GLib.timeout_add(150, self.watch, monotonic() + seconds + 8, report)
        report("Ouverture de la voix isolée…" if isolated else "Ouverture du mix…")
        self.process.communicate_async(None, None, self.decoded, (original, seconds, report))

    def decoded(self, process, result, data):
        original, seconds, report = data
        try:
            _, pcm, _ = process.communicate_finish(result)
        except GLib.Error:
            pcm = None
        if process is not self.process:
            return
        size = pcm.get_size() if pcm is not None else 0
        if (not 0 < size <= (MAX_SECONDS * RATE + 1) * FRAME_BYTES
                or size % FRAME_BYTES or not process.get_successful()):
            self.stop()
            report("Extrait audio illisible.")
            return
        if not self.permitted() or not original():
            self.stop()
            report("Écoute annulée. Le lecteur ou le fichier a changé.")
            return
        try:
            self.process = self.player()
        except GLib.Error:
            self.stop()
            report("Sortie audio indisponible.")
            return
        report(f"Écoute en cours · {seconds:g} s")
        self.process.communicate_async(pcm, None, self.played, report)

    @staticmethod
    def player():
        return Gio.Subprocess.new(("pw-play", "--raw", "--rate", str(RATE), "--channels", "2",
                                   "--format", "s16", "--properties", "media.name=Extrait-Parole", "-"),
                                  Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDERR_SILENCE)

    def played(self, process, result, report):
        try:
            process.communicate_finish(result)
            succeeded = process.get_successful()
        except GLib.Error:
            succeeded = False
        if process is self.process:
            self.stop()
            report("Extrait terminé." if succeeded else "Sortie audio indisponible.")
