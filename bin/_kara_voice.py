#!/usr/bin/python3 -B
"""Capture et transcription locale déclenchées par le mode jeu."""
from collections import deque
from dataclasses import dataclass
from hashlib import sha256
import json
import os
from pathlib import Path
from queue import Full, Queue
import re
from select import select
import struct
import subprocess
import sys
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
import time

import numpy as np
import socket
import struct
from pywhispercpp.model import Model

from _kara_capture import TurnCapture
from _kara_blackbox_turbo import submit as submit_turbo_input
from _kara_dtw_words import read_timing
from _kara_scoring import AudioWindow, SCORE_TOLERANCE, add_evidence, same_word
from _kara_words import IGNORED_HINTS, words
from _kara_turbo import TurboDecoder, build_decoder


RATE = 16_000
BLOCK = RATE // 50
CAPTURE_LATENCY = .075
DATA = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local/share"))
BASE_MODEL = DATA / "kara/models/ggml-base-q5_1.bin"
TURBO_MODEL = DATA / "pywhispercpp/models/ggml-large-v3-turbo.bin"
VOICE_FLOOR = 0.0015
QUIET_VOICE_FLOOR = .00055
OUTPUT = Lock()


def emit(event, **fields):
    with OUTPUT:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)


def pulse(*args):
    return subprocess.check_output(("pactl", *args), text=True, timeout=2).strip()


def stop_process(process):
    if process is not None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                stream.close()


def echo_filter(microphone):
    name = f"kara-micro-{os.getpid()}"
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", microphone):
        return None, microphone
    try:
        process = subprocess.Popen(("pw-cli",), stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, text=True)
    except OSError:
        return None, microphone
    try:
        process.stdin.write("load-module libpipewire-module-echo-cancel "
                            "{ monitor.mode=true "
                            "aec.args={ webrtc.noise_suppression=false webrtc.voice_detection=false } "
                            f"source.props={{ node.name={name} priority.session=1 node.virtual=true }} "
                            f'capture.props={{ target.object="{microphone}" }} }}\n')
        process.stdin.flush()
        for _ in range(12):
            sources = pulse("list", "short", "sources")
            if any(line.split()[1:2] == [name] for line in sources.splitlines()):
                return process, name
            if process.poll() is not None:
                break
            time.sleep(0.12)
    except (OSError, subprocess.SubprocessError):
        pass
    stop_process(process)
    return None, microphone


class Microphone:
    def __init__(self, stream):
        self.stream = stream
        self.started = Event()
        self.blocks = deque(maxlen=45 * RATE // BLOCK)
        self.lock = Lock()
        self.capture = None

    def read(self):
        previous = None
        while block := self.stream.read(BLOCK * 2):
            duration = len(block) / (2 * RATE)
            end = time.monotonic() - CAPTURE_LATENCY
            if previous is not None:
                expected = previous + duration
                if end < expected + .12:
                    correction = max(-.002, min(.002, .08 * (end - expected)))
                    end = expected + correction
            previous = end
            with self.lock:
                self.blocks.append((end, block))
                if self.capture is not None:
                    try:
                        self.capture.append(end, block)
                    except OSError:
                        self.close_capture()
                        emit("recording-error", message="Impossible de continuer l’enregistrement local.")
            self.started.set()
        emit("error", message="La capture du micro s’est arrêtée.")

    def read_network(self):
        # A datagram is one ingress block with its position already expressed
        # on the server monotonic clock. Gaps stay visible in samples().
        while message := self.stream.recv(649):
            if len(message) not in (328, 648):
                continue
            end, = struct.unpack_from("<d", message)
            with self.lock:
                self.blocks.append((end, message[8:]))
                if self.capture is not None:
                    try:
                        self.capture.append(end, message[8:])
                    except OSError:
                        self.close_capture()
            self.started.set()

    def record(self, begin):
        with self.lock:
            if self.capture is None:
                self.capture = TurnCapture(begin, RATE)
                try:
                    for end, block in self.blocks:
                        self.capture.append(end, block)
                except OSError:
                    self.close_capture()
                    raise
            return self.capture.path

    def close_capture(self):
        capture, self.capture = self.capture, None
        if capture is not None:
            try:
                capture.close()
            except OSError:
                emit("recording-error", message="Impossible de finaliser l’enregistrement local.")

    def stop_recording(self):
        with self.lock:
            self.close_capture()

    def samples(self, begin, end, *, minimum_coverage=.5):
        time.sleep(max(0.0, min(.50, end + .18 - time.monotonic())))
        with self.lock:
            blocks = list(self.blocks)
        size = max(0, round((end - begin) * RATE))
        samples = np.zeros(size, dtype=np.float32)
        coverage = 0
        written = 0
        for stamp, block in blocks:
            start = stamp - len(block) / (2 * RATE)
            if stamp <= begin or start >= end:
                continue
            offset = round((start - begin) * RATE)
            first = max(written, offset)
            last = min(size, offset + len(block) // 2)
            if first < last:
                data = np.frombuffer(block, dtype="<i2")
                samples[first:last] = data[first - offset:last - offset] / 32768
                coverage += last - first
                written = last
        return samples if coverage >= size * minimum_coverage else np.empty(0, dtype=np.float32)


class NativeDecoder:
    def __init__(self, language, *, live=False):
        self.live = live
        executable = Path(__file__).with_name("_kara_dtw")
        self.process = subprocess.Popen((str(executable), "--live" if live else "--stream", str(TURBO_MODEL), language or "auto"),
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        try:
            readable, _, _ = select([self.process.stdout], [], [], 25)
            if not readable or self.process.stdout.readline() != b"ready\n":
                raise RuntimeError("Le modèle Turbo avec minutage par mot est indisponible.")
        except (OSError, RuntimeError):
            self.close()
            raise

    def close(self):
        stop_process(self.process)

    def decode(self, audio, prompt):
        hint = prompt.encode()[:2048].decode("utf-8", errors="ignore").encode()
        spans = []
        for start in range(0, len(audio), 16 * RATE):
            clip = audio[start:start + 16 * RATE]
            pcm = np.clip(clip * 32768, -32768, 32767).astype("<i2")
            if self.live:
                pcm = np.pad(pcm, (0, RATE * 3 // 5))
            pipe = self.process.stdin
            pipe.write(struct.pack("<II", len(pcm), len(hint)))
            pipe.write(hint)
            pipe.write(pcm.tobytes())
            pipe.flush()
            lines = (line.decode("ascii") for line in iter(self.process.stdout.readline, b""))
            origin = start / RATE
            end = len(clip) / RATE
            decoded = read_timing(lines, lambda _: None, streaming=True)
            if self.live:
                decoded = [(a, min(b, end), text) for a, b, text in decoded if a < end]
            spans.extend((origin + a, origin + b, text) for a, b, text in decoded)
        return spans


def recognize(model, audio, *, language="", prompt="", turbo=False, timed=False):
    frames = len(audio) // 800
    if frames < 3:
        return [], False
    energies = np.sqrt(np.mean(audio[:frames * 800].reshape(-1, 800) ** 2, axis=1))
    audible = energies[energies > VOICE_FLOOR]
    if len(audible) < 3:
        # À bas niveau, un fond constant ne suffit pas à déclencher le score.
        floor = max(QUIET_VOICE_FLOOR, 2.2 * float(np.percentile(energies, 20)))
        audible = energies[energies > floor]
        if len(audible) < 3:
            return [], False
    gain = min(48.0, 0.065 / float(np.percentile(audible, 75)))
    if gain > 1.0:
        audio = np.clip(audio * gain, -1.0, 1.0)
    if isinstance(model, NativeDecoder):
        return model.decode(audio, prompt), True
    segments = model.transcribe(audio, language=language, initial_prompt=prompt, no_context=True,
                                no_speech_thold=.83 if timed else .6, audio_ctx=640 if turbo else 0,
                                token_timestamps=timed, split_on_word=timed, max_len=18 if timed else 0)
    end = len(audio) * 100 / RATE
    phrases = [(max(0, segment.t0) / 100, min(end, segment.t1) / 100, segment.text.strip())
               for segment in segments if segment.t0 < end
               and not (segment.t0 >= end - 60 and segment.t1 > end + 80)
               and segment.t1 > segment.t0]
    return phrases, True


SUNG_VOICE_FILTER = "highpass=f=220,lowpass=f=3600,atempo=1.24"
# Deux transpositions pour la reconnaissance, indépendantes de la hauteur du morceau.
PITCH_FILTER = "rubberband=pitch={ratio}:formant=preserved"
PITCH_RATIOS = (.7937, 1.2599)  # -4 et +4 demi-tons
SHORTEN_NOTES_FILTER = "rubberband=tempo={tempo}:pitch=0.92:formant=preserved"


def decode_filtered(audio, decode, filters, hint=""):
    """Filtre uniquement l'audio analysé et ramène les repères au temps du micro."""
    try:
        data = subprocess.check_output(
            ("ffmpeg", "-v", "error", "-f", "f32le", "-ar", str(RATE), "-ac", "1", "-i", "-",
             "-af", filters, "-f", "f32le", "-"),
            input=audio.astype("<f4", copy=False).tobytes(), stderr=subprocess.DEVNULL, timeout=2,
        )
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    transformed = np.frombuffer(data, dtype="<f4")
    if len(transformed) == 0:
        return None
    spans, _ = decode(transformed, hint)
    stretch = len(audio) / len(transformed)
    return [(a * stretch, b * stretch, word) for a, b, word in spans]


def recover_words(audio, window, expected, confirmed, decode, tolerance):
    centers = window.word_times
    if len(centers) != len(expected):
        return
    candidates = []
    for index, (target, center) in enumerate(zip(expected, centers)):
        if index in confirmed or target in IGNORED_HINTS or len(target) < 3:
            continue
        low, high = max(window.start, center - tolerance), min(window.stop, center + tolerance)
        if low < high:
            candidates.append((target, index, low, high))
    # Les mots distinctifs les plus longs apportent le plus d'information en isolation.
    candidates.sort(key=lambda candidate: (-len(candidate[0]), candidate[1]))
    for target, index, low, high in candidates[:2]:
        first = max(0, round((low - .25) * RATE))
        last = min(len(audio), round((high + .25) * RATE))
        clip_audio = audio[first:last]
        clip = AudioWindow(index, first / RATE, last / RATE, 1.)
        local = decode_filtered(clip_audio, decode, SHORTEN_NOTES_FILTER.format(tempo=1.30))
        if local is None:
            local, _ = decode(clip_audio, "")
        points = (point for point, word in clip.observed(local, words)
                  if low <= point <= high and same_word(target, word))
        for point in sorted(points, key=lambda point: abs(point - centers[index])):
            if any(other != index and target == expected[other]
                   and abs(point - time) < abs(point - centers[index])
                   for other, time in confirmed.items()):
                continue
            add_evidence(confirmed, index, point, expected)
            if index in confirmed:
                break


def transcribe_line(model, audio, job, turbo, *, diagnostics=None):
    language = job.get("language", "")
    prompt = ""  # The player's decoder never takes a text prompt from the job.
    timed = job.get("timed", False)
    expected = job.get("expected", ())
    tolerance = job.get("tolerance", SCORE_TOLERANCE)
    window = AudioWindow(0, 0., len(audio) / RATE, 1., tuple(job.get("word_times", ())))

    def decode(samples, hint):
        return recognize(model, samples, language=language, prompt=hint, turbo=turbo, timed=timed)

    def evidence_for(spans):
        observed = window.observed(spans, words)
        evidence = window.evidence(expected, observed, tolerance=tolerance)
        if diagnostics is not None:
            lexical = window.evidence(expected, observed)
            diagnostics.update(index for index, _ in lexical)
            diagnostics.update(index for index, _ in evidence)
        return evidence

    spans, signal = decode(audio, prompt)
    confirmed = dict(evidence_for(spans))
    if not signal or not expected or len(audio) < .65 * RATE:
        return spans, signal, sorted(confirmed.items())
    best = len(confirmed)
    passes = [(SUNG_VOICE_FILTER, "")]
    passes.extend((PITCH_FILTER.format(ratio=ratio), "") for ratio in PITCH_RATIOS)
    if best * 2 < len(expected):
        passes.append((SHORTEN_NOTES_FILTER.format(tempo=1.20), prompt))
    for filters, hint in passes:
        if len(confirmed) == len(expected):
            break
        retry = decode_filtered(audio, decode, filters, hint)
        if not retry:
            continue
        alternative = evidence_for(retry)
        if len(alternative) == len(expected):
            return retry, signal, alternative
        if len(alternative) > best:
            spans, best = retry, len(alternative)
        for index, point in alternative:
            add_evidence(confirmed, index, point, expected)
    recover_words(audio, window, expected, confirmed, decode, tolerance)
    return spans, signal, sorted(confirmed.items())


@dataclass(frozen=True)
class PlayerDecoding:
    """The stride owns timestamps; padding supplies neighboring audio only."""
    stride: int = 16
    padding: float = 1.
    beam: bool = False


PLAYER_DECODING = PlayerDecoding()


def transcribe_player(decoder, audio, language, *, decoding=None, diagnostics=None, observe=None):
    """Decode without lyric context and keep sample-relative token timestamps."""
    decoding = decoding or PLAYER_DECODING
    frames = len(audio) // 800
    if frames < 3:
        return [], "silence"
    blocks = audio[:frames * 800].reshape(-1, 800)
    energies = np.sqrt(np.mean((blocks - blocks.mean(axis=1, keepdims=True)) ** 2, axis=1))
    floor = max(QUIET_VOICE_FLOOR, 2.2 * float(np.percentile(energies, 20)))
    voiced = energies[energies > min(VOICE_FLOOR, floor)]
    if len(voiced) < 3:
        return [], "silence"
    gain = max(1., min(12., .065 / float(np.percentile(voiced, 75))))
    pcm = np.clip(audio * gain * 32768, -32768, 32767).astype("<i2")
    heard = []
    stride, pad = round(decoding.stride * RATE), round(decoding.padding * RATE)
    for call, low in enumerate(range(0, len(pcm), stride)):
        high = min(len(pcm), low + stride)
        begin, end = max(0, low - pad), min(len(pcm), high + pad)
        clip = pcm[begin:end]
        if len(clip) < RATE * 1.2:
            clip = np.pad(clip, (0, int(RATE * 1.2) - len(clip)))
        payload = clip.tobytes()
        delivered = observe(call, begin, payload) if observe else None
        started = time.monotonic_ns()
        options = {"trace_tokens": True} if diagnostics is not None and isinstance(decoder, TurboDecoder) else {}
        detected, segments = decoder.decode(payload, language, begin / RATE, beam=decoding.beam, **options)
        if diagnostics is not None:
            elapsed = time.monotonic_ns() - started
            call_trace = {
                "call": call, "input_frames": len(clip), "input_submitted": delivered,
                "input_sha256": sha256(payload).hexdigest() if observe else None,
                "begin": begin / RATE, "end": end / RATE, "owner": [low / RATE, high / RATE],
                "language_requested": language or "auto", "language_detected": detected, "inference_ns": elapsed,
                "started_ns": started, "beam_size": 3 if decoding.beam else None,
                "stride_s": decoding.stride, "padding_s": decoding.padding, "gain": gain, "prompted": False,
                "segments": [{"text": span.text, "start": span.start, "end": span.end,
                              "no_speech": span.no_speech} for span in segments[:32]],
            }
            tokens = [
                [i, token, token_time, probability, piece[:96]]
                for i, span in enumerate(segments)
                for token, token_time, probability, piece in getattr(span, "tokens", ())
            ]
            # Bound one JSON line even on pathological repeated output.
            call_trace["tokens"] = tokens[:96]
            call_trace["omitted_tokens"] = max(0, len(tokens) - 96)
            diagnostics.setdefault("decodes", []).append(call_trace)
        for segment in segments:
            if segment.no_speech > .65:
                continue
            for word in segment.words:
                if low <= round(word.start * RATE) < high:
                    left = max(0, int(word.start * 20) - 1)
                    right = min(frames, int(word.start * 20) + 4)
                    confidence = word.confidence
                    if right <= left or max(energies[left:right]) <= min(VOICE_FLOOR, floor):
                        confidence = 0.
                    heard.append({"word": word.text, "start": word.start, "end": word.end,
                                  "confidence": confidence})
    return sorted(heard, key=lambda word: word["start"]), "voice"


def replay_alignment(received, expected):
    """Find the highest normalized correlation on the decimated PCM grid."""
    n = len(received)
    if len(expected) < n or n < RATE // 10:
        return 0., 0
    energy = float(received @ received)
    if energy <= 1e-12:
        return 0., 0
    fft_size = 1 << (len(expected) + n - 1).bit_length()
    product = np.fft.rfft(expected, fft_size) * np.fft.rfft(received[::-1], fft_size)
    cross = np.fft.irfft(product, fft_size)[n - 1:len(expected)]
    power = np.concatenate(([0.], np.cumsum(expected * expected)))
    reference_power = power[n:] - power[:-n]
    valid = reference_power >= max(1e-12, float(reference_power.max()) * .1)
    if not valid.any():
        return 0., 0
    correlation = np.zeros_like(cross)
    correlation[valid] = np.abs(cross[valid]) / np.sqrt(energy * reference_power[valid])
    index = int(correlation.argmax())
    return min(1., float(correlation[index])), index


def replay_features(samples):
    # First differences suppress DC and slow level changes. The correlation
    # lag is evidence of replay only and never changes the scoring clock.
    return np.diff(samples, prepend=samples[0])[::4].astype(np.float64)


def backing_similarity(samples, reference):
    return replay_alignment(replay_features(samples), replay_features(reference))[0]


REPLAY_LOW_BAND = np.hanning(33)
REPLAY_LOW_BAND /= REPLAY_LOW_BAND.sum()


def filtered_replay_fraction(received, expected):
    """Estimate how much held-out energy a short playback filter explains."""
    similarity, offset = replay_alignment(received, expected)
    if similarity < .65 or len(received) < RATE // 4:
        return 0.
    # A 25-tap filter spans ±3 ms around the coarse lag at 4 kHz. Fit on
    # alternating half-second blocks and check the others, so a locally
    # matching beat cannot alone justify excluding the full window.
    radius = 12
    design = np.lib.stride_tricks.sliding_window_view(np.pad(expected, radius), 2 * radius + 1)
    indices = np.arange(0, len(received), 3)
    training = indices // (RATE // 8) % 2 == 0
    fit, check = indices[training], indices[~training]
    matrix = design[offset + fit]
    covariance = matrix.T @ matrix
    ridge = float(covariance.trace()) * 1e-4 / len(covariance)
    target = received[check]
    power = float(target @ target)
    if ridge <= 0. or power <= 1e-12:
        return 0.
    covariance.flat[::len(covariance) + 1] += ridge
    coefficients = np.linalg.solve(covariance, matrix.T @ received[fit])
    residual = design[offset + check] @ coefficients - target
    return max(0., 1. - float(residual @ residual) / power)


def transcribe_prepared_job(decoder, audio, job, *, decoding=None, diagnostics=None, observe=None):
    if not job.get("instrumental"):
        return [], "unverified"
    if float(np.max(np.abs(audio), initial=0.)) < QUIET_VOICE_FLOOR:
        return [], "silence"
    start = max(0., job["track_start"] - 1.25)
    duration = job["track_end"] - start + 1.25
    # An audible guide has the same words as the singer. Without a verified
    # acoustic separation, that overlap cannot earn credit. A conservative
    # decoded-stem gate still lets guide-free passages use the normal checks.
    for name in ("guide", "backing", "original"):
        if name == "guide" and not job.get(name):
            continue
        # No audio device is involved. Read the playback media only, so a
        # common waveform cannot turn instrumental leak into player credit.
        try:
            raw = subprocess.check_output(
                ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                 "-ss", str(start), "-i", job[name], "-map", "0:a:0", "-vn", "-sn", "-dn",
                 "-t", str(duration), "-ac", "1", "-ar", str(RATE), "-f", "f32le", "pipe:1"],
                stderr=subprocess.DEVNULL, timeout=5)
        except (OSError, subprocess.SubprocessError):
            return [], "unverified"
        reference = np.frombuffer(raw, dtype="<f4")
        if not len(reference):
            return [], "unverified"
        if name == "guide":
            present = float(np.max(np.abs(reference))) >= .0003
            if diagnostics is not None:
                diagnostics["guide_present"] = present
            if present:
                return [], "guide"
            continue
        reference = np.pad(reference, (0, max(0, len(audio) - len(reference))))
        similarity = backing_similarity(audio, reference)
        if diagnostics is not None:
            diagnostics.setdefault("similarity", {})[name] = similarity
        if similarity >= .72:
            return [], "backing"
        # Frequency shaping can hide a replay from broadband correlation.
        # The short, checked filter extends the rejection gate only. It
        # neither subtracts the backing waveform nor adjusts scoring times.
        if name == "backing":
            low_audio = replay_features(np.convolve(audio, REPLAY_LOW_BAND, mode="same"))
        low_ref = replay_features(np.convolve(reference, REPLAY_LOW_BAND, mode="same"))
        explained = filtered_replay_fraction(low_audio, low_ref)
        if diagnostics is not None:
            diagnostics.setdefault("filtered_replay_fraction", {})[name] = explained
        if explained >= .94:
            return [], "backing"
    return transcribe_player(decoder, audio, job["language"], decoding=decoding, diagnostics=diagnostics, observe=observe)


def record_prepared_diagnosis(stats, job, source, heard=(), diagnostic=None):
    if stats is None or "lignes" not in stats:
        return
    # The WAV and sidecar share the capture origin. The musical clock and
    # worker job ID let the comparator reproduce the same decision offline.
    origin = stats["_capture_begin"]
    stats["lignes"].append({
        "id": job["id"], "ligne": job["line"], "debut_s": round(job["begin"] - origin, 4),
        "fin_s": round(job["end"] - origin, 4), "debut_morceau_s": job["track_start"],
        "fin_morceau_s": job["track_end"], "vitesse": job.get("rate", 1.),
        "source": source, "mots_fenetre": heard, "diagnostic": diagnostic,
    })


def process_jobs(mic, jobs, model, turbo, stats=None, trace_channel=None):
    while True:
        job = jobs.get()
        began_ns = time.monotonic_ns()
        began = began_ns / 1_000_000_000
        try:
            minimum_coverage = .90 if job.get("prepared") else .50
            audio = mic.samples(job["begin"], job["end"], minimum_coverage=minimum_coverage)
            if len(audio) < RATE * (job["end"] - job["begin"]) * 0.50:
                if stats is not None:
                    stats["captures_incompletes"] += 1
                if job.get("prepared"):
                    record_prepared_diagnosis(stats, job, "capture")
                emit("skipped", id=job["id"], source="capture")
                continue
            if job.get("prepared"):
                diagnostic = {} if job.get("trace") or stats is not None and "lignes" in stats else None
                def observe(call, offset, block):
                    return submit_turbo_input(trace_channel, job["id"], call, offset, block)

                capture = trace_channel is not None and job.get("capture_decoder")
                heard, source = transcribe_prepared_job(model, audio, job, diagnostics=diagnostic,
                                                        observe=observe if capture else None)
                if stats is not None:
                    stats["lignes_analysees"] += 1
                    stats["lignes_signal_faible"] += source == "silence"
                    stats["mots_transcrits"] = stats.get("mots_transcrits", 0) + len(heard)
                    record_prepared_diagnosis(stats, job, source, heard, diagnostic)
                extra = ({"diagnostics": diagnostic, "inference_seconds": time.monotonic() - began,
                          "queue_wait_ns": max(0, began_ns - job.get("queued_ns", began_ns))}
                         if job.get("trace") else {})
                emit("score", id=job["id"], heard=heard, source=source, **extra)
                continue
            lexical = set() if stats is not None else None
            spans, signal, evidence = transcribe_line(model, audio, job, turbo, diagnostics=lexical)
            if stats is not None:
                indices = {index for index, _ in evidence}
                stats["lignes_analysees"] += 1
                stats["lignes_signal_faible"] += not signal
                stats["mots_attendus"] += len(job.get("expected", ()))
                stats["mots_reconnus_a_temps"] += len(indices)
                stats["mots_reconnus_hors_delai"] += len(lexical - indices)
                if "lignes" in stats:
                    origin = stats["_capture_begin"]
                    stats["lignes"].append({
                        "ligne": job.get("line"),
                        "debut_s": round(job["begin"] - origin, 3),
                        "fin_s": round(job["end"] - origin, 3),
                        "attendus": len(job.get("expected", ())),
                        "reconnus": len(indices),
                        "hors_delai": len(lexical - indices),
                        "signal": signal,
                    })
                stats["niveau_max_dbfs"] = max(stats["niveau_max_dbfs"],
                    round(20 * np.log10(max(1e-6, float(np.max(np.abs(audio))))), 1))
            emit("score", id=job["id"], text=" ".join(text for _, _, text in spans),
                 spans=spans, word_evidence=evidence, signal=signal)
        except (OSError, EOFError):
            emit("error", message="Le moteur Whisper s’est arrêté.")
            return
        except (RuntimeError, ValueError, TimeoutError):
            if stats is not None:
                stats["echecs_decodeur"] += 1
            if job.get("prepared"):
                record_prepared_diagnosis(stats, job, "decoder")
            emit("skipped", id=job["id"], source="decoder")
        finally:
            if stats is not None:
                stats["traitement_max_s"] = max(stats["traitement_max_s"], round(time.monotonic() - began, 2))


def save_diagnostics(stats):
    if not (stats["lignes_demandees"] or stats.get("capture_locale")):
        return
    summary = {key: value for key, value in stats.items() if not key.startswith("_")}
    targets = []
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime:
        targets.append(Path(runtime) / "kara-dernier-tour.json")
    recording = stats.get("capture_locale")
    if recording and Path(recording).is_file():
        targets.append(Path(recording).with_suffix(".json"))
    for target in targets:
        draft = None
        try:
            with NamedTemporaryFile(mode="w", encoding="utf-8", dir=target.parent, delete=False) as output:
                draft = Path(output.name)
                json.dump(summary, output, ensure_ascii=False)
            os.replace(draft, target)
        except OSError:
            pass
        finally:
            if draft is not None:
                draft.unlink(missing_ok=True)


def load_model(mode):
    options = ((TURBO_MODEL, "Turbo"), (BASE_MODEL, "Base")) if mode == "turbo" else ((BASE_MODEL, "Base"),)
    for path, label in options:
        if not path.is_file():
            continue
        emit("status", message=f"Chargement de Whisper {label}…")
        try:
            model = Model(str(path), n_threads=4, redirect_whispercpp_logs_to=os.devnull,
                          print_progress=False, print_realtime=False, print_timestamps=False,
                          print_special=False, temperature_inc=0.0)
        except (OSError, RuntimeError, ValueError):
            if label == "Base":
                raise
        else:
            return model, label
    raise ValueError("Le modèle local de karaoké est introuvable.")


def main(mode="base", language=""):
    echo = recorder = native = prepared_decoder = mic = None
    trace_channel = None
    stats = {"lignes_demandees": 0, "lignes_analysees": 0, "lignes_signal_faible": 0,
             "captures_incompletes": 0, "echecs_decodeur": 0, "file_pleine": 0,
             "mots_attendus": 0, "mots_reconnus_a_temps": 0,
             "mots_reconnus_hors_delai": 0, "niveau_max_dbfs": -120., "traitement_max_s": 0.,
             "filtre_echo": False}
    if mode == "prepared":
        stats["comparaison"] = "reference_turbo_gtk"
    try:
        wireless = os.environ.get("KARA_NETWORK_MIC_FD") == "3"
        if wireless and os.environ.get("KARA_TURBO_TRACE_FD") == "4":
            trace_channel = socket.socket(fileno=4)
        if not wireless:
            microphone = pulse("get-default-source")
            if not microphone or microphone.endswith(".monitor"):
                raise ValueError("Sélectionne un vrai micro dans les réglages audio.")
        if mode == "prepared":
            emit("status", message="Chargement de Whisper large-v3-turbo…")
            device = os.environ.get("KARA_TURBO_DEVICE", "gpu")
            prepared_decoder = TurboDecoder(build_decoder(), TURBO_MODEL, device, 8 if device == "cpu" else 4)
        if mode == "turbo" and TURBO_MODEL.is_file():
            emit("status", message="Chargement de Whisper Turbo…")
            try:
                native = NativeDecoder(language, live=True)
            except (OSError, RuntimeError):
                pass
        decoder = prepared_decoder or native
        model, label = (decoder, "Turbo") if decoder is not None else load_model(mode)
        jobs = Queue(maxsize=8)
        if wireless:
            mic = Microphone(socket.socket(fileno=3))
            Thread(target=mic.read_network, daemon=True).start()
        else:
            echo, source = echo_filter(microphone)
            stats["filtre_echo"] = echo is not None
            recorder = subprocess.Popen(
                ("parec", "--raw", "--format=s16le", "--channels=1", f"--rate={RATE}",
                 "--latency-msec=75", "--process-time-msec=20", "--client-name=Karaoké",
                 f"--device={source}"),
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            mic = Microphone(recorder.stdout)
            Thread(target=mic.read, daemon=True).start()
            if not mic.started.wait(timeout=2.0):
                raise ValueError("Aucun flux audio reçu du micro sélectionné.")
        Thread(target=process_jobs, args=(mic, jobs, model, label == "Turbo", stats, trace_channel), daemon=True).start()
        emit("ready", echo=echo is not None, model=label, decoder=prepared_decoder.identity if prepared_decoder else None)
        for line in sys.stdin:
            job = json.loads(line)
            command = job.get("command")
            if command == "record-start" and "capture_locale" not in stats:
                try:
                    begin = float(job["begin"])
                    now = time.monotonic()
                    if not now - 3 <= begin <= now + .5:
                        raise ValueError("Début du tour hors du tampon audio")
                    path = mic.record(begin)
                except (OSError, ValueError):
                    emit("recording-error", message="Impossible de créer l’enregistrement local.")
                else:
                    stats["capture_locale"] = str(path)
                    stats["morceau"] = job.get("song", "")
                    stats["_capture_begin"] = begin
                    if mode == "prepared":
                        stats.update({key: job[key] for key in ("reference_id", "reference_lrc",
                                                                 "reference_revision", "tolerance") if key in job})
                    stats["lignes"] = []
                    emit("recording", active=True)
            elif command == "record-stop":
                mic.stop_recording()
                emit("recording", active=False)
            if command != "score":
                continue
            stats["lignes_demandees"] += 1
            try:
                jobs.put_nowait(job)
            except Full:
                stats["file_pleine"] += 1
                if job.get("prepared"):
                    record_prepared_diagnosis(stats, job, "decoder")
                emit("skipped", id=job["id"], source="decoder")
        return 0
    except (EOFError, OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        emit("error", message=str(error))
        return 1
    finally:
        if trace_channel is not None:
            trace_channel.close()
        if mic is not None:
            mic.stop_recording()
        save_diagnostics(stats)
        stop_process(recorder)
        stop_process(echo)
        if native is not None:
            native.close()
        if prepared_decoder is not None:
            prepared_decoder.close()


if __name__ == "__main__":
    raise SystemExit(main(*sys.argv[1:]))
