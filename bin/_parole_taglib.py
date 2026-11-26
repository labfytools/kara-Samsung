"""Optional TagLib reader for inventory; unsupported metadata goes to FFprobe."""
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shlex
import subprocess
from tempfile import TemporaryDirectory

from _kara_paths import BIN, DATA, kara_data

FORMATS = {".flac": {"flac"}, ".opus": {"opus"}, ".ogg": {"vorbis"}, ".oga": {"vorbis"},
           ".mp3": {"mp3"}, ".m4a": {"aac", "alac"}}
MAX_OUTPUT = 1024 * 1024
HELPER = "taglib/parole-tags-v1"


@dataclass(frozen=True)
class Reading:
    tags: dict
    artwork: bool


def helper():
    configured = os.environ.get("KARA_TAGLIB_READER")
    path = Path(configured) if configured else kara_data(HELPER)
    return path if path.is_absolute() and path.is_file() and os.access(path, os.X_OK) else None


def read(path, executable):
    codecs = FORMATS.get(path.suffix.lower())
    if executable is None or not codecs:
        return None
    try:
        raw = subprocess.run((executable, path), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             check=True, timeout=3).stdout
        if len(raw) > MAX_OUTPUT or not raw.endswith(b"\0"):
            return None
        magic, duration, codec, artwork, *fields = raw[:-1].decode("utf-8").split("\0")
        if (magic != "parole-tags-v1" or codec not in codecs or artwork not in ("0", "1")
                or len(fields) % 2 or not duration.isascii() or not duration.isdecimal()
                or len(duration) > 10 or not 0 < int(duration) <= 2**31 - 1):
            return None
        tags = {}
        for name, value in zip(fields[::2], fields[1::2]):
            # A named lyric frame or compound credit requires the established
            # reader if this representation would lose its distinction.
            if name.startswith("LYRICS:"):
                return None
            name = re.sub(r"[^a-z0-9]", "", name.casefold())
            if not name or name in tags:
                return None
            if value.strip():
                tags[name] = value.strip()
        # A container can be readable while its tag flavour is outside the
        # property map (for example a QuickTime keyed-metadata-only M4A).
        if not {"title", "artist", "album"} <= tags.keys():
            return None
        tags.update(codec=codec, duration_seconds=int(duration) / 1000)
        return Reading(tags, artwork == "1")
    except (OSError, UnicodeError, ValueError, subprocess.SubprocessError):
        return None


def build(destination):
    flags = subprocess.check_output(("pkg-config", "--cflags", "--libs", "taglib"), text=True, timeout=10)
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with TemporaryDirectory(prefix=".taglib-", dir=destination.parent) as directory:
        output = Path(directory) / "parole-tags"
        subprocess.run(("c++", "-std=c++17", "-O2", "-Wall", "-Wextra", "-Werror",
                        str(BIN.parent / "native/src/parole_tags.cpp"), "-o", str(output), *shlex.split(flags)),
                       check=True, timeout=60)
        output.chmod(0o700)
        os.replace(output, destination)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Compiler le lecteur facultatif TagLib pour Parole.")
    parser.add_argument("--destination", type=Path, default=DATA / "kara" / HELPER)
    args = parser.parse_args()
    build(args.destination.resolve())
    print(args.destination)
