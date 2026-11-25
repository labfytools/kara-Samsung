"""Cross-process admission and cooldowns for the import engine."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import os
from pathlib import Path
from shutil import disk_usage
import time

from _kara_import_result import ImportIssue
from _kara_paths import STATE

DOWNLOADS = 3
CATALOGUE = 2
PROCESSING = 2
DISK_RESERVE = 2 * 1024**3
STATE_RESERVE = 64 * 1024**2
MAX_MEDIA = 256 * 1024**2
WORKING_RESERVE = MAX_MEDIA * DOWNLOADS * 2
RATE_DIRECTORY = STATE / "kara" / "import-rates"


@contextmanager
def lease(path, *, wait=True):
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        try:
            flock(fd, LOCK_EX if wait else LOCK_EX | LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            # Close without LOCK_UN. An admitted child can inherit this same
            # open file description and keep the lease after its parent dies.
            yield fd
    finally:
        os.close(fd)


@contextmanager
def resource_slot(directory, capacity, notice=None, *, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        for index in range(capacity):
            with lease(directory / str(index), wait=False) as fd:
                if fd is not None:
                    yield fd
                    return
        if notice is not None:
            notice()
            notice = None
        if deadline is not None and time.monotonic() >= deadline:
            raise ImportIssue("network", "Aucun créneau de requête disponible.")
        time.sleep(0.17)


def slot_count(directory, capacity):
    active = 0
    if directory.is_dir():
        for index in range(capacity):
            path = directory / str(index)
            if path.is_file():
                with lease(path, wait=False) as fd:
                    active += fd is None
    return active


def check_space(path, *, state=False, extra=0):
    reserve = STATE_RESERVE if state else DISK_RESERVE
    if disk_usage(path).free < reserve + extra:
        raise ImportIssue("space", f"Espace disque insuffisant dans {path} (réserve de {reserve // 1024**2} Mio).")


def rate_record(fd, when=None):
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        previous = float(os.read(fd, 64) or b"0")
    except ValueError:
        previous = 0.
    if when is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, str(max(when, previous)).encode())
        os.ftruncate(fd, os.lseek(fd, 0, os.SEEK_CUR))
    return previous


def wait_service(service, interval, *, max_wait=60):
    path = RATE_DIRECTORY / service
    deadline = time.monotonic() + max_wait
    while True:
        with lease(path) as fd:
            now = time.time()
            delay = rate_record(fd) - now
            if delay <= 0:
                rate_record(fd, now + interval)
                return
        if time.monotonic() + delay > deadline:
            raise ImportIssue("network", f"{service} demande d’attendre avant de reprendre.")
        time.sleep(delay)


def defer_service(service, delay):
    with lease(RATE_DIRECTORY / service) as fd:
        rate_record(fd, time.time() + delay)
