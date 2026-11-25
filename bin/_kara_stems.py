#!/usr/bin/python3 -B
"""Séparation locale des pistes instrumentales et vocales avec Demucs CPU.

Un seul morceau à la fois, fichiers originaux intacts, publication atomique.
Le cache est réutilisé uniquement si le fichier source est inchangé.
"""
import hashlib
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
from tempfile import TemporaryDirectory

from _kara_paths import CACHE, kara_data
from _kara_audio_identity import pcm_digest
from _kara_import_resources import lease

MODEL = "htdemucs"
ROOT = CACHE / "paroles-strawberry/stems"
PYTHON = kara_data("demucs-venv/bin/python")
CHECKPOINT = Path(os.environ.get("TORCH_HOME") or CACHE / "torch") / "hub/checkpoints/955717e8-8726e21a.th"
FILES = ("instrumental.flac", "vocals.flac")
PARAMETERS = {"engine": "demucs", "model": MODEL, "stem": "vocals", "shifts": 0,
              "overlap": 0.1, "segment": 7, "format": "flac"}


def model_signature():
    info = CHECKPOINT.stat()
    return {"settings": PARAMETERS, "checkpoint": [info.st_size, info.st_mtime_ns]}


def read_metadata(directory):
    with (directory / "meta.json").open("rb") as source:
        return json.loads(source.read(16385))


def stem_files(directory):
    files = tuple(directory / name for name in FILES)
    if all(p.is_file() and p.stat().st_size > 4096 for p in files) and aligned_pair(files):
        return files
    return None


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
        data = read_metadata(directory)
        if data.get("source", data) != expected:
            return None
        if "processing" in data and data["processing"] != model_signature():
            return None
        return stem_files(directory)
    except (OSError, ValueError, TypeError, KeyError, IndexError, subprocess.SubprocessError):
        return None


@lru_cache(maxsize=128)
def stem_layout(path, size, mtime_ns):
    # Cache by file identity metadata. Old Demucs caches pass the same
    # admission check as new generations without rewriting their metadata.
    result = subprocess.run(("ffprobe", "-v", "error", "-select_streams", "a:0",
                             "-show_entries", "stream=sample_rate,channels,start_pts,duration_ts,time_base",
                             "-of", "json", str(path)), check=True, capture_output=True, timeout=4)
    stream = json.loads(result.stdout)["streams"][0]
    rate = int(stream["sample_rate"])
    frames = int(stream["duration_ts"])
    if (stream["start_pts"], stream["time_base"], stream["channels"]) != (0, f"1/{rate}", 2):
        raise ValueError("Les pistes Demucs ne partagent pas une origine PCM stéréo")
    if frames <= 0 or rate <= 0:
        raise ValueError("Durée PCM de piste Demucs invalide")
    # The FLAC header alone cannot detect a damaged frame mid-song.
    subprocess.run(("ffmpeg", "-v", "error", "-xerror", "-nostdin", "-i", str(path),
                    "-map", "0:a:0", "-f", "null", "-"),
                   check=True, capture_output=True, timeout=15)
    return rate, frames


def aligned_pair(files):
    layouts = []
    for path in files:
        info = path.stat()
        layouts.append(stem_layout(path, info.st_size, info.st_mtime_ns))
    return layouts[0] == layouts[1]


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
    # The desktop controller, anticipation and the CLI use the same slot.
    # A child holding the slot always finishes publishing before it is reused.
    with lease(ROOT / "demucs.lock", wait=False) as lock:
        if lock is None:
            raise RuntimeError("Une séparation Demucs est déjà en cours")
        return separate_locked(song)


def separate_locked(song):
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
    before = original.stat()
    audio = pcm_digest(original)
    processing = model_signature()
    checksum = hashlib.sha256(json.dumps([audio, processing], sort_keys=True).encode()).hexdigest()
    registry = ROOT / "identities"
    registry.mkdir(mode=0o700, exist_ok=True)
    pointer = registry / checksum
    with TemporaryDirectory(prefix="processing-", dir=ROOT) as temporary:
        draft = Path(temporary)
        (draft / ".parole-stage").touch()
        try:
            good_id = pointer.read_text()
            cached = ROOT / good_id if re.fullmatch(r"[a-f0-9]{64}", good_id) else None
            data = read_metadata(cached) if cached else {}
            reuse = stem_files(cached) if data.get("audio") == audio and data.get("processing") == processing else None
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            reuse = None
        if reuse:
            publish(draft, output, original, before, meta, audio, processing, reuse, copy=True)
            emit("progress", percent=100, status="Pistes réassociées au contenu audio")
            return cached_stems(original)
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
        if not aligned_pair(files) or max(timings) - min(timings) > .35:
            raise RuntimeError("Les pistes ne sont pas synchronisées avec l'original")
        publish(draft, output, original, before, meta, audio, processing, files)
        candidate = draft / "pointer"
        candidate.write_text(token)
        candidate.replace(pointer)
    emit("progress", percent=100, status="Pistes instrumentales et vocales prêtes")
    return cached_stems(original)


def publish(draft, output, original, before, meta, audio, processing, files, copy=False):
    after = original.stat()
    # Detect metadata edits or content replacement before either output is
    # made visible. Existing accepted directories are never overwritten.
    if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns):
        raise ValueError("Audio modifié pendant la préparation")
    ready = draft / "ready"
    ready.mkdir(mode=0o700)
    for file, target in zip(files, FILES, strict=True):
        path = ready / target
        if copy:
            os.link(file, path)
        else:
            shutil.move(str(file), str(path))
        with path.open("rb") as stream:
            os.fsync(stream.fileno())
    envelope = {"source": meta, "processing": processing, "audio": audio}
    manifest = ready / "meta.json"
    with manifest.open("w") as stream:
        os.chmod(manifest, 0o600)
        json.dump(envelope, stream, sort_keys=True)
        stream.flush()
        os.fsync(stream.fileno())
    if output.exists():
        if cached_stems(original):
            return
        # Preserve an older or corrupt generation for diagnosis and to avoid
        # unlinking data while a separately started V2 session has it open.
        output.rename(draft.parent / ("obsolete-" + draft.name))
    ready.rename(output)
    for parent in (output, output.parent):
        fd = os.open(parent, os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


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
