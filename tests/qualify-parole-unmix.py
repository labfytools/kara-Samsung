#!/usr/bin/env python3
"""Compare UMX-HQ à la séparation Demucs déjà qualifiée sur les mêmes extraits."""
from argparse import ArgumentParser, Namespace
from collections import Counter
import json
import os
from pathlib import Path
import runpy
import subprocess
import sys
from time import monotonic
import wave

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import MODEL, digest
from _parole_process import Interrupted

RATE, FFT, HOP = 44_100, 4096, 1024


def worker(model, audio, start, end, destination):
    import numpy as np
    import onnxruntime as ort

    started = monotonic()
    settings = ort.SessionOptions()
    settings.intra_op_num_threads = settings.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), settings, providers=["CPUExecutionProvider"])
    (input_tensor,) = session.get_inputs()
    samples = subprocess.check_output(("ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                                      "-ss", f"{start:.9f}", "-threads", "1", "-i", str(audio),
                                      "-t", f"{end-start:.9f}", "-map", "0:a:0", "-ac", "2", "-ar", str(RATE),
                                      "-f", "f32le", "pipe:1"), timeout=20)
    signal = np.frombuffer(samples, dtype="<f4").reshape(-1, 2).T
    size = signal.shape[-1]
    if not FFT <= size <= RATE * 50:
        raise ValueError("Taille PCM de l’extrait UMX-HQ invalide")
    print('{"phase":"stft"}', flush=True)
    center = np.pad(signal, ((0, 0), (FFT // 2, FFT // 2)), mode="reflect")
    window = np.hanning(FFT + 1)[:-1].astype(np.float32)
    frames = np.lib.stride_tricks.sliding_window_view(center, FFT, axis=-1)[:, ::HOP, :]
    spectrum = np.fft.rfft(frames * window, axis=-1).astype(np.complex64)
    magnitude = np.abs(spectrum)
    tensor = np.ascontiguousarray(magnitude.transpose(0, 2, 1)[None])
    print('{"phase":"unmix"}', flush=True)
    (estimated,) = session.run(None, {input_tensor.name: tensor})
    if estimated.shape != tensor.shape or not np.isfinite(estimated).all():
        raise ValueError("Spectre de sortie UMX-HQ invalide")
    # This export estimates one stem. Reuse the phase of the corresponding
    # mix bin, then normalize overlap-add on the original sample clock.
    vocal = spectrum * (estimated[0].transpose(0, 2, 1) / np.maximum(magnitude, 1e-12))
    blocks = np.fft.irfft(vocal, n=FFT, axis=-1).astype(np.float32) * window
    result = np.zeros_like(center)
    norm = np.zeros(center.shape[-1], dtype=np.float32)
    for i, block in enumerate(blocks.transpose(1, 0, 2)):
        a, b = i * HOP, i * HOP + FFT
        result[:, a:b] += block
        norm[a:b] += window ** 2
    target = slice(FFT // 2, FFT // 2 + size)
    restored = result[:, target] / norm[target]
    pcm = np.clip(restored.T * 32768, -32768, 32767).astype("<i2")
    print('{"phase":"pcm"}', flush=True)
    with wave.open(str(destination), "wb") as wav:
        wav.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        wav.writeframes(pcm.tobytes())
    print(json.dumps(dict(complete=True, seconds=monotonic() - started, frames=size)), flush=True)


def main():
    cli = ArgumentParser(description=__doc__)
    for name in ("history", "workspace", "library", "model", "worker_python"):
        cli.add_argument(name, type=Path)
    cli.add_argument("--stem-only", action="store_true")
    args = cli.parse_args()
    args.workspace = args.workspace.resolve(strict=True)
    args.library = args.library.resolve(strict=True)
    if args.workspace.is_relative_to(args.library) or args.workspace.stat().st_mode & 0o077:
        cli.error("Utiliser un dossier d’essai privé, hors bibliothèque")
    os.umask(0o077)
    bench = runpy.run_path(str(HERE.with_name("qualify-parole-separation.py")))
    read, write, guarded = (bench[key] for key in ("read", "write", "guarded"))
    manifest = args.history / "manifest.json"
    plan = read(manifest)
    if digest(MODEL) != plan["weights"]:
        raise ValueError("Les poids Turbo F16 diffèrent du comparatif original")
    params = Namespace(workspace=args.workspace, annotations=None, library=args.library, profile=None, memory_psi=1.)
    signature = dict(manifest=digest(manifest), engine=digest(HERE), model=digest(args.model), turbo=digest(MODEL))
    with bench["lease"](args.workspace / "campaign.lock", wait=False) as owner:
        if owner is None:
            raise ValueError("Séparation UMX-HQ déjà active")
        marker = args.workspace / "manifest.json"
        if marker.exists() and read(marker) != signature:
            raise ValueError("Les entrées du comparatif UMX-HQ ont changé")
        write(marker, signature)
        genres = Counter()
        for case in plan["cases"]:
            if genres[case["genre"]]:
                continue
            genres[case["genre"]] += 1
            identity = case["id"]
            if not identity.isascii() or not identity.isalnum():
                raise ValueError("Identifiant d’extrait invalide")
            source = Path(case["path"]).resolve(strict=True)
            if not source.is_relative_to(args.library) or digest(source) != case["audio_sha256"]:
                raise ValueError("Le signal diffère du manifeste")
            folder = args.workspace / identity
            folder.mkdir(mode=0o700, exist_ok=True)
            write(folder / "case.json", case)
            stem, output, receipt = folder / "stem.json", folder / "vocals.wav", folder / "turbo.json"
            try:
                if not stem.exists():
                    start = max(0., case["start"] - 7.)
                    span = case["end"] + 7.
                    command = ["bwrap", "--unshare-net", "--die-with-parent", "--ro-bind", "/", "/",
                               "--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp",
                               "--bind", str(folder), str(folder), str(args.worker_python.absolute()), "-B", str(HERE),
                               "--worker", str(args.model.resolve()), str(source), str(start), str(span), str(output)]
                    data = guarded(params, command, stem)
                    write(stem, dict(**data, vocals=str(output), offset=start, sha256=digest(output)))
                if not args.stem_only and not receipt.exists():
                    if digest(output) != read(stem)["sha256"]:
                        raise ValueError("La piste vocale a changé")
                    transcribe = HERE.with_name("qualify-parole-separation.py")
                    data = guarded(params, [sys.executable, "-B", str(transcribe), "--turbo-worker",
                                            str(folder / "case.json"), str(stem), str(receipt), "", str(MODEL), "vocals"],
                                   receipt)
                    write(receipt, data)
                print(f"{case['genre']} · terminé", flush=True)
            except Interrupted as error:
                print(f"{case['genre']} · {error}", flush=True)


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        model, audio, begin, finish, output = sys.argv[2:]
        worker(Path(model), Path(audio), float(begin), float(finish), Path(output))
    else:
        main()
