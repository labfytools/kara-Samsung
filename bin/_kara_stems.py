#!/usr/bin/python3 -B
"""Séparation locale des pistes instrumentales et vocales avec Demucs CPU.

Un seul morceau à la fois, fichiers originaux intacts, publication atomique.
Le cache est réutilisé uniquement si le fichier source est inchangé.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory

MODEL = "htdemucs"
ROOT = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "paroles-strawberry" / "stems"
PYTHON = Path.home() / ".local/share/kara/demucs-venv/bin/python"
CHECKPOINT = Path.home() / ".cache/torch/hub/checkpoints/955717e8-8726e21a.th"
FILES = ("instrumental.flac", "vocals.flac")


def source_signature(song):
    song = Path(song).resolve(strict=True)
    info = song.stat()
    if not song.is_file() or info.st_size < 4096:
        raise ValueError("Audio local invalide")
    data = {"path": str(song), "bytes": info.st_size, "mtime_ns": info.st_mtime_ns, "model": MODEL}
    token = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
    return data, token


def stem_directory(song):
    _, token = source_signature(song)
    return ROOT / token


def cached_stems(song):
    try:
        expected, _token = source_signature(song)
        directory = stem_directory(song)
        if json.loads((directory / "meta.json").read_text()) != expected:
            return None
        files = tuple(directory / name for name in FILES)
        if not all(p.is_file() and p.stat().st_size > 4096 for p in files):
            return None
        return files
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return None


def emit(event, **fields):
    try:
        print(json.dumps({"event": event, **fields}, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        pass


def duration(song):
    result = subprocess.run(("ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=noprint_wrappers=1:nokey=1", str(song)),
                            text=True, capture_output=True, timeout=15, check=True)
    return float(result.stdout.strip())


def separate(song):
    original = Path(song).resolve(strict=True)
    meta, token = source_signature(original)
    existing = cached_stems(original)
    if existing:
        emit("progress", percent=100, status="Pistes vocales déjà préparées")
        return existing
    if not PYTHON.is_file() or not CHECKPOINT.is_file():
        raise RuntimeError("Modèle Demucs absent ou installation incomplète")
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    output = ROOT / token
    with TemporaryDirectory(prefix="processing-", dir=ROOT) as temporary:
        draft = Path(temporary)
        result_dir = draft / "demucs"
        command = (str(PYTHON), "-m", "demucs", "--two-stems=vocals", "-n", MODEL,
                   "-d", "cpu", "--shifts", "0", "--overlap", "0.1", "--segment", "7",
                   "--flac", "-j", "1", "-o", str(result_dir), str(original))
        env = os.environ.copy()
        env.update({"OMP_NUM_THREADS": "6", "MKL_NUM_THREADS": "6", "OPENBLAS_NUM_THREADS": "1"})
        emit("progress", percent=2, status="Chargement du modèle de séparation")
        with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              env=env, bufsize=0) as process:
            progress = 2
            buffer = ""
            try:
                while data := os.read(process.stdout.fileno(), 2048):
                    buffer = (buffer + data.decode("utf-8", "replace"))[-2048:]
                    matches = re.findall(r"(?:^|\r)\s*(\d{1,3})%\|", buffer)
                    if matches:
                        pct = min(100, int(matches[-1]))
                        value = min(92, max(progress, 3 + round(pct * .89)))
                        if value > progress:
                            progress = value
                            emit("progress", percent=progress, status="Séparation voix / instruments")
                code = process.wait()
            except BaseException:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                raise
            if code:
                raise RuntimeError("Demucs a interrompu la séparation (ressources ou modèle)")
        stem_dir = result_dir / MODEL / original.stem
        files = (stem_dir / "no_vocals.flac", stem_dir / "vocals.flac")
        if not all(p.is_file() and p.stat().st_size > 4096 for p in files):
            raise RuntimeError("Les deux pistes n'ont pas été produites")
        emit("progress", percent=95, status="Vérification de la synchronisation")
        timings = [duration(path) for path in (original, *files)]
        if max(timings) - min(timings) > .35:
            raise RuntimeError("Les pistes ne sont pas synchronisées avec l'original")
        ready = draft / "ready"
        ready.mkdir(mode=0o700)
        for file, target in zip(files, FILES):
            shutil.move(str(file), str(ready / target))
        (ready / "meta.json").write_text(json.dumps(meta, sort_keys=True))
        os.chmod(ready / "meta.json", 0o600)
        if output.exists():
            # Ne jamais écraser les pistes d'une autre préparation valide.
            if cached_stems(original):
                emit("progress", percent=100, status="Pistes déjà présentes")
                return cached_stems(original)
            shutil.rmtree(output)
        ready.rename(output)
    emit("progress", percent=100, status="Pistes instrumentales et vocales prêtes")
    return cached_stems(original)


if __name__ == "__main__":
    try:
        def cancel(_sig, _frame):
            raise KeyboardInterrupt("Séparation interrompue")
        signal.signal(signal.SIGTERM, cancel)
        paths = separate(Path(sys.argv[1]))
        if not paths:
            raise RuntimeError("Cache de séparation invalide")
        emit("complete", instrumental=str(paths[0]), vocals=str(paths[1]), model=MODEL)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError, KeyboardInterrupt) as error:
        emit("error", message=str(error) or "Séparation annulée")
        raise SystemExit(1)
