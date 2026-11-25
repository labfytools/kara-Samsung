"""Cross-process admission and cooldowns for the import engine."""
from contextlib import contextmanager
from contextvars import ContextVar
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, flock
import os
from pathlib import Path
from shutil import disk_usage
import signal
import socket
import sqlite3
import subprocess
from threading import Event, Lock, Thread
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
_pause = ContextVar("import_pause", default=None)


class ImportSuspended(Exception):
    """Control flow only: a paused operation consumes no failure or retry."""


class ImportPause:
    def __init__(self, requested):
        self.requested = requested
        self.suspended = Event()
        self.done = Event()
        self.sockets = set()
        self.lock = Lock()

    def __enter__(self):
        self.thread = Thread(target=self.watch, name="import-pause")
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.done.set()
        self.thread.join()

    def watch(self):
        while not self.done.is_set():
            try:
                requested = self.requested()
            except (sqlite3.Error, OSError):
                # A transient journal lock must not kill the watcher and
                # make a later durable pause invisible to this worker.
                requested = False
            if requested:
                self.suspended.set()
                with self.lock:
                    for stream in self.sockets:
                        try:
                            stream.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                return
            self.done.wait(.1)

    def is_set(self):
        return self.suspended.is_set()

    @contextmanager
    def scope(self):
        token = _pause.set(self)
        try:
            yield
        finally:
            _pause.reset(token)

    def watch_socket(self, stream):
        with self.lock:
            checkpoint()
            self.sockets.add(stream)

    def forget_socket(self, stream):
        with self.lock:
            self.sockets.discard(stream)


def current_pause():
    return _pause.get()


def checkpoint():
    if (pause := current_pause()) is not None and pause.is_set():
        raise ImportSuspended


def wait(seconds):
    if (pause := current_pause()) is None:
        time.sleep(seconds)
    elif pause.suspended.wait(seconds):
        raise ImportSuspended


def terminate_group(process):
    # The group includes helpers such as yt-dlp's FFmpeg. Reap the leader
    # and signal any descendants still alive before acknowledging the pause.
    for request in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(process.pid, request)
        except ProcessLookupError:
            break
        if request == signal.SIGTERM:
            try:
                process.wait(timeout=.5)
            except subprocess.TimeoutExpired:
                pass
    process.wait()


def run_checked(args, *, timeout, check=False, capture_output=False, input=None, pass_fds=()):
    # Keep ordinary library/USB callers on the existing subprocess path.
    if current_pause() is None:
        return subprocess.run(args, timeout=timeout, check=check, capture_output=capture_output,
                              input=input, pass_fds=pass_fds)
    checkpoint()
    deadline = time.monotonic() + timeout
    pipe = subprocess.PIPE if capture_output else None
    with subprocess.Popen(args, stdin=subprocess.PIPE if input is not None else None, stdout=pipe, stderr=pipe,
                          pass_fds=pass_fds, start_new_session=True) as process:
        try:
            while True:
                checkpoint()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout)
                try:
                    stdout, stderr = process.communicate(input, timeout=min(.15, remaining))
                    break
                except subprocess.TimeoutExpired:
                    input = None
        except (ImportSuspended, subprocess.TimeoutExpired):
            terminate_group(process)
            raise
        result = subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
        checkpoint()
        if check:
            result.check_returncode()
        return result


@contextmanager
def lease(path, *, wait=True, shared=False):
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        try:
            operation = LOCK_SH if shared else LOCK_EX
            if wait:
                lock_file(fd, operation)
            else:
                flock(fd, operation | LOCK_NB)
        except BlockingIOError:
            yield None
        else:
            # Close without LOCK_UN. An admitted child can inherit this same
            # open file description and keep the lease after its parent dies.
            yield fd
    finally:
        os.close(fd)


def lock_file(fd, operation=LOCK_EX):
    if current_pause() is None:
        flock(fd, operation)
        return
    while True:
        checkpoint()
        try:
            flock(fd, operation | LOCK_NB)
            return
        except BlockingIOError:
            wait(.1)


@contextmanager
def resource_slot(directory, capacity, notice=None, *, timeout=None):
    deadline = time.monotonic() + timeout if timeout is not None else None
    while True:
        checkpoint()
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
        wait(0.17)


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
        wait(delay)


def defer_service(service, delay):
    with lease(RATE_DIRECTORY / service) as fd:
        rate_record(fd, time.time() + delay)
