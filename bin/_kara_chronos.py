"""Private, input-bound CHRONOS preparation shared by Kara and BlackBox."""
from functools import lru_cache
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import re
from shutil import disk_usage
import subprocess
from tempfile import TemporaryDirectory
from time import monotonic

from _kara_import_resources import lease
from _kara_paths import BIN, CACHE
from _kara_prepare import atomic, file_token, lrc_token, path_id, read_source
from _kara_stems import duration as audio_duration
from _kara_turbo import digest
from _parole_process import Interrupted, run
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc, source_orders

ROOT = CACHE / "kara/chronos"
ENGINES = ("huang", "wav2vec2", "huang-ft")
MAX_RESULT = 850_000
MAX_CACHE = 128 * 1024**2
SETTINGS = {"duration_limit_s": 600, "threads": 2, "turbo_window_s": 25,
            "lexical_window_s": .65, "lexical_confidence": .25,
            "acoustic_margin": -3, "acoustic_impostor": -.5, "deep_difficulty": .20}

ASSETS = {
    "model.py": "a7c7f7db24cd26d80b975e2a3b0a13beeebc0f71117eb93d6bec480c5046862f",
    "utils.py": "1d48ae2799ccb713f31182371784b096ec0164e4ef0c4b5462428c85d1799b0e",
    "checkpoint_Baseline": "d541bde0a0e2759c5eac1ee7b70e06169a2aada504bc2359a98c7de4344d4dfe",
    "vocals.onnx": "3d05709ff7197bbd4a33aee759e4e82002acb491f41c94770227ab57507b6ccb",
    "ggml-large-v3-turbo-q5_0.bin": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
    "model_int8.onnx": "74174710e34035bbb7f611601d016c32fc575de7a6f53b1078107dc10a84e7ae",
    "vocab.json": "d732ab2456c0c017930001dc9af0b41b3b93d25b2eb9740bf9d925508d7d87d0",
    "04573f0d-f3cf25b2.th": "f3cf25b222c4eed7cd49dd8b2c9597d50c18bd154090f7b919cfa5f93cf22c49",
    "955717e8-8726e21a.th": "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4",
}


def local_tokens(song):
    return [file_token(song), lrc_token(song.with_suffix(".lrc")), lrc_token(song.with_suffix(".txt"))]


def source_for(song, cache, *, fallback=None, include_draft=False, prefer_timed=False):
    # The exact editor/source text and order remain available when no time
    # survives alignment. A validated legacy receipt supplies its own order.
    before = local_tokens(song)
    lrc, plain = song.with_suffix(".lrc"), song.with_suffix(".txt")
    if prefer_timed and before[1]:
        return {"kind": "sidecar-lrc", "original": read_source(lrc).decode("utf-8"), "timed": True}
    reference = cache.cached(song, verify=False)
    if not reference and (not include_draft or prefer_timed):
        reference = cache.cached(song, verify=False, insufficient=True)
    if before[2]:
        raw = read_source(plain).decode("utf-8")
        if raw.strip():
            return {"kind": "sidecar-text", "original": raw, "timed": False}
    if reference and reference.get("source_lrc"):
        return {"kind": reference["source"]["kind"], "provider": reference["source"].get("provider"),
                "original": reference["source_lrc"], "timed": True,
                "order": reference.get("source_order", "timestamps")}
    if before[1]:
        return {"kind": "sidecar-lrc", "original": read_source(lrc).decode("utf-8"), "timed": True}
    if fallback is None:
        from _musique_oz import probe
        tags, _ = probe(song)
        fallback = next((tags[key] for key in ("lyrics", "unsyncedlyrics", "lyricseng", "lyricsxxx")
                         if tags.get(key)), "")
    if not fallback and include_draft:
        from _parole_transcribe import draft_source

        if draft := draft_source(cache, song):
            return draft
    return {"kind": "embedded" if fallback else "unavailable", "original": fallback, "timed": False}


def reference_lines(source):
    text = source["original"].removeprefix("\ufeff")
    if len(text.encode()) > MAX_LRC_BYTES:
        raise ValueError("Les paroles dépassent la taille admise par Kara.")
    if source["timed"] or parse_lrc(text):
        return [row for _, row in source_orders(text).get(source.get("order"), parse_lrc(text)) if row]
    return [row.strip() for row in text.splitlines() if row.strip()]


def timed_lines(source, duration):
    """Accept supplied line onsets on the file clock for display only."""
    if not source["timed"] or not isfinite(duration) or duration <= 0:
        return []
    rows = [(stamp, text) for stamp, text in parse_lrc(source["original"]) if text]
    if not rows or any(not 0 <= stamp < duration for stamp, _ in rows):
        return []
    return [{"text": text, "start": stamp, "words": [], "status": "source-timing",
             "reason": "lrc-line-onset"} for stamp, text in rows]


def displayable(experiment):
    return bool(experiment and display_index(experiment["lines"])[0])


def display_receipt(result):
    if result and result["status"] in ("abstained", "source-timing"):
        lines = timed_lines(result["source"], result["duration"])
        if lines:
            return dict(result, engine="LRC", status="source-timing", lines=lines)
    return result


def cache_path(song, engine):
    return ROOT / (path_id(song) + "-" + engine + ".json")


def load_json(path, maximum=MAX_RESULT):
    with path.open("rb") as stream:
        data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise ValueError("Reçu CHRONOS trop volumineux.")
    return json.loads(data)


def display_index(lines):
    # Missing lines stay in the text index. Only observed onsets enter the
    # ordered time index, and the original score never consumes this index.
    times, indices = [], []
    for index, line in enumerate(lines):
        start = line.get("start")
        if type(start) in (int, float) and isfinite(start) and start >= 0 and (not times or start > times[-1]):
            times.append(start)
            indices.append(index)
    return times, indices


def cached(song, engine, *, cache=None):
    try:
        result = load_json(cache_path(song, engine))
        if (result["version"] == 1 and result["status"] in ("experimental", "abstained", "source-timing")
                and result["engine"] == engine and result["tokens"] == local_tokens(song)
                and result["implementation"] == implementation()):
            if result.get("source", {}).get("kind") == "generated-transcription":
                from _kara_prepare import PreparedCache
                from _parole_transcribe import draft_source

                if draft_source(cache or PreparedCache(), song) != result["source"]:
                    return None
            return result
    except (OSError, ValueError, KeyError, TypeError):
        pass


def playback_experiment(song, cache, engine, *, before=None):
    if not engine:
        return None
    try:
        before = local_tokens(song) if before is None else before
    except (OSError, ValueError):
        return None
    experiment = cached(song, engine, cache=cache)
    if experiment:
        return display_receipt(experiment)
    if before[1] or before[2]:
        return None
    if engine != "huang":
        candidate = cached(song, "huang", cache=cache)
        if candidate and candidate.get("source", {}).get("kind") == "generated-transcription":
            return display_receipt(candidate)
    from _parole_transcribe import draft_display

    return draft_display(cache, song, before)


@lru_cache(maxsize=1)
def implementation():
    # This fileset defines interpretation of cached decisions across updates.
    return sha256("".join(digest(path) for path in sorted(BIN.glob("_kara_chronos*.py"))
                          if not path.is_symlink()).encode()).hexdigest()


def guard(directory, *, require_quiet=True):
    from _parole_night import mains_online
    from _parole_resources import memory_psi, multimedia_active, thermal_admission

    if mains_online() is not True:
        return "Alimentation secteur requise"
    if disk_usage(directory).free < 21 * 1024**3:
        return "Réserve disque CHRONOS"
    memory = Path("/proc/meminfo").read_text()
    available = int(re.search(r"^MemAvailable:\s+(\d+)", memory, re.M)[1])
    if available < 1536 * 1024 or max(memory_psi(Path("/proc/pressure/memory").read_text())) >= 1:
        return "Réserve ou pression mémoire CHRONOS"
    cpu, gpu = [], []
    for folder in Path("/sys/class/hwmon").glob("hwmon*"):
        label = (folder / "name").read_text().strip()
        selected = (cpu if label in ("k10temp", "coretemp", "zenpower") else
                    gpu if label in ("amdgpu", "nvidia", "nouveau", "xe", "i915") else None)
        if selected is not None:
            selected.extend(int(sensor.read_text()) / 1000 for sensor in folder.glob("temp*_input"))
    if not cpu or not thermal_admission({"night_cpu_temperature": 95, "night_gpu_temperature": 92,
                                         "temperature": 95}, cpu, gpu, nocturnal=bool(gpu)):
        return "Température hors limites CHRONOS"
    if require_quiet and multimedia_active():
        return "Lecture en cours. Kara garde la priorité."


def calculate(song, source, engine, *, expected_audio=None, allow_demucs=True, for_party=False):
    """Run one explicit alignment under the existing Parole process supervisor."""
    if engine not in (*ENGINES, "edition") or not allow_demucs and engine != "huang":
        raise ValueError("Moteur CHRONOS inconnu.")
    python = Path(os.environ.get("KARA_CHRONOS_PYTHON", ""))
    if not python.is_absolute() or not python.is_file():
        raise ValueError("Python CHRONOS local indisponible.")
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    # One admission per host using the existing model lease inside the scope.
    # The private transient directory is removed even when the worker is cancelled.
    with TemporaryDirectory(prefix="draft-", dir=ROOT) as temporary:
        scratch = Path(temporary)
        atomic(scratch / "source.json", json.dumps(source, ensure_ascii=False).encode())
        last_check, reason = 0., None

        def stop():
            nonlocal last_check, reason
            if monotonic() >= last_check + 1:
                last_check = monotonic()
                try:
                    reason = guard(scratch, require_quiet=not for_party)
                except (OSError, ValueError, TypeError, subprocess.SubprocessError):
                    reason = "Mesures système CHRONOS indisponibles"
            return reason is not None

        args = ["systemd-run", "--user", "--scope", "--quiet", "-p", "MemoryMax=8G",
                "-p", "MemorySwapMax=0", "-p", "CPUQuota=200%", "-p", "RuntimeMaxSec=360", "nice", "-n", "8", str(python), "-B",
                str(BIN / "_kara_chronos_worker.py"), str(song), str(scratch), engine,
                expected_audio or ""]
        try:
            code, output = run(args, stop=stop, timeout=365, maximum=32_768,
                               env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="2",
                                        MKL_NUM_THREADS="2", PYTHONNOUSERSITE="1",
                                        KARA_CHRONOS_TIMED_PARTY="1" if for_party else "0",
                                        KARA_CHRONOS_ALLOW_DEMUCS="1" if allow_demucs else "0"))
        except Interrupted:
            raise RuntimeError(reason) from None
        if code:
            raise RuntimeError(output.decode(errors="replace").strip()[-600:] or
                               f"Exécution CHRONOS indisponible (sortie {code}).")
        return load_json(scratch / "result.json")


def prepare(song, cache, engine, *, supplied=None, allow_demucs=True, for_party=False, source_clock=None,
            expected_tokens=None):
    before = local_tokens(song)
    if expected_tokens is not None and expected_tokens != before:
        raise ValueError("Le morceau ou les paroles ont changé avant la préparation CHRONOS.")
    source = supplied if supplied is not None else source_for(song, cache)
    if not reference_lines(source):
        raise ValueError("Aucune parole de référence disponible pour cet enregistrement.")
    key = sha256(json.dumps(source, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    previous = cached(song, engine, cache=cache)
    same_source = previous and previous["source_sha256"] == key
    if (same_source and previous["status"] != "source-timing"
            and (not allow_demucs or previous.get("separation_allowed", True))):
        return previous["status"]
    admission = {"for_party": True} if for_party else {}
    failure = None
    try:
        result = calculate(song, source, engine, allow_demucs=allow_demucs, **admission)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.SubprocessError) as error:
        # Keep a privately retrieved LRC playable even when admission fails.
        # Its line times have source provenance and never qualify for scoring.
        if not source_clock or same_source:
            raise
        failure = error
        seconds, lines = source_clock
        result = {"version": 1, "implementation": implementation(), "engine": engine,
                  "status": "source-timing", "source": source, "duration": seconds,
                  "lines": lines, "audio_sha256": digest(song)}
    if (local_tokens(song) != before or supplied is None and source_for(song, cache) != source
            or source["kind"] == "generated-transcription"
            and source_for(song, cache, include_draft=True) != source):
        raise ValueError("Le morceau ou les paroles ont changé pendant la préparation CHRONOS.")
    result.update(tokens=before, source_sha256=key, separation_allowed=allow_demucs)
    from _kara_paths import music_library
    from _parole_lyrics_lookup import playback_candidates

    if candidates := playback_candidates(song, music_library()):
        # Alternative catalogue evidence is diagnostic. It is not supplied
        # to the model and cannot change the input identity or score.
        result["lyrics_candidates"] = candidates
    raw = json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode()
    if len(raw) > MAX_RESULT:
        raise ValueError("Résultat trop volumineux pour le cache CHRONOS.")
    with lease(ROOT / "cache.lock"):
        existing = sorted((path for path in ROOT.glob("*.json")
                           if not path.is_symlink() and path != cache_path(song, engine)),
                          key=lambda path: (path.stat().st_mtime_ns, path.name))
        size = sum(path.stat().st_size for path in existing) + len(raw)
        for old in existing:
            if size <= MAX_CACHE:
                break
            size -= old.stat().st_size
            old.unlink()
        atomic(cache_path(song, engine), raw)
    if failure is not None:
        raise failure
    return result["status"]
