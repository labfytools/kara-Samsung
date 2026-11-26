"""Read-only system checks and bounded synthetic BlackBox trials."""
import argparse
import json
from math import ceil, isfinite
import os
from pathlib import Path
from selectors import DefaultSelector, EVENT_READ
import shutil
import sqlite3
import stat
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep

from _kara_blackbox import BlackBox, DEFAULT_VOICE_BYTES, MIN_FREE, session_folders, validate_recording
from _kara_paths import BIN, DATA, STATE
from _kara_turbo import MODEL

GB = 1_000_000_000
MEASURED_BYTES_PER_HOUR = 5_087_852_400  # Five tracks on the six-second V3 bench.
BASE_BUDGET_PER_HOUR = 6 * GB  # Allow margin for Turbo windows and the event journal.
WITNESS_BUDGET_PER_HOUR = 900_000_000
TURN = {"turn": "a" * 16, "player": "Fixture", "song": "Synthétique"}


def storage_plan(root, hours, witness=False, budget=DEFAULT_VOICE_BYTES):
    root = root.resolve()
    path = root
    while not path.exists():
        path = path.parent
    free = shutil.disk_usage(path).free
    private = (not root.exists() or root.is_dir() and root.stat().st_uid == os.getuid()
               and stat.S_IMODE(root.stat().st_mode) & 0o077 == 0)
    rate = BASE_BUDGET_PER_HOUR + (WITNESS_BUDGET_PER_HOUR if witness else 0)
    if not isfinite(hours) or not 0 < hours < 2 ** 63 / rate:
        raise ValueError("Durée de jeu hors limites pour le budget en octets.")
    planned = ceil(hours * rate)
    return {"hours_of_active_playback": hours, "witness": witness, "bytes_per_hour_planned": rate,
            "bench_bytes_per_hour": MEASURED_BYTES_PER_HOUR, "planned_bytes": planned,
            "session_budget_bytes": budget, "reserve_bytes": MIN_FREE, "free_bytes": free,
            "private_root": private, "fits_session_budget": planned <= budget,
            "fits_free_space": planned + MIN_FREE <= free,
            "max_active_hours_by_session_budget": round(budget / rate, 2)}


def synthetic_packets(box):
    for sequence in range(96):
        box.pcm("wifi", 48000, 1, bytes(1920), sequence=sequence, **TURN)


def crash_writer(root):
    box = BlackBox(root)
    box.start(consent=True, voice=True, tracks=["wifi"])
    box.emit("party", "turn-start", **TURN)
    synthetic_packets(box)
    # The parent should kill an owner with durable indexed PCM and an
    # unfinished RIFF, rather than accidentally exercising an empty session.
    limit = monotonic() + 8
    while box.status(storage=True)["bytes_written"] < 120_000:
        if monotonic() >= limit:
            raise RuntimeError("La fixture d’écriture n’a pas atteint sa barrière.")
        sleep(.02)
    print("ready", flush=True)
    sys.stdin.read(1)


def recovery_trial(root):
    with subprocess.Popen([sys.executable, "-B", __file__, "--crash-writer", str(root)],
                          stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) as process:
        # Timeout and termination apply only to the child owned here.
        try:
            with DefaultSelector() as selector:
                selector.register(process.stdout, EVENT_READ)
                if not selector.select(timeout=15) or process.stdout.readline() != "ready\n":
                    raise RuntimeError("La fixture de récupération a échoué avant la coupure.")
        finally:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)
    box = BlackBox(root)
    box.recover()
    folder, = session_folders(root)
    manifest = json.loads((folder / "manifest.json").read_text())
    verdict = validate_recording(folder, manifest)
    blocks = [path for path in manifest["files"] if path.startswith("audio/")]
    summary = box.status()["latest"]
    return {"ok": verdict["status"] == "incomplete" and bool(blocks) and summary["reason"] == "interrupted",
            "status": verdict["status"], "recovered_wav_blocks": len(blocks),
            "unrecoverable_pcm_events": summary["unrecoverable_pcm_events"]}


def quota_trial(root):
    quota = 100_000
    box = BlackBox(root, max_bytes=quota)
    box.start(consent=True, voice=True, tracks=["wifi"])
    synthetic_packets(box)
    box.stop(wait=True)
    folder, = session_folders(root)
    manifest = json.loads((folder / "manifest.json").read_text())
    verdict = validate_recording(folder, manifest)
    accounted = sum(item["bytes"] for name, item in manifest["files"].items()
                    if name == "events.jsonl" or name.startswith("audio/"))
    try:
        BlackBox(root / "reserve", reserve=shutil.disk_usage(root).total + 1).start(consent=True)
    except OSError as error:
        reserve_refused = "insuffisant" in str(error)
    else:
        reserve_refused = False
    limited = BlackBox(root / "during")
    limited.disk_available = lambda: limited.reserve + 65536
    limited.start(consent=True, voice=True, tracks=["wifi"])
    synthetic_packets(limited)
    limited.stop(wait=True)
    during = limited.status(storage=True)
    runtime_reserve = bool(during["verified"] and "Réserve disque" in (during["error"] or ""))
    return {"ok": verdict["status"] == "incomplete" and accounted <= quota and reserve_refused and runtime_reserve,
            "status": verdict["status"], "capture_bytes": accounted, "quota_bytes": quota,
            "reserve_refused": reserve_refused, "reserve_protected_while_recording": runtime_reserve}


def usb_cursor():
    for path in (STATE / "labfy-music-manager").glob("*/pending-media.sqlite"):
        try:
            with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.5) as db:
                if db.execute("SELECT 1 FROM sqlite_schema WHERE name='sync_cursor'").fetchone():
                    row = db.execute("SELECT completed,total,bytes_done,bytes_total,token IS NOT NULL,"
                                     "last_error IS NOT NULL FROM sync_cursor WHERE id=1").fetchone()
                    if row:
                        return dict(zip(("completed", "total", "bytes_done", "bytes_total",
                                         "step_in_progress", "error_recorded"), row))
        except sqlite3.Error:
            return {"status": "unknown"}
    return None


def service():
    if not shutil.which("systemctl"):
        return {}
    try:
        probe = subprocess.run(["systemctl", "--user", "show", "kara.service", "-p", "ActiveState", "-p", "MainPID"],
                               capture_output=True, text=True, timeout=3, check=True)
        return dict(line.split("=", 1) for line in probe.stdout.splitlines() if "=" in line)
    except (OSError, subprocess.SubprocessError):
        return {}


def diagnostic(root, hours, witness, budget, ctc_python=None, ctc_model=None, ctc_vocab=None):
    plan = storage_plan(root, hours, witness, budget)
    venv = DATA / "hyprwhspr/venv/bin/python"
    from _kara_aec_pipewire import PipeWire, build

    with TemporaryDirectory(prefix="kara-blackbox-diagnostic-") as work:
        work = Path(work)
        recovery = recovery_trial(work / "crash")
        quota = quota_trial(work / "budget")
        model_probe = subprocess.run([str(venv if venv.is_file() else sys.executable), "-B",
                                      str(BIN / "_kara_blackbox_negative.py")],
                                     capture_output=True, text=True, timeout=30)
        negatives = json.loads(model_probe.stdout) if model_probe.stdout.strip() else {"ok": False, "kind": "unavailable"}
        negatives["ok"] = negatives["ok"] and model_probe.returncode == 0
        try:
            library = work / "pipewire.so"
            build(library)
            PipeWire(library, 2).close()
            spa = {"available": True}
        except (OSError, RuntimeError, subprocess.SubprocessError) as error:
            spa = {"available": False, "detail": type(error).__name__}
    ctc = {"configured": bool(ctc_model and ctc_vocab), "available": False}
    if ctc["configured"] and ctc_model.is_file() and ctc_vocab.is_file():
        try:
            worker = subprocess.run([str(ctc_python or sys.executable), "-B", "-c", "import onnxruntime"],
                                    capture_output=True, timeout=10)
            ctc["available"] = worker.returncode == 0
        except (OSError, subprocess.SubprocessError):
            pass
    return {"ready_software": all((plan["private_root"], plan["fits_session_budget"], plan["fits_free_space"],
                                   recovery["ok"], quota["ok"], negatives["ok"], MODEL.is_file(),
                                   not ctc["configured"] or ctc["available"])),
            "storage": plan, "interrupted_capture": recovery, "quota": quota, "negative_score": negatives,
            "pipewire_spa": spa, "ctc": ctc, "turbo_model_file_present": MODEL.is_file(),
            "service": service(), "usb_cursor": usb_cursor(),
            "physical_checks_pending": ["consent", "phone", "output_reference", "clock_anchors", "latency"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--crash-writer", type=Path, required=True)
    crash_writer(parser.parse_args().crash_writer)
