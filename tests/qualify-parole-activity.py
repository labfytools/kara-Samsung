#!/usr/bin/env python3
"""Mesure la couverture des paroles annotées par un détecteur d’activité."""
from argparse import ArgumentParser
from collections import defaultdict
from csv import DictReader
import json
from math import isfinite
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_paths import DATA
from _kara_turbo import MAX_SECONDS, RATE

WINDOW = RATE // 10


def build(target):
    root = Path(os.environ.get("KARA_WHISPER_ROOT", DATA / "hyprwhspr/pywhispercpp-src")).resolve()
    source = Path(__file__).with_name("kara-vad-probe.c")
    args = ("cc", "-std=c17", "-O2", "-Wall", "-Wextra", "-Werror",
            "-I", str(root / "whisper.cpp/include"), "-I", str(root / "whisper.cpp/ggml/include"),
            str(source), "-L", str(root), "-Wl,-rpath," + str(root), "-lwhisper", "-o", str(target))
    subprocess.run(args, check=True, timeout=30)


def stage(path, output):
    subprocess.run(("ffmpeg", "-v", "error", "-nostdin", "-xerror", "-protocol_whitelist", "file,pipe",
                    "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(RATE), "-f", "f32le",
                    "-fs", str(MAX_SECONDS * RATE * 4), str(output)), check=True, timeout=120)
    length = output.stat().st_size
    if length % 4 or not WINDOW * 4 <= length < MAX_SECONDS * RATE * 4:
        raise ValueError("Durée de PCM hors des limites du banc")
    return np.memmap(output, dtype="<f4", mode="r", shape=(length // 4,))


def membership(centers, ranges, margin=0.):
    included = np.zeros(centers.size, dtype=bool)
    # Annotation rows may overlap. Mark each sample only once.
    for start, stop in ranges:
        low, high = np.searchsorted(centers, [start - margin, stop + margin])
        included[low:high] = True
    return included


def run(mix, vocals, annotations, model, work):
    with annotations.open(newline="") as stream:
        truth = [(start, stop) for row in DictReader(stream)
                 if isfinite(start := float(row["word_start"])) and isfinite(stop := float(row["word_end"]))]
    files = {"mix": work / "mix.f32", "stem": work / "stem.f32"}
    pcm = {name: stage(path, files[name]) for name, path in (("mix", mix), ("stem", vocals))}
    if abs(len(pcm["mix"]) - len(pcm["stem"])) > RATE // 20:
        raise ValueError("Le mix et le stem doivent partager la même horloge")
    count = min(map(len, pcm.values())) // WINDOW
    centers = (np.arange(count) + .5) / 10
    if any(not 0 <= start < stop <= count / 10 + .1 for start, stop in truth):
        raise ValueError("Annotations hors des bornes audio")
    positive = membership(centers, truth)
    gaps = ~membership(centers, truth, .5)
    if not positive.any() or not gaps.any():
        raise ValueError("Le témoin doit contenir des paroles annotées et des intervalles éloignés des repères")

    def score(mask):
        return {"kept": int(mask.sum()), "word_coverage": float((mask & positive).sum() / positive.sum()),
                "gap_rejection": float((~mask & gaps).sum() / gaps.sum())}

    levels = {name: np.sqrt(np.einsum("ij,ij->i", frames, frames) / WINDOW)
              for name, signal in pcm.items() for frames in (signal[:count * WINDOW].reshape(count, WINDOW),)}
    result = {f"stem_rms_{floor}dB": score(levels["stem"] >= 10 ** (floor / 20)) for floor in (-50, -43, -36, -30)}
    result.update({f"stem_mix_{floor}dB": score(levels["stem"] >= levels["mix"] * 10 ** (floor / 20))
                   for floor in (-15, -10, -6)})
    probe = work / "vad-probe"
    build(probe)
    for name, audio in files.items():
        began = monotonic()
        output = subprocess.run((str(probe), str(model), str(audio)), check=True, timeout=90,
                                capture_output=True, text=True).stdout
        elapsed = monotonic() - began
        segments = defaultdict(list)
        for line in output.splitlines():
            threshold, start, stop = map(float, line.split())
            segments[threshold].append((start, stop))
        for level in (.35, .5, .65):
            result[f"silero_{name}_{level}"] = dict(**score(membership(centers, segments[level])), seconds=elapsed)
    return dict(seconds=count / 10, word_bins=int(positive.sum()), gap_bins=int(gaps.sum()), variants=result)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("mix", type=Path)
    parser.add_argument("vocals", type=Path)
    parser.add_argument("annotations", type=Path)
    parser.add_argument("--model", type=Path, default=DATA / "pywhispercpp/models/ggml-silero-v5.1.2.bin")
    args = parser.parse_args()
    if sys.byteorder != "little":
        parser.error("Le PCM float32 du banc est petit-boutiste")
    with TemporaryDirectory(prefix="parole-vad-") as directory:
        result = run(args.mix, args.vocals, args.annotations, args.model, Path(directory))
    print(json.dumps(result, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
