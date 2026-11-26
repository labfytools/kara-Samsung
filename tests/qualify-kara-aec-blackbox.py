#!/usr/bin/env python3
"""Rejoue le moniteur et le micro témoin BlackBox sans ouvrir de flux."""
from argparse import ArgumentParser
from collections import OrderedDict
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

import numpy as np
from scipy.signal import correlate

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_aec_prototype import FRAME, RATE, Speex, WebRtc, build_webrtc, find_delay, process, with_engine
from _kara_blackbox import events, private_file, validate_recording, wave_layout

SECONDS = 12


def matched_cycles(folder):
    pending = OrderedDict()
    run, best, previous = [], [], None
    for row in events(folder / "events.jsonl"):
        if row["event"] != "pcm":
            continue
        data = row["data"]
        track = data["track"]
        if track not in ("monitor", "witness"):
            continue
        clock = data["clock"]
        key = row["turn"], clock["epoch"], clock["driver"], clock["frame"]
        cycle = pending.setdefault(key, {})
        if track in cycle:
            raise ValueError("Plusieurs pistes occupent le même cycle PipeWire")
        cycle[track] = data
        if len(cycle) != 2:
            # Keep a bounded allowance for interleaved logger writes.
            if len(pending) > 64:
                pending.popitem(last=False)
            continue
        del pending[key]
        if cycle["monitor"]["frames"] != cycle["witness"]["frames"]:
            run, previous = [], None
            continue
        if previous != key:
            run = []
        frames = cycle["monitor"]["frames"]
        run.append(((run[-1][0] if run else 0) + frames, cycle))
        previous = (*key[:3], key[-1] + frames)
        if not best or run[-1][0] > best[-1][0]:
            best = run.copy()
        if best[-1][0] >= SECONDS * RATE:
            break
    return [pair for _, pair in best]


def signal(folder, cycles, track, channels):
    data = []
    layouts = {}
    for pair in cycles:
        chunk = pair[track]
        path = folder / chunk["file"]
        if path not in layouts:
            layouts[path] = wave_layout(path)
        rate, actual_channels, frames, width, offset = layouts[path]
        if (rate, actual_channels, width) != (RATE, channels, 4) or not (
                0 <= chunk["sample"] < chunk["sample"] + chunk["frames"] <= frames):
            raise ValueError("Piste PipeWire incompatible avec la grille du rejeu")
        with open(path, "rb", opener=private_file) as stream:
            stream.seek(offset + chunk["sample"] * width * channels)
            raw = stream.read(chunk["frames"] * width * channels)
        if len(raw) != chunk["frames"] * width * channels:
            raise ValueError("PCM BlackBox incomplet")
        data.append(np.frombuffer(raw, dtype="<f4").reshape(-1, channels))
    samples = np.concatenate(data, axis=0)
    end = min(SECONDS * RATE, samples.shape[0])
    return samples[:end - end % FRAME]


def rms(value):
    return float(np.sqrt(np.mean(value.astype(np.float64) ** 2)))


def run(folder):
    manifest = json.loads((folder / "manifest.json").read_text())
    validated = validate_recording(folder, manifest)
    if not validated.get("valid"):
        raise ValueError("Capture BlackBox non validée")
    cycles = matched_cycles(folder)
    if not cycles or sum(pair["monitor"]["frames"] for pair in cycles) < 4 * RATE:
        raise ValueError("Aucune fenêtre commune continue d’au moins quatre secondes")
    render = signal(folder, cycles, "monitor", 2)
    witness = signal(folder, cycles, "witness", 1).ravel()
    if not np.isfinite(render).all() or not np.isfinite(witness).all():
        raise ValueError("Valeur non finie dans la capture")
    reference = render.mean(axis=1)
    common = correlate(witness, reference, mode="full", method="fft")
    start = witness.size - 1
    lag = int(np.argmax(np.abs(common[start:start + RATE // 2])))
    # Estimate a delay on the input only. Use the identical segments and
    # the same reference for both engines, with no claim of clean voice.
    def projection(value):
        end = min(value.size, reference.size + lag)
        base = reference[:end - lag].astype(np.float64)
        observed = value[lag:end].astype(np.float64)
        scale = np.linalg.norm(base) * np.linalg.norm(observed)
        return float(np.dot(base, observed) / scale) if scale > 0 else None

    baseline = projection(witness)
    results = {"seconds": witness.size / RATE, "delay_candidate_ms": lag * 1000 / RATE,
               "input_correlation": baseline, "monitor_rms": rms(reference), "witness_rms": rms(witness)}
    with TemporaryDirectory(prefix="kara-aec-replay-") as directory:
        library = Path(directory) / "webrtc.so"
        build_webrtc(library)
        for label, factory in (("webrtc", lambda channels: WebRtc(library, channels)), ("speex", Speex)):
            output, _, wall = with_engine(factory, 2, process, render, witness, round(lag * 1000 / RATE))
            delay = find_delay(factory)
            shifted = output[delay:]
            results[label] = {"rms": rms(shifted), "correlation": projection(shifted),
                              "engine_delay_ms": delay * 1000 / RATE,
                              "frame_p95_us": float(np.percentile(wall, 95))}
    return results


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("session", type=Path)
    args = parser.parse_args()
    print(json.dumps(run(args.session.resolve(strict=True)), indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
