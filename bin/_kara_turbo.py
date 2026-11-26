"""Whisper Turbo hors ligne, sans dépendance au lecteur ni au micro."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import file_digest, sha256
import os
from pathlib import Path
import re
from select import select
import struct
import subprocess
from tempfile import TemporaryDirectory, TemporaryFile
from time import monotonic

from _kara_paths import BIN, CACHE, DATA
from _kara_words import word_boundaries

RATE = 16_000
STRIDE = 24
OVERLAP = 2
MAX_SECONDS = 3600
MAX_PROMPT_BYTES = 768
MODEL = DATA / "pywhispercpp/models/ggml-large-v3-turbo.bin"
SOURCE = BIN.parent / "native/src/_kara_dtw.c"


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def build_decoder():
    # The headers and shared libraries must come from the same installed build.
    root = Path(os.environ.get("KARA_WHISPER_ROOT", DATA / "hyprwhspr/pywhispercpp-src")).resolve()
    include = root / "whisper.cpp/include"
    library = root / "libwhisper.so"
    key = sha256()
    for path in (SOURCE, include / "whisper.h", library,
                 *sorted(root.glob("libggml*.so"))):
        key.update(digest(path).encode())
    folder = CACHE / "kara/native"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    binary = folder / ("turbo-" + key.hexdigest()[:24])
    if not binary.is_file():
        with TemporaryDirectory(dir=folder) as draft:
            compiled = Path(draft) / "turbo"
            subprocess.run(["cc", "-std=c17", "-O2", "-Wall", "-Wextra", "-Werror",
                            "-I", str(include), "-I", str(root / "whisper.cpp/ggml/include"),
                            str(SOURCE), "-L", str(root), "-Wl,-rpath," + str(root),
                            "-lwhisper", "-o", str(compiled)], check=True, timeout=60)
            os.replace(compiled, binary)
    return binary


@dataclass(frozen=True)
class ObservedWord:
    text: str
    start: float
    end: float | None
    confidence: float
    end_origin: str | None = None


@dataclass(frozen=True)
class Segment:
    text: str
    start: float
    end: float
    no_speech: float
    words: tuple


def decode_segment(start, end, no_speech, tokens, origin, duration):
    if not 0 <= start < end <= duration + .55 or not 0 <= no_speech <= 1:
        return None
    text = b"".join(piece for _, _, piece in tokens).decode("utf-8", errors="replace")
    if re.fullmatch(r"\s*(\[[^\]]*\]|\([^)]*\)|[♫♪♬\s]+)\s*", text):
        return Segment(text, origin + start, origin + min(end, duration), no_speech, ())
    # BPE can split UTF-8 characters. Match character boundaries against byte
    # positions in the reassembled segment, then use the corresponding tokens.
    from bisect import bisect_right
    from itertools import accumulate
    words = []
    offsets = list(accumulate(len(piece) for _, _, piece in tokens))
    if "\ufffd" not in text:
        for word, left, right in word_boundaries(text):
            first = bisect_right(offsets, len(text[:left].encode("utf-8")))
            after = bisect_right(offsets, len(text[:right].encode("utf-8")) - 1) + 1
            mark = tokens[first][0]
            if not max(0, start - .5) <= mark < duration or mark > end + .2:
                continue
            following = tokens[after][0] if after < len(tokens) else end
            # The next DTW point is an upper boundary with possible intervening
            # silence. A distant or reversed boundary supplies no word end.
            stop = origin + following if mark < following <= min(mark + 2.5, duration, end + .2) else None
            confidence = min(probability for _, probability, _ in tokens[first:after])
            words.append(ObservedWord(word, round(origin + mark, 4), stop, confidence,
                                      ("next_token" if after < len(tokens) else "segment_end") if stop else None))
    return Segment(text.strip(), origin + start, origin + min(end, duration), no_speech, tuple(words))


class TurboDecoder:
    """One native context with an optional, frame-local experimental prompt."""
    def __init__(self, binary, model, device, threads, *, logs=False, require_vulkan=False):
        backend = "vulkan" if require_vulkan else device
        self.process = subprocess.Popen([str(binary), "--prepare", str(model), backend, str(threads)],
                                        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=None if logs else subprocess.DEVNULL)
        self.buffer = bytearray()
        try:
            if self.read_line(monotonic() + 60) != b"ready":
                raise ValueError("Le modèle Whisper large-v3-turbo n’a pas démarré.")
        except BaseException:
            self.close()
            raise

    def read_line(self, limit):
        while b"\n" not in self.buffer:
            if not select([self.process.stdout], [], [], max(0, limit - monotonic()))[0]:
                raise TimeoutError("Délai dépassé pour le moteur Turbo.")
            data = os.read(self.process.stdout.fileno(), 8192)
            if not data:
                raise EOFError("Le moteur Turbo s’est arrêté.")
            self.buffer.extend(data)
            if len(self.buffer) > 65536:
                raise ValueError("Sortie Turbo invalide.")
        line, _, self.buffer = self.buffer.partition(b"\n")
        return line

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()

    def peak_rss_kib(self):
        try:
            status = Path(f"/proc/{self.process.pid}/status").read_text()
            line = next(line for line in status.splitlines() if line.startswith("VmHWM:"))
            return int(line.split()[1])
        except (OSError, StopIteration, ValueError, IndexError):
            return None

    def decode(self, pcm, language, origin=0, *, beam=False, prompt=""):
        code = (language or "auto").encode("ascii")
        if len(pcm) % 2 or not 0 < len(pcm) <= 29 * RATE * 2 or not 0 < len(code) <= 15:
            raise ValueError("Fenêtre PCM invalide.")
        hints = prompt.encode("utf-8")
        if hints and (len(hints) > MAX_PROMPT_BYTES or not prompt.isprintable()):
            raise ValueError("Contexte linguistique invalide.")
        pipe = self.process.stdin
        flags = (0x80 if beam else 0) | (0x40 if hints else 0)
        pipe.write(struct.pack("<IB", len(pcm) // 2, len(code) | flags) + code)
        if hints:
            pipe.write(struct.pack("<H", len(hints)) + hints)
        pipe.write(pcm)
        pipe.flush()
        limit = monotonic() + 180
        length = len(pcm) / (2 * RATE)
        segments, tokens = [], []
        boundary = None
        detected = language
        for _ in range(8192):
            kind, *fields = self.read_line(limit).split()
            if kind in (b"segment", b"done") and boundary is not None:
                segment = decode_segment(*boundary, tokens, origin, length)
                if segment is not None:
                    segments.append(segment)
                tokens = []
            if kind == b"done":
                return detected, segments
            if kind == b"language":
                detected = fields[0].decode("ascii")
            elif kind == b"segment":
                boundary = int(fields[0]) / 100, int(fields[1]) / 100, float(fields[2])
            elif kind == b"token":
                tokens.append((int(fields[0]) / 100, float(fields[1]), bytes.fromhex(fields[2].decode())))
            elif kind == b"error":
                if self.read_line(limit) != b"done":
                    raise ValueError("Fin de trame Turbo invalide.")
                raise ValueError("Turbo a rejeté la fenêtre ou son contexte linguistique.")
            else:
                raise ValueError("Échec du décodage Whisper Turbo.")
        raise ValueError("Sortie Turbo trop volumineuse.")


@contextmanager
def pcm_file(path):
    # Overlapping windows share a private disk-backed sample timeline.
    with TemporaryFile() as pcm:
        bound = MAX_SECONDS * RATE * 2
        subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                        "-map", "0:a:0", "-vn", "-sn", "-dn", "-ac", "1", "-ar", str(RATE),
                        "-f", "s16le", "-fs", str(bound), "pipe:1"],
                       stdin=subprocess.DEVNULL, stdout=pcm, check=True, timeout=120)
        size = pcm.tell()
        if not 2 * RATE <= size < bound or size % 2:
            raise ValueError("Durée PCM trop courte ou hors limite de préparation.")

        def read(start, stop):
            pcm.seek(start * 2)
            return pcm.read((stop - start) * 2)

        yield size // 2, read


def transcribe_file(path, decoder, language, progress):
    with pcm_file(path) as (samples, read):
        observed, segments, languages = [], [], {}
        stride = STRIDE * RATE
        pad = OVERLAP * RATE
        for low in range(0, samples, stride):
            high = min(samples, low + stride)
            start, stop = max(0, low - pad), min(samples, high + pad)
            detected, spans = decoder.decode(read(start, stop), language, start / RATE)
            for span in spans:
                owned = tuple(w for w in span.words if low <= round(w.start * RATE) < high)
                # Keep one owner for each time point where windows overlap.
                if owned or low <= round(span.start * RATE) < high:
                    segments.append({"start": span.start, "end": span.end, "text": span.text,
                                     "no_speech": span.no_speech})
                if span.no_speech <= .65:
                    observed.extend(owned)
                    languages[detected] = languages.get(detected, 0) + len(owned)
            progress(high / samples)
        return (samples / RATE, max(languages, key=languages.get, default=language or "auto"),
                sorted(observed, key=lambda w: w.start), segments)
