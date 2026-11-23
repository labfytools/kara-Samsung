"""Envoie à Whisper un extrait du micro par ligne des paroles."""
from bisect import bisect_right
import json
from pathlib import Path
import sys
import time

from gi.repository import Gio, GLib

from _kara_scoring import AudioWindow, SCORE_TOLERANCE
from _kara_words import LyricVocabulary, words


BIN = Path(__file__).resolve().parent
SESSION = BIN / "_kara_voice_session.py"
WORKER_PYTHON = Path.home() / ".local/share/hyprwhspr/venv/bin/python"


MAX_PENDING_LINES = 8


class GameScore:
    def __init__(self, changed):
        self.changed = changed
        self.process = None
        self.requested = False
        self.sequence = 0
        self.message = ""
        self.ready = False
        self.echo = False
        self.round_total = None
        self.vocabulary = LyricVocabulary(())
        self.language = ""
        self.model = ""
        self.recording = False
        self.reset()

    def configure_voice(self, lyrics, vocabulary=None):
        vocabulary = vocabulary if vocabulary is not None else LyricVocabulary(lyrics)
        if vocabulary.language != self.language and self.requested:
            self.pause()
        if vocabulary.lines != self.vocabulary.lines:
            self.reset()
        self.vocabulary, self.language = vocabulary, vocabulary.language

    def set_round(self, lyrics=None, vocabulary=None):
        if lyrics is not None:
            self.configure_voice(lyrics, vocabulary)
        self.round_total = sum(len(words(line)) for line in lyrics) if lyrics is not None else None
        self.reset()

    def reset(self):
        self.total = self.points = 0
        self.feedback = ""
        self.credited_words = set()
        self.word_evidence = {}
        self.scored_lines = set()
        self.clear_timing()

    def clear_timing(self):
        self.cursor = None
        self.previous = None
        self.pending = {}

    @property
    def score(self):
        if self.round_total is not None:
            if not self.points or not self.round_total:
                return 0
            return min(100, max(1, 100 * self.points // self.round_total))
        return round(100 * self.points / self.total) if self.total else None

    def start(self):
        if self.requested:
            return
        self.requested = True
        self.message = "Préparation du modèle local…"
        try:
            flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            process = Gio.Subprocess.new(
                [sys.executable, "-B", str(SESSION), str(WORKER_PYTHON), "-B", str(BIN / "_kara_voice.py"),
                 "turbo", self.language],
                flags,
            )
        except GLib.Error:
            self.message = "Le moteur Whisper local est indisponible."
            self.changed()
            return
        self.process = process
        self.changed()
        stream = Gio.DataInputStream.new(process.get_stdout_pipe())
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read, process)

    def stop(self):
        self.reset()
        self.pause()

    def pause(self):
        if not self.requested:
            return
        self.requested = False
        process = self.process
        self.process = None
        self.ready = self.echo = False
        self.model = ""
        self.recording = False
        self.clear_timing()
        self.message = ""
        if process is not None:
            self.close_input(process)
        self.changed()

    def fail(self, process, message):
        self.process = None
        self.ready = self.echo = False
        self.model = ""
        self.recording = False
        self.clear_timing()
        self.close_input(process)
        self.message = message
        self.changed()

    def close_input(self, process):
        try:
            process.get_stdin_pipe().close(None)
        except GLib.Error:
            pass
        process.wait_async(None, self.reaped)

    @staticmethod
    def reaped(process, result):
        try:
            process.wait_finish(result)
        except GLib.Error:
            pass

    def read(self, stream, result, process):
        if self.process is not process:
            return
        try:
            line, _ = stream.read_line_finish_utf8(result)
            event = json.loads(line) if line else None
        except (GLib.Error, ValueError):
            event = None
        if not isinstance(event, dict) or event.get("event") == "error":
            detail = event.get("message") if isinstance(event, dict) else None
            self.fail(process, detail or "Le moteur de score s’est arrêté.")
            return
        if event.get("event") == "status":
            self.message = event["message"]
        if event.get("event") == "ready":
            self.ready = True
            self.echo = bool(event.get("echo"))
            self.model = event.get("model", "")
            self.message = ""
        if event.get("event") == "recording":
            self.recording = bool(event.get("active"))
        if event.get("event") == "recording-error":
            self.recording = False
            self.message = event.get("message", "Erreur de l’enregistrement local.")
        if event.get("event") in {"score", "skipped"}:
            batch = self.pending.pop(event.get("id"), None)
            if batch is not None and event["event"] == "skipped":
                self.feedback = "Capture audio incomplète"
            elif batch is not None:
                heard, hits = self.match_window(batch, event)
                self.points += hits
                if hits:
                    self.feedback = ""
                elif heard:
                    self.feedback = "Aucun mot du passage reconnu"
                else:
                    self.feedback = ("Signal du micro faible" if event.get("signal") is False
                                     else "Aucune parole reconnue")
        self.changed()
        stream.read_line_async(GLib.PRIORITY_DEFAULT, None, self.read, process)

    def record_turn(self, begin=None, song=""):
        if self.process is None or not self.ready:
            return False
        job = ({"command": "record-stop"} if begin is None else
               {"command": "record-start", "begin": begin, "song": song})
        try:
            self.process.get_stdin_pipe().write_all((json.dumps(job) + "\n").encode(), None)
        except GLib.Error:
            return False
        return True

    def send_job(self, window, begin, finish):
        self.sequence += 1
        self.pending[self.sequence] = window
        job = {"command": "score", "id": self.sequence, "line": window.line + 1, "begin": begin, "end": finish,
               "language": self.language, "prompt": self.vocabulary.prompt(window.line, window.line),
               "timed": True, "expected": self.vocabulary.tokens[window.line],
               "word_times": [(time - window.start) / window.rate for time in window.word_times],
               "tolerance": SCORE_TOLERANCE / window.rate}
        try:
            self.process.get_stdin_pipe().write_all((json.dumps(job) + "\n").encode(), None)
        except GLib.Error:
            self.fail(self.process, "La capture du micro s’est arrêtée.")
            return False
        return True

    def match_window(self, window, event):
        expected = self.vocabulary.tokens[window.line]
        heard = window.observed(event.get("spans", ()), words)
        if window.line not in self.scored_lines:
            self.scored_lines.add(window.line)
            self.total += len(expected)
        recognized = event.get("word_evidence")
        if recognized is None:
            evidence = window.evidence(expected, heard)
        else:
            evidence = ((index, window.start + point * window.rate) for index, point in recognized)
        adjacent = [item for line in (window.line - 1, window.line + 1)
                    for item in self.word_evidence.get(line, ())]
        recorded = self.word_evidence.setdefault(window.line, [])
        fresh = 0
        for index, point in evidence:
            key = window.line, index
            if not 0 <= index < len(expected) or key in self.credited_words:
                continue
            if not window.start <= point <= window.stop:
                continue
            word = expected[index]
            if any(word == other and abs(point - stamp) <= .24 for other, stamp in adjacent):
                continue
            self.credited_words.add(key)
            recorded.append((word, point))
            fresh += 1
        return len(heard), fresh

    def tick_round(self, times, duration, position, rate, now, word_times, word_ends, *, end_of_song=False):
        if not self.ready or not times:
            return
        if self.cursor is None:
            self.cursor = max(0, bisect_right(times, position) - 1)
        while self.cursor < len(times) and len(self.pending) < MAX_PENDING_LINES:
            index = self.cursor
            if index >= len(self.vocabulary.tokens):
                return
            start = times[index]
            expected = self.vocabulary.tokens[index]
            calibrated = word_times[index] if word_times else ()
            ending = word_ends[index] if word_ends else None
            end = self.line_end(times, duration, index) if ending is None else ending
            limit = times[index + 1] if index + 1 < len(times) else duration or end
            end = min(end, limit)
            if duration:
                end = min(end, duration)
            if not expected or end <= max(0, start):
                self.cursor += 1
                continue
            head = max(0, start - SCORE_TOLERANCE)
            tail = end + SCORE_TOLERANCE
            if duration:
                tail = min(tail, duration)
            if not end_of_song and position < tail:
                return
            self.cursor += 1
            if tail <= head or (end_of_song and head >= position):
                continue
            begin = now + (head - position) / rate
            finish = now + (tail - position) / rate
            if not self.send_job(AudioWindow(index, head, tail, rate, calibrated), begin, finish):
                return

    def flush_round(self, times, duration, now, word_times=(), word_ends=(), rate=1.0):
        if duration and self.round_total is not None and self.cursor is not None:
            self.tick_round(times, duration, duration, rate, now, word_times, word_ends, end_of_song=True)

    def tick(self, times, duration, position, rate, now, word_times=(), word_ends=()):
        previous = self.previous
        self.previous = (position, rate, now)
        if not self.ready or not times:
            return
        if previous is not None:
            old_position, old_rate, old_time = previous
            if abs(position - old_position - old_rate * (now - old_time)) > 0.65:
                if self.round_total is None:
                    self.reset()
                else:
                    self.clear_timing()
                self.previous = (position, rate, now)
                self.changed()
        if rate <= 0:
            self.cursor = None
            return
        self.tick_round(times, duration, position, rate, now, word_times, word_ends)

    @staticmethod
    def line_end(times, duration, index):
        start = times[index]
        if index + 1 < len(times):
            following = times[index + 1]
        else:
            following = duration - 0.18 if duration > start + 0.5 else start + 2.5
        return min(start + 8.0, following)
