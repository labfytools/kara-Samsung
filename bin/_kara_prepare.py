"""Préparation persistante des chansons, utilisable par GTK et en mode silencieux."""
import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import sha256
import json
from math import isfinite
import os
import re
from pathlib import Path
import signal
import stat
import subprocess
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep
from urllib.error import URLError

from _kara_paths import BIN, STATE, music_library
from _kara_audio_identity import pcm_digest
from _kara_reference import (METHOD, MIN_LINE_COVERAGE, MIN_TOKEN_CONFIDENCE, MIN_TRANSCRIPT_AGREEMENT,
                             MIN_WORD_COVERAGE, VERSION, alignment, lrc_text)
from _kara_refine import MAX_DISAGREEMENT, MIN_LOCAL_CONFIDENCE, VERSION as REFINEMENT_VERSION, refine
from _kara_stems import MODEL as STEM_MODEL, cached_stems
from _kara_turbo import MODEL, OVERLAP, RATE, STRIDE, ObservedWord, TurboDecoder, build_decoder, digest, transcribe_file
from _kara_words import LyricVocabulary, word_boundaries
from _paroles_lrc import MAX_LRC_BYTES, source_orders

CACHE_ROOT = STATE / "kara/prepared"
MAX_MANIFEST = 16_000_000
SECTION = re.compile(r"^\[(?:intro|outro|couplet|refrain|chorus|verse|bridge|pont|pre[- ]?chorus|pré[- ]?refrain|"
                     r"interlude)(?:\s+\d+)?(?:\s*:\s*[^\]]{1,100})?\]$", re.I)


def file_token(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Un fichier ordinaire est requis.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def lrc_token(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Le fichier LRC doit être un fichier ordinaire.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def read_source(path):
    with path.open("rb") as source:
        raw = source.read(MAX_LRC_BYTES + 1)
    if len(raw) > MAX_LRC_BYTES:
        raise ValueError("Fichier LRC trop volumineux.")
    return raw


def fingerprint(raw):
    return sha256(raw).hexdigest()


def path_id(song):
    return fingerprint(os.fsencode(song))


def atomic(path, data):
    # Rename is the publication point. Flush both data and directory so a
    # process or machine interruption cannot publish a partial generation.
    draft = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            draft = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(draft, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)


def number(value, low, high):
    return type(value) in (int, float) and isfinite(value) and low <= value <= high


def validate(manifest):
    if manifest["version"] != VERSION or manifest["method"] != METHOD or manifest["state"] != "ready":
        return False
    duration = manifest["duration"]
    report = manifest["report"]
    if (not number(duration, 1, 3600)
            or not number(report["word_coverage"], MIN_WORD_COVERAGE, 1)
            or not number(report["line_coverage"], MIN_LINE_COVERAGE, 1)
            or not number(report["transcript_agreement"], MIN_TRANSCRIPT_AGREEMENT, 1)):
        return False
    source = source_orders(manifest["source_lrc"])[manifest.get("source_order", "timestamps")]
    lines = manifest["lines"]
    if not lines or len(source) != len(lines):
        return False
    previous = last_word = -1
    supported_lines = supported_words = total_words = total_lines = 0
    for index, (line, (stamp, text)) in enumerate(zip(lines, source)):
        display = line["display_start"]
        if (line["id"] != index or (line["source_start"], line["text"]) != (stamp, text)
                or not number(display, 0, duration) or not previous < display < duration):
            return False
        previous = display
        if line["start"] is not None and (not number(line["start"], 0, duration)
                                         or not line["words"] or line["start"] != line["words"][0]["start"]):
            return False
        if line["end"] is not None and (line["start"] is None or
                                       not number(line["end"], line["start"], duration)):
            return False
        tokens = word_boundaries(text)
        total_lines += bool(tokens)
        supported_lines += line["start"] is not None
        total_words += len(tokens)
        if len(line["words"]) != len(tokens):
            return False
        for j, (word, (token, left, right)) in enumerate(zip(line["words"], tokens)):
            a, b = word["characters"]
            start, end, confidence = word["start"], word["end"], word["confidence"]
            if (word["id"] != j or word["normalized"] != token or (a, b) != (left, right)
                    or word["text"] != text[a:b]
                    or (confidence is not None and not number(confidence, 0, 1))):
                return False
            if start is None:
                if end is not None or word["origin"] != "uncertain":
                    return False
            elif (not number(start, max(0, last_word), duration)
                  or not number(confidence, MIN_TOKEN_CONFIDENCE, 1)
                  or (end is not None and not number(end, start, duration))
                  or not valid_origin(word, duration)):
                return False
            else:
                last_word = start
                supported_words += 1
    return (bool(total_words and total_lines)
            and supported_words / total_words >= MIN_WORD_COVERAGE
            and supported_lines / total_lines >= MIN_LINE_COVERAGE)


def valid_origin(word, duration):
    if word["origin"] == "whisper_dtw":
        return True
    if word["origin"] not in {"whisper_local_mix", "whisper_local_vocals"}:
        return False
    alternate = word["evidence"]["alternate_start"]
    return (word["confidence"] >= MIN_LOCAL_CONFIDENCE and number(alternate, 0, duration)
            and abs(alternate - word["start"]) <= MAX_DISAGREEMENT)


def matching_settings(manifest, requested):
    saved = manifest["settings"]
    if requested is None or saved == requested:
        return True
    # The previous decoder frame is exactly the new greedy frame with a
    # zero option bit. Allow its validated anchors to migrate once, while
    # retaining the original build identity separately from local retries.
    differences = {"build", "local_decoding"}
    return (manifest["state"] == "ready" and not manifest.get("refinement")
            and "local_decoding" not in saved and "local_decoding" in requested
            and {key: value for key, value in saved.items() if key not in differences}
            == {key: value for key, value in requested.items() if key not in differences})


def failed_first_pass(receipt, audio_hash, model, settings, requested_language):
    """Read bounded mix observations from an insufficient Vulkan receipt."""
    if receipt is None:
        return None
    try:
        draft = receipt["first_pass"]
        duration, language = receipt["duration"], receipt["language"]
        # A new lyric candidate can change the alignment, but it must not
        # silently reuse observations made with another input or decoder.
        if (receipt["audio"]["sha256"] != audio_hash or receipt["model"] != model
                or receipt["settings"] != settings or receipt["observation"] != {
                    "input": "original", "acceleration": "vulkan"}
                or draft["language_request"] != requested_language
                or not isinstance(draft["words"], list)
                or not number(duration, 1, 3600) or not re.fullmatch("[a-z]{2,8}", language)):
            return None
        heard = []
        previous = -1.
        for text, start, end, confidence, origin in draft["words"]:
            if (not isinstance(text, str) or not number(start, 0, duration) or start < previous
                    or not number(confidence, 0, 1)
                    or end is not None and not number(end, start, duration + .01)
                    or origin not in (None, "next_token", "segment_end")):
                return None
            heard.append(ObservedWord(text, start, end, confidence, origin))
            previous = start
        segments = draft["segments"]
        if not isinstance(segments, list) or any(
                not (number(segment["start"], 0, duration)
                     and number(segment["end"], segment["start"], duration)
                     and number(segment["no_speech"], 0, 1) and isinstance(segment["text"], str))
                for segment in segments):
            return None
        return duration, language, heard, segments
    except (KeyError, TypeError, ValueError, OverflowError):
        return None


class PreparedCache:
    def __init__(self, root=CACHE_ROOT, model=MODEL):
        self.root = Path(root)
        self.model = Path(model)
        self.model_identity = None

    def folder(self, song):
        return self.root / path_id(song)

    def model_info(self):
        token = file_token(self.model)
        if self.model_identity is None or self.model_identity["file"] != token:
            # GGML model type is reported by whisper.cpp on startup. The hash
            # distinguishes actual weights even when the filename stays equal.
            identity = digest(self.model)
            if file_token(self.model) != token:
                raise ValueError("Le modèle a changé pendant la lecture.")
            self.model_identity = {"name": "large-v3-turbo", "sha256": identity, "file": token}
        return self.model_identity

    @contextmanager
    def locked(self, song):
        folder = self.folder(song)
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        with (folder / "lock").open("a") as lock:
            try:
                flock(lock, LOCK_EX | LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Un autre processus prépare déjà ce morceau.") from error
            yield folder

    def cached(self, song, *, verify=True, settings=None, rebind=None, insufficient=False, previous=None):
        folder = self.folder(previous or song)
        try:
            with (folder / "prepared.json").open("rb") as stream:
                raw = stream.read(MAX_MANIFEST + 1)
            if len(raw) > MAX_MANIFEST:
                return None
            item = json.loads(raw)
            if insufficient:
                # A negative receipt has no playable/scorable reference and
                # cannot be mistaken for a published preparation.
                valid = (item["state"] == "insufficient" and item["version"] == VERSION
                         and item["method"] == METHOD and item["refinement"]["version"] == REFINEMENT_VERSION
                         and number(item["duration"], 1, 3600)
                         and number(item["report"]["word_coverage"], 0, 1)
                         and number(item["report"]["line_coverage"], 0, 1))
            else:
                valid = validate(item)
            if item["song_id"] != path_id(previous or song) or not valid or not matching_settings(item, settings):
                return None
            source_path = song.with_suffix(".lrc")
            source = lrc_token(source_path)
            audio = file_token(song)
            model = file_token(self.model)
            origin = item["source"]
            fallback = origin["kind"] == "fallback"
            # An alternate reference is bound to the exact rejected sidecar.
            # Even a verified rebind must not bypass a later human edit.
            if fallback and (source is None or source != origin["file"]):
                return None
            if not fallback and origin["kind"] != ("local" if source is not None else "retrieved"):
                return None
            if "plain_file" in origin and lrc_token(song.with_suffix(".txt")) != origin["plain_file"]:
                return None
            if verify:
                raw_source = read_source(source_path) if source is not None and not fallback else None
                stored = item["audio"]
                same_raw = stored["sha256"] == digest(song)
                same_pcm = (not same_raw and stored.get("pcm_sha256")
                            and pcm_digest(song) == stored["pcm_sha256"])
                if (item["model"]["sha256"] != self.model_info()["sha256"]
                        or not (same_raw or same_pcm)
                        or (raw_source is not None and (item["source"]["sha256"] != fingerprint(raw_source)
                            or item["source_lrc"] != raw_source.decode("utf-8-sig")))
                        or (raw_source is None and not insufficient and item["source"]["sha256"] !=
                            fingerprint(item["source_lrc"].encode("utf-8")))
                        or (audio, model, source) != (file_token(song), file_token(self.model), lrc_token(source_path))):
                    return None
            elif (item["model"]["file"] != model or item["audio"]["file"] != audio
                  or item["source"]["file"] != source):
                return None
            if insufficient:
                return item
            corrected = lrc_text(item["lines"]).encode()
            key = fingerprint(corrected)
            with (folder / (key + ".lrc")).open("rb") as stream:
                rendered = stream.read(len(corrected) + 1)
            if item["lrc_sha256"] != key or rendered != corrected:
                return None
            if previous is not None and (not verify or rebind is None):
                return None
            if verify and rebind is not None and (previous is not None or any(
                    item[key]["file"] != token for key, token in
                    zip(("audio", "source", "model"), (audio, source, model)))):
                # Rebind only after proving the recording, reference and model.
                item["song_id"] = path_id(song)
                if not same_raw:
                    item["audio"]["sha256"] = digest(song)
                if not item["audio"].get("pcm_sha256"):
                    try:
                        item["audio"]["pcm_sha256"] = pcm_digest(song)
                    except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
                        pass
                for key, token in zip(("audio", "source", "model"), (audio, source, model)):
                    item[key]["file"] = token
                self.publish(rebind, item)
            return item
        except (OSError, KeyError, ValueError, TypeError, OverflowError, RecursionError, subprocess.SubprocessError):
            return None

    def publish(self, folder, item):
        if not validate(item):
            raise ValueError("Référence incohérente : publication refusée.")
        corrected = lrc_text(item["lines"]).encode()
        key = fingerprint(corrected)
        item["lrc_sha256"] = key
        raw = json.dumps(item, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
        if len(raw) > MAX_MANIFEST:
            raise ValueError("Référence trop volumineuse.")
        output = folder / (key + ".lrc")
        # The LRC is immutable within its generation. The manifest references
        # the complete LRC only after its write has been committed.
        atomic(output, corrected)
        atomic(folder / "prepared.json", raw)
        for old in folder.glob("*.lrc"):
            if old != output:
                old.unlink(missing_ok=True)


def retrieve(song):
    result = subprocess.run([str(BIN / "paroles-musique"), "--bibliotheque", str(music_library()),
                             "--lrc-stdout", "--", str(song)],
                            capture_output=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError(result.stderr.decode(errors="replace").strip()[-350:] or
                         "Aucune source de paroles minutées disponible.")
    data = json.loads(result.stdout)
    raw = data["lyrics"].encode()
    if len(raw) > MAX_LRC_BYTES:
        raise ValueError("Paroles récupérées trop volumineuses.")
    return raw, data["provider"]


def choose_alignment(orders, heard, duration, progress):
    results = [(order, *alignment(rows, heard, duration, progress if i == 0 else lambda *_: None))
               for i, (order, rows) in enumerate(orders.items())]
    return max(results, key=lambda result: (result[3], result[2]["word_coverage"], result[2]["line_coverage"]))


class Preparer:
    def __init__(self, cache, *, device="gpu", threads=None, logs=False, reader=retrieve,
                 separate_if_needed=False, trace=None, require_vulkan=False):
        self.cache = cache
        self.device = device
        self.threads = threads if threads is not None else (4 if device == "gpu" else 8)
        self.logs = logs
        self.reader = reader
        self.separate_if_needed = separate_if_needed
        self.trace = trace
        self.require_vulkan = require_vulkan
        self.decoder = None
        self.binary = None
        self.model_lock = None

    def close(self):
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None
        if self.model_lock is not None:
            self.model_lock.close()
            self.model_lock = None

    def reserve_model(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR")
        if not runtime:
            return
        # Share the existing Kara model slot with live inference. Preparation
        # does not enter the voice session or change the dictation service.
        lock = (Path(runtime) / "kara-model.lock").open("a")
        try:
            limit = monotonic() + 30
            while True:
                try:
                    flock(lock, LOCK_EX | LOCK_NB)
                    self.model_lock = lock
                    return
                except BlockingIOError:
                    if monotonic() >= limit:
                        raise TimeoutError("Le moteur local est déjà utilisé par une autre session Kara.")
                    sleep(.2)
        except BaseException:
            lock.close()
            raise

    def settings(self):
        if self.binary is None:
            self.binary = build_decoder()
        return {"backend": "whisper.cpp-dtw", "build": self.binary.name, "device": self.device,
                "threads": self.threads, "rate": RATE, "window_stride": STRIDE, "window_overlap": OVERLAP,
                "temperature": 0, "no_context": True, "no_speech_threshold": .65, "prompt": "",
                "fallback": "near-threshold-dtwturbo-" + STEM_MODEL,
                "local_decoding": "beam3-with-greedy-onset-audit"}

    def vocal_stem(self, song, progress):
        stems = cached_stems(song)
        if stems is None and self.separate_if_needed:
            progress("separation", 94)
            # The existing Demucs CLI terminates its child on SIGTERM. Release
            # the model before a full separation to avoid concurrent memory peaks.
            self.close()
            with subprocess.Popen([sys.executable, "-B", str(BIN / "_kara_stems.py"), str(song)],
                                  stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL) as separation:
                try:
                    separation.wait(timeout=900)
                except (KeyboardInterrupt, subprocess.TimeoutExpired) as error:
                    separation.terminate()
                    try:
                        separation.wait(timeout=12)
                    except subprocess.TimeoutExpired:
                        separation.kill()
                    if isinstance(error, KeyboardInterrupt):
                        raise
            stems = cached_stems(song)
        return stems[1] if stems else None

    def load_decoder(self):
        if self.decoder is None:
            self.reserve_model()
            self.decoder = TurboDecoder(self.binary, self.cache.model, self.device, self.threads,
                                        logs=self.logs, require_vulkan=self.require_vulkan)
        return self.decoder

    def transcribe(self, song, language, progress, *, review=None):
        load_seconds = 0.
        if self.decoder is None:
            start = monotonic()
            progress("model", 12)
            self.load_decoder()
            load_seconds = monotonic() - start
        progress("transcription", 20)
        start = monotonic()
        result = transcribe_file(song, self.decoder, language,
                                 lambda f: progress("transcription", 20 + int(55 * f)), review=review)
        return *result, load_seconds, monotonic() - start

    def trace_text(self, song, kind, text, **data):
        if self.trace:
            for offset in range(0, len(text), 6000):
                self.trace(song, kind, offset=offset, text=text[offset:offset + 6000], **data)

    def first_pass(self, song, lrc, settings, progress, plain=None, timed=None,
                   provider="Strawberry", rejected=None, previous_attempt=None):
        audio_file = file_token(song)
        audio_hash = digest(song)
        source_file = lrc_token(lrc)
        progress("lyrics", 8)
        if (plain is not None or timed is not None) and source_file != rejected:
            raise ValueError("Le LRC original a changé avant l’analyse.")
        plain_file = lrc_token(song.with_suffix(".txt")) if plain is not None and rejected else None
        if plain is not None or timed is not None:
            source = plain if plain is not None else timed
            kind = "fallback" if rejected else "retrieved"
        elif source_file is not None:
            source, provider, kind = read_source(lrc), "library", "local"
        else:
            source, provider = self.reader(song)
            kind = "retrieved"
        if len(source) > MAX_LRC_BYTES:
            raise ValueError("Fichier LRC trop volumineux.")
        text = source.decode("utf-8-sig")
        if self.trace:
            self.trace_text(song, "source-lrc", text, provider=provider, sha256=fingerprint(source))
        if plain is not None:
            from unicodedata import normalize
            rows = [(None, normalize("NFC", line.strip())) for line in text.splitlines()
                    if not SECTION.fullmatch(line.strip()) and word_boundaries(line)]
            orders = {"timestamps": rows}
        else:
            orders = source_orders(text)
        vocabulary = LyricVocabulary(t for _, t in orders["timestamps"])
        if not any(vocabulary.tokens):
            raise ValueError("Paroles minutées introuvables.")
        model = self.cache.model_info()
        saved = failed_first_pass(previous_attempt, audio_hash, model, settings, vocabulary.language)
        if saved is None:
            duration, language, heard, segments, model_seconds, analysis_seconds = self.transcribe(
                song, vocabulary.language, progress)
        else:
            progress("cached_transcription", 75)
            duration, language, heard, segments = saved
            model_seconds = analysis_seconds = 0.
        if self.trace:
            for index, segment in enumerate(segments):
                self.trace(song, "original-transcription", index=index, segment=segment, language=language)
        source_order, lines, report, _ = choose_alignment(orders, heard, duration, progress)
        report.update(input="original", duration=round(duration, 3), model_seconds=round(model_seconds, 3),
                      analysis_seconds=round(analysis_seconds, 3), real_time_factor=round(analysis_seconds / duration, 4))
        if saved is not None:
            report["reused_first_pass"] = True
        if self.decoder is not None:
            report["native_peak_rss_kib"] = self.decoder.peak_rss_kib()
        audio = {"sha256": audio_hash, "file": audio_file}
        try:
            audio["pcm_sha256"] = pcm_digest(song)
        except (OSError, ValueError, KeyError, IndexError, subprocess.SubprocessError):
            # A receipt can still be validated against its exact file hash.
            pass
        return {"version": VERSION, "method": METHOD, "song_id": path_id(song),
                "title": song.stem, "language": language, "model": model, "settings": settings,
                "audio": audio,
                "source": {"kind": kind,
                           "file": source_file, "provider": provider,
                           "sha256": fingerprint(source if kind == "local" else text.encode()),
                           **({"plain_file": plain_file} if plain is not None and rejected else {})},
                "source_lrc": text, "source_order": source_order, "duration": duration, "lines": lines,
                "segments": segments, "observation": {
                    "input": "original", **({"acceleration": "vulkan"} if self.require_vulkan else {})},
                "report": report, **({"first_pass": (vocabulary.language, heard)} if self.require_vulkan else {})}

    def deepen(self, song, item, progress):
        base = item["report"]
        # Keep the original first-pass summary when the user retries local
        # work on an already consolidated generation.
        initial = base.get("baseline") or {key: base.get(key) for key in (
            "word_coverage", "line_coverage", "uncertain_words", "uncertain_lines", "max_delta",
            "analysis_seconds", "native_peak_rss_kib")}
        stem_seconds = 0.
        stem_id = None
        stem_identity = None

        def choose_stem():
            nonlocal stem_seconds, stem_id, stem_identity
            began = monotonic()
            stems = cached_stems(song)
            # A costly new separation is opt-in and limited to an originally
            # near-threshold result. Existing stems may be compared directly.
            if stems:
                stem = stems[1]
            elif (self.separate_if_needed and base["line_coverage"] < MIN_LINE_COVERAGE
                  and base["word_coverage"] >= .62 and base["transcript_agreement"] >= .55):
                stem = self.vocal_stem(song, progress)
            else:
                stem = None
            if stem is not None:
                stem_identity = (stem, file_token(stem))
                stem_id = digest(stem)
            stem_seconds += monotonic() - began
            return stem

        began = monotonic()
        previous = item.get("refinement", {})
        audit_only = bool(previous and any(word["start"] is None and word.get("rejected")
                                          for line in item["lines"] for word in line["words"]))
        lines, report, ready, stats = refine(song, item["lines"], base, item["duration"], item["language"],
                                            self.load_decoder, vocal_stem=choose_stem, audit_only=audit_only,
                                            progress=progress)
        if stats["recovered_stem"]:
            stem, token = stem_identity
            if file_token(stem) != token:
                raise ValueError("La piste vocale a changé pendant la vérification.")
            stats["vocal_source"] = {"model": STEM_MODEL, "sha256": stem_id}
        stats.update(seconds=round(monotonic() - began, 3), separation_seconds=round(stem_seconds, 3))
        if self.decoder is not None and stats["decodes"]:
            stats["native_peak_rss_kib"] = self.decoder.peak_rss_kib()
        if previous:
            stats["previous_work"] = {key: previous.get(key) for key in
                                      ("windows", "stem_windows", "decodes", "seconds", "recovered_mix",
                                       "recovered_stem", "quarantined", "native_peak_rss_kib")}
        report.update(baseline=initial, refinement=stats)
        item.update(lines=lines, report=report, refinement=stats)
        return ready

    def prepare(self, song, *, force=False, progress=lambda *_: None, plain=None, previous=None,
                timed=None, provider="Strawberry", rejected=None, observed_corrections=False):
        song = Path(song).resolve(strict=True)
        start = monotonic()
        lrc = song.with_suffix(".lrc")
        with self.cache.locked(song) as folder:
            progress("cache", 2)
            settings = self.settings()
            item = self.cache.cached(song, settings=settings, rebind=folder)
            if item is None and previous is not None:
                item = self.cache.cached(song, settings=settings, rebind=folder, previous=previous)
            from_reference = item is not None
            if item and self.trace:
                self.trace(song, "cache-revision", id=item["song_id"], lrc_sha256=item["lrc_sha256"],
                           audio_sha256=item["audio"]["sha256"], revision=item["prepared_at"])
            if not force:
                if item and item.get("refinement", {}).get("version") == REFINEMENT_VERSION:
                    if self.trace:
                        self.trace_text(song, "source-lrc", item["source_lrc"],
                                        provider=item["source"].get("provider"), sha256=item["source"]["sha256"])
                        self.trace_text(song, "corrected-lrc", lrc_text(item["lines"]), sha256=item["lrc_sha256"])
                        for index, entry in enumerate(item["lines"]):
                            self.trace(song, "reference-line", index=index, line=entry)
                    progress("cached", 100)
                    return True, item["report"], monotonic() - start, item["duration"]
                if item is None:
                    attempt = (self.cache.cached(song, settings=settings, insufficient=True)
                               if plain is None and timed is None else None)
                    if attempt:
                        progress("cached_insufficient", 100)
                        return False, attempt["report"], monotonic() - start, attempt["duration"]
            if item is None:
                previous_attempt = None
                if self.require_vulkan and not force and (plain is not None or timed is not None):
                    previous_attempt = self.cache.cached(song, settings=settings, insufficient=True, verify=False)
                if previous_attempt is None and self.require_vulkan and not force:
                    from _parole_transcribe import read_draft

                    # A reviewed lyric can use the private mix observations,
                    # subject to the same hash, model and language checks.
                    previous_attempt = read_draft(self.cache, song)
                item = self.first_pass(song, lrc, settings, progress, plain=plain, timed=timed,
                                       provider=provider, rejected=rejected, previous_attempt=previous_attempt)
            elif item["settings"] != settings:
                item["baseline_settings"] = item["settings"]
                item["settings"] = settings
            ready = self.deepen(song, item, progress)
            if ((file_token(song), lrc_token(lrc), file_token(self.cache.model))
                    != tuple(item[key]["file"] for key in ("audio", "source", "model"))):
                raise ValueError("Le morceau, le modèle ou le LRC a changé pendant la préparation.")
            if ("plain_file" in item["source"]
                    and lrc_token(song.with_suffix(".txt")) != item["source"]["plain_file"]):
                raise ValueError("Le texte de référence a changé pendant la préparation.")
            report = item["report"]
            first_pass = item.pop("first_pass", None)
            if observed_corrections and report.get("display_only_changes"):
                # Interactive playback permits display-only interpolation.
                # Automatic correction may keep inherited timestamps, but a
                # changed timestamp must have acoustic evidence.
                ready = False
            report["refinement"]["reused_reference"] = from_reference
            duration = item["duration"]
            if self.trace:
                self.trace(song, "reference-validation", id=item["song_id"], ready=ready, report=report,
                           audio_sha256=item["audio"]["sha256"], source_sha256=item["source"]["sha256"],
                           settings=item["settings"], model_sha256=item["model"].get("sha256"))
                for index, entry in enumerate(item["lines"]):
                    self.trace(song, "reference-line", index=index, line=entry)
            if not ready:
                report["message"] = "Référence temporelle insuffisante pour noter le morceau."
                receipt = {k: v for k, v in item.items() if k not in ("lines", "segments", "lrc_sha256")}
                receipt["state"] = "insufficient"
                if first_pass is not None and not from_reference:
                    language_request, heard = first_pass
                    receipt["first_pass"] = {
                        "language_request": language_request,
                        "words": [[w.text, w.start, w.end, w.confidence, w.end_origin] for w in heard],
                        "segments": item["segments"],
                    }
                # The same publication pointer revokes an old generation in
                # one rename. Observations for a retry remain insufficient,
                # and an oversized optional receipt falls back to redecoding.
                raw = json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                if "first_pass" in receipt and len(raw) > MAX_MANIFEST:
                    receipt.pop("first_pass")
                    raw = json.dumps(receipt, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
                atomic(folder / "prepared.json", raw)
                return False, report, monotonic() - start, duration
            progress("saving", 97)
            stats = report["refinement"]
            item["state"] = "ready"
            if not from_reference or stats["recovered_mix"] or stats["recovered_stem"] or stats["quarantined"]:
                item["prepared_at"] = datetime.now(timezone.utc).isoformat()
            if plain is not None and not from_reference:
                self.publish_generated(song, lrc, item, plain, preserve=bool(rejected))
            self.cache.publish(folder, item)
            if self.trace:
                self.trace_text(song, "corrected-lrc", lrc_text(item["lines"]), sha256=item["lrc_sha256"])
            progress("ready", 100)
            return True, report, monotonic() - start, duration


    def publish_generated(self, song, lrc, item, plain, *, preserve=False):
        from _musique_oz import shared_mutation, share_file

        # Seed the canonical cache with the actual observed, generated LRC.
        # A hard-link publication has no replace window, even with an editor.
        raw = lrc_text(item["lines"]).encode()
        if len(raw) > MAX_LRC_BYTES:
            raise ValueError("LRC généré trop volumineux")
        item["source_lrc"] = raw.decode()
        item["source_order"] = "timestamps"
        for line in item["lines"]:
            line["source_start"] = line["display_start"]
        item["source"].update(sha256=fingerprint(raw), plain_sha256=fingerprint(plain))
        if not validate(item):
            raise ValueError("Le LRC généré ne conserve pas les références lexicales")
        if preserve:
            # The validated timed text lives in the cache beside the corrected
            # reference. The rejected library LRC remains the observed input.
            return
        root = music_library()
        if not song.is_relative_to(root) or song.resolve(strict=True) != song:
            raise ValueError("Publication hors bibliothèque")
        with shared_mutation(root):
            if file_token(song) != item["audio"]["file"]:
                raise ValueError("La chanson a changé avant publication.")
            with NamedTemporaryFile(dir=lrc.parent, prefix=".parole-lrc-") as draft:
                draft.write(raw)
                draft.flush()
                os.fsync(draft.fileno())
                share_file(draft.name, lrc)
                os.link(draft.name, lrc)
                fd = os.open(lrc.parent, os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        item["source"].update(kind="local", file=lrc_token(lrc))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("songs", nargs="+", type=Path)
    parser.add_argument("--cache", type=Path, default=CACHE_ROOT)
    references = parser.add_mutually_exclusive_group()
    references.add_argument("--plain-reference", type=Path)
    references.add_argument("--timed-reference", type=Path)
    parser.add_argument("--reference-provider", default="Strawberry")
    parser.add_argument("--rejected-lrc", type=json.loads, help="Version du LRC à conserver sans modification")
    parser.add_argument("--observed-corrections-only", action="store_true")
    parser.add_argument("--require-vulkan", action="store_true")
    parser.add_argument("--previous-path", type=Path)
    parser.add_argument("--local-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--force-path", type=Path, action="append", default=[])
    parser.add_argument("--trace", action="store_true")
    parser.add_argument("--device", choices=("cpu", "gpu"), default=os.environ.get("KARA_TURBO_DEVICE", "gpu"))
    parser.add_argument("--threads", type=int, choices=range(1, 33))
    parser.add_argument("--backend-logs", action="store_true")
    parser.add_argument("--separate-if-needed", action="store_true")
    parser.add_argument("--chronos-night-draft", action="store_true",
                        help="Conserver le calage CHRONOS du texte en brouillon privé sans Demucs.")
    parser.add_argument("--chronos", choices=("huang", "wav2vec2", "huang-ft"),
                        help="Préparer aussi un défilement expérimental pour cette sélection.")
    args = parser.parse_args()
    if args.require_vulkan and args.device != "gpu":
        parser.error("La préparation Vulkan requiert --device gpu.")
    if args.chronos_night_draft and (not args.plain_reference or not args.local_only or args.chronos):
        parser.error("Le brouillon nocturne exige un texte et une préparation locale.")

    def cancel(_signal, _frame):
        raise KeyboardInterrupt

    def send(event, path, **data):
        print(json.dumps({"event": event, "path": str(path), **data}, ensure_ascii=False), flush=True)

    signal.signal(signal.SIGTERM, cancel)
    def local_reader(_song):
        raise ValueError("Référence locale introuvable après la recherche Internet.")

    preparer = Preparer(PreparedCache(args.cache), device=args.device, threads=args.threads, logs=args.backend_logs,
                        reader=local_reader if args.local_only else retrieve,
                        separate_if_needed=args.separate_if_needed, require_vulkan=args.require_vulkan,
                        trace=(lambda song, kind, **payload: send("evidence", song, kind=kind, evidence=payload))
                        if args.trace else None)
    plain = read_source(args.plain_reference) if args.plain_reference is not None else None
    timed = read_source(args.timed_reference) if args.timed_reference is not None else None
    if args.rejected_lrc is not None and (plain is None and timed is None or
            type(args.rejected_lrc) is not list or len(args.rejected_lrc) != 5
            or any(type(value) is not int for value in args.rejected_lrc)):
        parser.error("Version du LRC à conserver invalide")
    failed = False
    force_paths = {path.resolve() for path in args.force_path}
    try:
        for song in dict.fromkeys(path.resolve() for path in args.songs):
            phase = ""

            def progress(stage, percent):
                nonlocal phase
                phase = stage
                send("progress", song, phase=stage, percent=percent)

            if args.chronos_night_draft:
                from _kara_chronos import prepare as prepare_chronos

                progress("chronos", 5)
                source = {"kind": "night-plain", "provider": args.reference_provider,
                          "original": plain.decode("utf-8"), "timed": False}
                try:
                    status = prepare_chronos(song, preparer.cache, "huang", supplied=source, allow_demucs=False)
                except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                    send("chronos", song, engine="huang", status="unavailable", message=str(error))
                else:
                    send("chronos", song, engine="huang", status=status)
            if args.chronos and plain is None and timed is None:
                from _kara_chronos import prepare as prepare_chronos, source_for
                from _parole_process import Interrupted
                from _parole_transcribe import draft_source

                try:
                    draft = draft_source(preparer.cache, song)
                    use_draft = draft and source_for(song, preparer.cache, include_draft=True) == draft
                except (OSError, ValueError):
                    use_draft = False
                if use_draft:
                    # Draft text must never become a historical/scoring
                    # reference. Admit only the bounded Huang path here.
                    preparer.close()
                    progress("chronos", 95)
                    try:
                        status = prepare_chronos(song, preparer.cache, "huang", supplied=draft, allow_demucs=False)
                    except (OSError, ValueError, RuntimeError, KeyError, Interrupted, subprocess.SubprocessError):
                        status = "unavailable"
                    send("chronos", song, engine="huang", status=status)
                    send("result", song, ready=False, experimental=True,
                         report={"message": "Paroles générées expérimentalement · chant sans note"},
                         duration=0., seconds=0.)
                    failed = True
                    continue
            result_available = False
            try:
                ready, report, elapsed, duration = preparer.prepare(
                    song, force=args.force or song in force_paths, progress=progress, plain=plain,
                    previous=args.previous_path, timed=timed, provider=args.reference_provider, rejected=args.rejected_lrc,
                    observed_corrections=args.observed_corrections_only)
            except (EOFError, OSError, ValueError, RuntimeError, KeyError, URLError, subprocess.SubprocessError) as error:
                preparer.close()
                send("error", song, message=str(error))
                failed = True
            else:
                result_available = True
                cache_hit = phase in ("cached", "cached_insufficient")
                failed |= not ready
            if args.chronos:
                from _kara_chronos import prepare as prepare_chronos

                # Release Turbo before entering the independently admitted
                # CHRONOS scope. Its result never changes the score reference.
                preparer.close()
                progress("chronos", 95)
                try:
                    status = prepare_chronos(song, preparer.cache, args.chronos)
                except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
                    send("chronos", song, engine=args.chronos, status="unavailable", message=str(error))
                else:
                    send("chronos", song, engine=args.chronos, status=status)
            if result_available:
                send("result", song, ready=ready, cached=cache_hit, report=report,
                     seconds=round(elapsed, 3), duration=round(duration, 3))
    except KeyboardInterrupt:
        return 130
    finally:
        preparer.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
