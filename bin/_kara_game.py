"""Envoie à Whisper un extrait du micro par ligne des paroles."""
from bisect import bisect_right
import json
import os
from math import isfinite
from pathlib import Path
import socket
import sys
import time

from gi.repository import Gio, GLib

from _kara_blackbox_turbo import CHUNK, receive
from _kara_scoring import (AudioWindow, MAX_PREPARED_WINDOW, SCORE_TOLERANCE,
                           prepared_window, score_breakdown, score_reference_line)
from _kara_words import LyricVocabulary, words


BIN = Path(__file__).resolve().parent
SESSION = BIN / "_kara_voice_session.py"
WORKER_PYTHON = Path.home() / ".local/share/hyprwhspr/venv/bin/python"


MAX_PENDING_LINES = 8


def timing_tolerance():
    try:
        value = float(os.environ.get("KARA_SCORE_TOLERANCE", SCORE_TOLERANCE))
        if isfinite(value) and 0 <= value <= 5:
            return value
    except ValueError:
        pass
    return SCORE_TOLERANCE


class GameScore:
    def __init__(self, changed, *, diagnostics=None):
        self.changed = changed
        self.diagnostics = diagnostics
        self.diagnostic_turn = {}
        self.capture_window = None
        self.turbo_channel = None
        self.network_socket = None
        self.process = None
        self.requested = False
        self.sequence = 0
        self.message = ""
        self.ready = False
        self.echo = False
        self.round_total = None
        self.reference = None
        self.tolerance = timing_tolerance()
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

    def set_round(self, lyrics=None, vocabulary=None, *, reference=None):
        self.reference = reference
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
        self.line_results = {}
        self.clear_timing()

    def clear_timing(self):
        self.cursor = None
        self.previous = None
        self.pending = {}
        self.pending_capture = {}
        self.epoch = 0.
        self.source_since = None
        self.seeked = False

    def mark_seek(self):
        self.seeked = True

    @property
    def breakdown(self):
        return score_breakdown(self.reference, self.line_results) if self.reference else None

    @property
    def score(self):
        if self.reference:
            return self.breakdown["score"]
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
        diagnostic_channel = None
        try:
            flags = Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE | Gio.SubprocessFlags.STDERR_SILENCE
            args = [sys.executable, "-B", str(SESSION), str(WORKER_PYTHON), "-B", str(BIN / "_kara_voice.py"),
                    "prepared" if self.reference else "turbo", self.language]
            if self.network_socket is None:
                process = Gio.Subprocess.new(args, flags)
            else:
                # The launcher owns the duplicate; the turn retains the
                # original so worker restarts do not change the input route.
                launcher = Gio.SubprocessLauncher.new(flags)
                launcher.take_fd(os.dup(self.network_socket.fileno()), 3)
                launcher.setenv("KARA_NETWORK_MIC_FD", "3", True)
                if self.reference and self.capture_window:
                    try:
                        diagnostic_channel, sender = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
                        diagnostic_channel.setblocking(False)
                        launcher.take_fd(sender.detach(), 4)
                        launcher.setenv("KARA_TURBO_TRACE_FD", "4", True)
                    except OSError:
                        if diagnostic_channel:
                            diagnostic_channel.close()
                            diagnostic_channel = None
                process = launcher.spawnv(args)
        except (GLib.Error, OSError):
            if diagnostic_channel:
                diagnostic_channel.close()
            self.requested = False
            self.message = "Le moteur Whisper local est indisponible."
            self.changed()
            return
        self.turbo_channel = diagnostic_channel
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
        if self.diagnostics:
            self.diagnostics.emit("turbo", "error", **self.diagnostic_turn, stage="voice")
        self.process = None
        self.ready = self.echo = False
        self.model = ""
        self.recording = False
        self.clear_timing()
        self.close_input(process)
        self.message = message
        self.changed()

    def close_input(self, process):
        if self.turbo_channel is not None:
            self.turbo_channel.close()
            self.turbo_channel = None
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
            if self.diagnostics and event.get("decoder"):
                self.diagnostics.emit("turbo", "decoder-ready", **self.diagnostic_turn, decoder=event["decoder"])
            self.message = ""
        if event.get("event") == "recording":
            self.recording = bool(event.get("active"))
        if event.get("event") == "recording-error":
            self.recording = False
            self.message = event.get("message", "Erreur de l’enregistrement local.")
        if event.get("event") in {"score", "skipped"}:
            if self.turbo_channel is not None:
                receive(self.turbo_channel, self.record_decode)
            self.pending_capture.pop(event.get("id"), None)
            batch = self.pending.pop(event.get("id"), None)
            if batch is not None and self.reference:
                self.match_reference(batch, event)
            elif batch is not None and event["event"] == "skipped":
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
        if begin is not None and self.reference:
            job.update(reference_id=self.reference["song_id"], reference_lrc=self.reference["lrc_sha256"],
                       reference_revision=self.reference["prepared_at"], tolerance=self.tolerance)
        try:
            self.process.get_stdin_pipe().write_all((json.dumps(job) + "\n").encode(), None)
        except GLib.Error:
            return False
        return True

    def record_decode(self, header, fd, size):
        window, call, offset = header
        admitted = self.pending_capture.get(window)
        if admitted is None:
            return
        session, context, begin, finish = admitted
        if not self.capture_window(context["turn"], begin, finish):
            return
        for cursor in range(0, size, CHUNK):
            block = os.pread(fd, min(CHUNK, size - cursor), cursor)
            self.diagnostics.pcm("turbo", 16000, 1, block, **context, recording_session=session,
                                 decode={"window": window, "call": call, "window_offset": offset,
                                         "first_frame": cursor // 2, "total_frames": size // 2})

    def send_job(self, window, begin, finish, source=None):
        self.sequence += 1
        self.pending[self.sequence] = window
        job = {"command": "score", "id": self.sequence, "line": window.line + 1,
               "begin": begin, "end": finish, "language": self.language}
        if self.reference:
            job["language"] = self.reference["language"]
            job.update(prepared=True, instrumental=bool(source), rate=window.rate, track_start=window.start,
                       track_end=window.stop, backing=source[0] if source else "",
                       original=source[1] if source else "")
            if source and len(source) == 4:
                job["guide"] = source[2] if any(
                    start < window.stop and (stop is None or stop > window.start)
                    for start, stop in source[3]) else ""
            if self.diagnostics and self.diagnostics.active():
                job["trace"] = True
                turn = self.diagnostic_turn.get("turn")
                if self.turbo_channel is not None and turn and self.capture_window(turn, begin):
                    session = self.diagnostics.voice_window("turbo", begin)
                    if session:
                        job["capture_decoder"] = True
                        self.pending_capture[self.sequence] = session, dict(self.diagnostic_turn), begin, finish
                job["queued_ns"] = time.monotonic_ns()
        else:
            # Only the comparison step receives the expected text. The legacy
            # decoder gets an empty prompt, just like the prepared decoder.
            job.update(prompt="", timed=True, expected=self.vocabulary.tokens[window.line],
                       word_times=[(time - window.start) / window.rate for time in window.word_times],
                       tolerance=self.tolerance / window.rate)
        try:
            self.process.get_stdin_pipe().write_all((json.dumps(job) + "\n").encode(), None)
        except GLib.Error:
            self.fail(self.process, "La capture du micro s’est arrêtée.")
            return False
        if self.diagnostics:
            self.diagnostics.emit("turbo", "window", **self.diagnostic_turn, window=self.sequence,
                                  position=window.start, line=window.line, capture_begin=begin,
                                  capture_end=finish, language=job["language"], rate=window.rate,
                                  track_end=window.stop, instrumental=bool(source), guide=bool(job.get("guide")),
                                  capture_decoder=bool(job.get("capture_decoder")))
        return True

    def match_reference(self, window, event):
        index = window.line
        lines = self.reference["lines"]
        source = event.get("source", "unverified")
        recognized = []
        if source == "voice":
            # Adjacent windows overlap. Assign a matching occurrence to the
            # nearest calibrated line before counting it or labeling it extra.
            nearby = range(max(0, index - 1), min(len(lines), index + 2))
            for item in event.get("heard", ()):
                point = window.start + item["start"] * window.rate
                if not window.start <= point <= window.stop:
                    continue
                distances = [(abs(point - word["start"]), i) for i in nearby for word in lines[i]["words"]
                             if word["start"] is not None and word["normalized"] == item["word"]]
                if distances and min(distances)[0] <= self.tolerance and min(distances)[1] != index:
                    continue
                if not distances and not lines[index]["display_start"] <= point < (
                        lines[index + 1]["display_start"] if index + 1 < len(lines) else self.reference["duration"]):
                    continue
                end = item.get("end")
                recognized.append({**item, "start": point,
                                   "end": window.start + end * window.rate if end is not None else None})
        audit = {} if self.diagnostics and self.diagnostics.active() else None
        self.line_results[index] = score_reference_line(lines[index], recognized, self.tolerance,
                                                        source=source, audit=audit)
        if audit is not None:
            self.diagnostics.emit("scoring", "line-decision", **self.diagnostic_turn,
                                  position=window.start, line=index, window=event.get("id"),
                                  raw_observed=event.get("heard", []), observed=recognized, source=source,
                                  tolerance=self.tolerance, reference=lines[index], decisions=self.line_results[index],
                                  comparison=audit,
                                  decoder=event.get("diagnostics"), inference_seconds=event.get("inference_seconds"),
                                  queue_wait_ns=event.get("queue_wait_ns"))
        if source == "backing":
            self.feedback = "Accompagnement dominant · ligne non évaluée"
        elif source == "unverified":
            self.feedback = "Source vocale indisponible · ligne non évaluée"
        elif source == "guide":
            self.feedback = "Voix originale présente · attribution incertaine"
        elif source in ("capture", "decoder"):
            self.feedback = ("Capture audio incomplète" if source == "capture" else "Décodage indisponible")
        elif source == "silence":
            self.feedback = "Aucune voix reconnue dans la fenêtre"
        else:
            self.feedback = ""

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

    def tick_round(self, times, duration, position, rate, now, word_times, word_ends, *,
                   end_of_song=False, source=None):
        if not self.ready or not times or rate <= 0:
            return
        if self.cursor is None:
            self.cursor = max(0, bisect_right(times, position) - 1)
        while self.cursor < len(times) and len(self.pending) < MAX_PENDING_LINES:
            index = self.cursor
            if index >= len(self.vocabulary.tokens):
                return
            if index in self.line_results or any(job.line == index for job in self.pending.values()):
                self.cursor += 1
                continue
            start = times[index]
            expected = self.vocabulary.tokens[index]
            calibrated = word_times[index] if word_times else ()
            ending = word_ends[index] if word_ends else None
            end = self.line_end(times, duration, index) if ending is None else ending
            if self.reference:
                if not expected:
                    self.cursor += 1
                    continue
                # Include measured words even if display uses a fallback line
                # onset, or the last syllable was held beyond a short line.
                window = prepared_window(index, start, end, duration, rate, calibrated, self.tolerance)
                if window is None:
                    self.cursor += 1
                    continue
                head, tail = window.start, window.stop
            else:
                limit = times[index + 1] if index + 1 < len(times) else duration or end
                end = min(end, limit)
                if duration:
                    end = min(end, duration)
                if not expected or end <= max(0, start):
                    self.cursor += 1
                    continue
                head, tail = max(0, start - self.tolerance), end + self.tolerance
                if duration:
                    tail = min(tail, duration)
            if not end_of_song and position < tail:
                return
            self.cursor += 1
            if tail <= head or (end_of_song and head >= position):
                continue
            if self.reference:
                earliest_word = max(0., next(x for x in calibrated if x is not None) - self.tolerance)
                if earliest_word < self.epoch - .02 or tail > head + MAX_PREPARED_WINDOW:
                    self.match_reference(AudioWindow(index, head, tail, rate), {"source": "unverified"})
                    continue
                head = max(head, self.epoch)
                if self.source_since is not None and earliest_word >= self.source_since - .02:
                    head = max(head, self.source_since)
            begin = now + (head - position) / rate
            finish = now + (tail - position) / rate
            trusted = (source if self.source_since is not None and head >= self.source_since - .02
                       and abs(rate - 1) < .001 else None)
            if not self.send_job(AudioWindow(index, head, tail, rate, calibrated), begin, finish, trusted):
                return

    def flush_round(self, times, duration, now, word_times=(), word_ends=(), rate=1.0, source=None):
        if duration and self.round_total is not None and self.cursor is not None:
            self.tick_round(times, duration, duration, rate, now, word_times, word_ends,
                            end_of_song=True, source=source)

    def tick(self, times, duration, position, rate, now, word_times=(), word_ends=(), *, source=None):
        previous = self.previous
        self.previous = (position, rate, now)
        if not self.ready or not times:
            return
        if self.reference:
            # An epoch is a continuous mapped piece of the playback clock.
            # Completed windows stay usable across pause/seek. A window
            # spanning the discontinuity cannot yield a claimed phrase onset.
            if previous is None and rate > 0:
                self.epoch = 0. if position < .6 else position
            if self.seeked:
                self.cursor = None
                self.epoch = position
                self.source_since = None
                self.seeked = False
            elif previous is not None:
                old_position, old_rate, old_time = previous
                if rate != old_rate or abs(position - old_position - old_rate * (now - old_time)) > .65:
                    self.cursor = None
                    self.epoch = position
                    self.source_since = None
            if source is None or rate <= 0:
                self.source_since = None
            elif self.source_since is None:
                self.source_since = max(self.epoch, position)
        elif previous is not None:
            old_position, old_rate, old_time = previous
            if abs(position - old_position - old_rate * (now - old_time)) > .65:
                self.reset()
                self.previous = (position, rate, now)
                self.changed()
        if rate <= 0:
            self.cursor = None
            return
        self.tick_round(times, duration, position, rate, now, word_times, word_ends, source=source)

    @staticmethod
    def line_end(times, duration, index):
        start = times[index]
        if index + 1 < len(times):
            following = times[index + 1]
        else:
            following = duration - 0.18 if duration > start + 0.5 else start + 2.5
        return min(start + 8.0, following)
