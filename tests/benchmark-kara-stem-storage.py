#!/usr/bin/env python3
"""Comparer le FLAC courant à un niveau d'encodage FFmpeg, en lecture silencieuse."""

import argparse
import json
from pathlib import Path
import statistics
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

import gi

gi.require_version("Gtk", "4.0")
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))

from _kara_audio_identity import pcm_digest
from _kara_stem_playback import StemPlayback
from _kara_stems import FILES, stem_layout


def load_seconds(pair):
    player = StemPlayback()
    began = monotonic()
    try:
        # Use Kara's two decode branches and mixer. The appsink branch has
        # the same preroll contract and never opens a real output device.
        player._set_pipeline(pair, None)
        return monotonic() - began
    finally:
        player.disable()


def layout(path):
    info = path.stat()
    return stem_layout(path, info.st_size, info.st_mtime_ns)


def experiment(source, draft, level):
    baseline = tuple((source / name).resolve(strict=True) for name in FILES)
    candidate = tuple(draft / name for name in FILES)
    encodes = []
    initial = [layout(path) for path in baseline]
    if initial[0] != initial[1]:
        raise ValueError("Paire FLAC source désynchronisée")
    for old, new in zip(baseline, candidate, strict=True):
        began = monotonic()
        subprocess.run(("ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(old),
                        "-map", "0:a:0", "-c:a", "flac", "-compression_level", str(level),
                        str(new)), capture_output=True, check=True, timeout=60)
        encodes.append(monotonic() - began)
        if layout(new) != layout(old) or pcm_digest(new) != pcm_digest(old):
            raise ValueError("Le réencodage a modifié le PCM ou son origine temporelle")
    # Alternate both variants in the same process to balance warm file pages
    # and GStreamer initialization. Preroll excludes the physical sound path.
    samples = [[], []]
    for order in ((0, 1), (1, 0)) * 5:
        for i in order:
            samples[i].append(load_seconds((baseline, candidate)[i]))
    return {
        "layout": {"sample_rate": initial[0][0], "frames_per_channel": initial[0][1], "origin_frames": 0},
        "pcm_equal": True,
        "input_bytes": {p.name: p.stat().st_size for p in baseline},
        "output_bytes": {p.name: p.stat().st_size for p in candidate},
        "input_allocated": sum(p.stat().st_blocks * 512 for p in baseline),
        "output_allocated": sum(p.stat().st_blocks * 512 for p in candidate),
        "encode_seconds": encodes,
        "preroll_seconds": samples,
        "preroll_median_seconds": [statistics.median(values) for values in samples],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--level", choices=range(13), type=int, default=8)
    parser.add_argument("sources", type=Path, nargs="+")
    args = parser.parse_args()
    for source in args.sources:
        with TemporaryDirectory(dir=args.workspace, prefix="flac-") as folder:
            result = experiment(source, Path(folder), args.level)
            print(json.dumps({"case": source.name, "compression_level": args.level, **result}), flush=True)


if __name__ == "__main__":
    main()
