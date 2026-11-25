"""Exécution bornée des moteurs non interactifs de maintenance."""
import json
import os
import selectors
import signal
import subprocess
import sys
from pathlib import Path
from time import monotonic


class Interrupted(Exception):
    pass


def run(command, *, stop, timeout, env=None, maximum=1_048_576, event=None):
    guard = Path(__file__).with_name("_parole_guard.py")
    with subprocess.Popen([sys.executable, "-B", str(guard), str(os.getpid()), *command], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, start_new_session=True, env=env) as process:
        deadline = monotonic() + timeout
        output = bytearray()
        pending = bytearray()
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if stop():
                        raise Interrupted
                    if monotonic() >= deadline:
                        raise TimeoutError("Délai du moteur dépassé")
                    for _, _ in selector.select(.15):
                        chunk = os.read(process.stdout.fileno(), 8192)
                        if not chunk:
                            selector.unregister(process.stdout)
                            continue
                        output.extend(chunk)
                        if len(output) > maximum:
                            raise ValueError("Sortie du moteur trop volumineuse")
                        if event is not None:
                            pending.extend(chunk)
                            while (end := pending.find(b"\n")) >= 0:
                                line = bytes(pending[:end])
                                del pending[:end + 1]
                                try:
                                    value = json.loads(line)
                                except (ValueError, UnicodeError, RecursionError):
                                    continue
                                if isinstance(value, dict):
                                    event(value)
            while process.poll() is None:
                if stop():
                    raise Interrupted
                if monotonic() >= deadline:
                    raise TimeoutError("Délai du moteur dépassé")
                try:
                    process.wait(timeout=.15)
                except subprocess.TimeoutExpired:
                    pass
            return process.returncode, bytes(output)
        except BaseException:
            # The worker and descendants inherit the same group. SIGTERM
            # permits atomic workers to drop their drafts, then bound cleanup.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=4)
            except subprocess.TimeoutExpired:
                pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            raise
