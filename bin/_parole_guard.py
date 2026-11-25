"""Termine les processus de maintenance si leur propriétaire disparaît."""
import ctypes
import os
import signal
import subprocess
import sys
from time import monotonic, sleep


def main():
    owner = int(sys.argv[1])
    child = None

    def terminate(_signal, _frame):
        # We are the process group leader. Pass termination down to the engine
        # and its descendants, even after a crash of the GTK owner.
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        os.killpg(os.getpgrp(), signal.SIGTERM)
        if child:
            deadline = monotonic() + 3
            while monotonic() < deadline:
                try:
                    if os.waitpid(child.pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                sleep(.04)
        os.killpg(os.getpgrp(), signal.SIGKILL)

    signal.signal(signal.SIGTERM, terminate)
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.prctl(1, signal.SIGTERM, 0, 0, 0) != 0 or os.getppid() != owner:
        return 125
    child = subprocess.Popen(sys.argv[2:], stdin=subprocess.DEVNULL)
    return child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
