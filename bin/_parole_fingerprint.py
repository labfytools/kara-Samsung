"""Optional, local Chromaprint evidence for audio that uses different encodings."""
from dataclasses import dataclass
import json
import os
from pathlib import Path
from shutil import which
import subprocess
from _kara_import_resources import run_checked

VERSION = 1
ALGORITHM = 2
RATE = 11025
SECONDS = 120
MIN_WORDS = 60
MAX_WORDS = 1024
MAX_SHIFT = 10


@dataclass(frozen=True)
class Fingerprint:
    words: tuple[int, ...]
    millis: int

    def document(self):
        return json.dumps({"version": VERSION, "algorithm": ALGORITHM, "millis": self.millis, "words": self.words},
                          separators=(",", ":"))


def parse(record):
    if not isinstance(record, dict) or record.get("version") != VERSION or record.get("algorithm") != ALGORITHM:
        return None
    words, millis = record.get("words"), record.get("millis")
    if (type(millis) is not int or not 0 < millis <= SECONDS * 1000
            or not isinstance(words, list) or not MIN_WORDS <= len(words) <= MAX_WORDS
            or any(type(value) is not int or not 0 <= value <= 0xffffffff for value in words)
            or len(set(words)) < 16):
        return None
    return Fingerprint(tuple(words), millis)


def executable():
    configured = os.environ.get("KARA_FPCALC", "").strip()
    if configured:
        path = Path(configured)
        return str(path) if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None
    return which("fpcalc")


def read(path, *, tool=None, retain=()):
    tool = tool or executable()
    if not tool:
        return None
    try:
        # Only verified, bounded PCM is routed to fpcalc. The outer decoder
        # owns protocols and cannot open a remote playlist or media segment.
        audio = run_checked(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-protocol_whitelist", "file,pipe", "-i", str(path),
             "-map", "0:a:0", "-t", str(SECONDS), "-vn", "-ac", "1", "-ar", str(RATE), "-f", "s16le", "pipe:1"],
            check=True, capture_output=True, timeout=30, pass_fds=retain).stdout
        if len(audio) % 2 or not 0 < len(audio) <= RATE * SECONDS * 2:
            return None
        output = run_checked(
            [tool, "-format", "s16le", "-rate", str(RATE), "-channels", "1", "-length", str(SECONDS),
             "-algorithm", str(ALGORITHM), "-raw", "-json", "-"], input=audio,
            check=True, capture_output=True, timeout=15, pass_fds=retain).stdout
        if len(output) > 16 * 1024:
            return None
        payload = json.loads(output)
        if not isinstance(payload, dict):
            return None
        return parse({"version": VERSION, "algorithm": ALGORITHM, "millis": round(len(audio) * 500 / RATE),
                      "words": payload.get("fingerprint")})
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def similarity(left, right):
    if min(left.millis, right.millis) < .8 * max(left.millis, right.millis):
        return None
    best = None
    # Chromaprint words contain 32 bits. This bounded offset search accounts
    # for small encoder delays. It is not an estimate of LRC or PCM alignment.
    for shift in range(-MAX_SHIFT, MAX_SHIFT + 1):
        a, b = max(0, shift), max(0, -shift)
        count = min(len(left.words) - a, len(right.words) - b)
        if count < MIN_WORDS or count < .8 * max(len(left.words), len(right.words)):
            continue
        distance = sum((left.words[a + i] ^ right.words[b + i]).bit_count() for i in range(count))
        value = 1 - distance / (32 * count)
        best = value if best is None else max(best, value)
    return best
