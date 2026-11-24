"""Libère le micro et le modèle de Hyprwhspr le temps du score de karaoké."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock
import json
import os
from pathlib import Path
from select import POLLHUP, poll
import signal
import subprocess
import sys


UNIT = "hyprwhspr.service"


def event(kind, message):
    try:
        print(json.dumps({"event": kind, "message": message}, ensure_ascii=False), flush=True)
    except BrokenPipeError:
        pass


def systemctl(*args, check=True):
    return subprocess.run(
        ("systemctl", "--user", *args), text=True, capture_output=True, check=check, timeout=110,
    )


def service_state():
    result = systemctl("show", UNIT, "-p", "LoadState", "-p", "ActiveState", "-p", "MainPID", check=False)
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    if fields.get("LoadState") == "not-found":
        return "inactive", "0"
    if result.returncode or not fields.get("ActiveState") or not fields.get("MainPID"):
        raise RuntimeError("Impossible de vérifier l’état de Hyprwhspr.")
    return fields["ActiveState"], fields["MainPID"]


def exclusive_slot(handle):
    try:
        flock(handle, LOCK_EX | LOCK_NB)
    except BlockingIOError:
        return False
    return True


def restore_dictation(marker, notify):
    if marker.exists():
        try:
            systemctl("start", UNIT)
        except (OSError, subprocess.SubprocessError):
            notify("error", "Impossible de redémarrer Hyprwhspr. Relance son service utilisateur.")
        else:
            marker.unlink()


@contextmanager
def without_dictation(notify=event):
    runtime = Path(os.environ["XDG_RUNTIME_DIR"])
    state_file = runtime / f"kara-{UNIT}"
    gate = state_file.with_suffix(".gate")
    marker = state_file.with_suffix(".restore")
    with state_file.open("a") as users:
        with gate.open("a") as transition:
            flock(transition, LOCK_EX)
            first = exclusive_slot(users)
            try:
                state, pid = service_state()
                if state in {"active", "activating", "reloading", "refreshing"}:
                    marker.touch()
                if state not in {"inactive", "failed"} or pid != "0":
                    notify("status", "Arrêt de Hyprwhspr…")
                    systemctl("stop", UNIT)
                    state, pid = service_state()
                    if state not in {"inactive", "failed"} or pid != "0":
                        raise RuntimeError("Hyprwhspr tourne encore. Le lancement est interrompu.")
                flock(users, LOCK_SH)
            except BaseException:
                if first:
                    restore_dictation(marker, notify)
                raise
        try:
            yield
        finally:
            with gate.open("a") as transition:
                flock(transition, LOCK_EX)
                flock(users, LOCK_UN)
                if exclusive_slot(users):
                    restore_dictation(marker, notify)

def wait_for_worker(worker, input_poll):
    try:
        while worker.poll() is None:
            if any(events & POLLHUP for _, events in input_poll.poll(150)):
                try:
                    return worker.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    break
        return worker.returncode if worker.returncode is not None else 1
    finally:
        if worker.poll() is None:
            worker.terminate()
            try:
                worker.wait(timeout=3)
            except subprocess.TimeoutExpired:
                worker.kill()
                worker.wait()


def interrupted(signum, _frame):
    raise SystemExit(128 + signum)


def main(command):
    try:
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        input_poll = poll()
        input_poll.register(sys.stdin, POLLHUP)
        with (runtime / "kara-model.lock").open("a") as lock:
            flock(lock, LOCK_EX)
            if any(events & POLLHUP for _, events in input_poll.poll(0)):
                return 0
            with without_dictation():
                if any(events & POLLHUP for _, events in input_poll.poll(0)):
                    return 0
                # The private datagram route, when supplied by Parole, is
                # inherited only by the score worker, never by system tools.
                passed = (3,) if os.environ.get("KARA_NETWORK_MIC_FD") == "3" else ()
                worker = subprocess.Popen(command, pass_fds=passed)
                return wait_for_worker(worker, input_poll)
    except (OSError, KeyError, RuntimeError, subprocess.SubprocessError) as error:
        if isinstance(error, subprocess.SubprocessError):
            message = "Impossible d’arrêter Hyprwhspr. Le modèle karaoké n’a pas démarré."
        elif isinstance(error, RuntimeError):
            message = str(error)
        else:
            message = "Impossible de démarrer le moteur karaoké."
        event("error", message)
        return 1


if __name__ == "__main__":
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, interrupted)
    raise SystemExit(main(sys.argv[1:]))
