"""Cross-process admission and cooldowns for the import engine."""
from contextlib import ExitStack, contextmanager
from collections import Counter, deque
from concurrent.futures import wait as wait_futures
from contextvars import ContextVar, copy_context
from fcntl import LOCK_EX, LOCK_NB, LOCK_SH, flock
import json
import os
from pathlib import Path
import re
from shutil import disk_usage
import signal
import socket
import sqlite3
import subprocess
from tempfile import NamedTemporaryFile
from threading import Event, Lock, Thread
import time

from _kara_import_result import (YOUTUBE_AVAILABILITY, YOUTUBE_HOLD_MESSAGE, YOUTUBE_RATE,
                                 ImportIssue, provider_rate_delay)
from _kara_paths import STATE

DEFAULT_DOWNLOADS = 10
MAX_DOWNLOADS = 20
MEMORY_PRESSURE_SLOTS = 3
YOUTUBE_DEFAULT_SLOTS = 3
YOUTUBE_INTERVAL = .5


def youtube_audio_slots(value):
    # This is the local access profile. Installations with documented
    # automated access can supply their allowed concurrency independently
    # of the global ceiling. Invalid values keep the conservative profile.
    if 0 < len(value) <= 2 and value.isascii() and value.isdecimal():
        slots = int(value)
        if 1 <= slots <= MAX_DOWNLOADS:
            return slots
    return YOUTUBE_DEFAULT_SLOTS


AUDIO_PROVIDERS = {"youtube": (youtube_audio_slots(os.environ.get("KARA_YOUTUBE_AUDIO_SLOTS", "")), 60),
                   "archive.org": (MAX_DOWNLOADS, 5),
                   "api.jamendo.com": (MAX_DOWNLOADS, 5)}
AUDIO_INTERVALS = {"youtube": YOUTUBE_INTERVAL, "archive.org": 1.2, "api.jamendo.com": 1.2}
CATALOGUE = 2
PROCESSING = 2
DISK_RESERVE = 2 * 1024**3
STATE_RESERVE = 64 * 1024**2
MAX_MEDIA = 256 * 1024**2
WORKING_RESERVE = MAX_MEDIA * 2
RATE_DIRECTORY = STATE / "kara" / "import-rates"
YOUTUBE_AMBIGUOUS = re.compile(
    r"^ERROR: \[youtube\] ([A-Za-z0-9_-]{11}): Video unavailable\.?\s*"
    r"(?:This video is not available\.?)?$", re.MULTILINE | re.IGNORECASE)
YOUTUBE_FAILURE_LIMIT = 6
YOUTUBE_FAILURE_WINDOW = 120
YOUTUBE_AMBIGUOUS_PAUSE = 1800
_pause = ContextVar("import_pause", default=None)
_downloads = ContextVar("import_downloads", default=None)
_download_observer = ContextVar("import_download_observer", default=None)


@contextmanager
def download_scope(settings):
    token = _downloads.set(settings)
    try:
        yield
    finally:
        _downloads.reset(token)


@contextmanager
def watch_download(observer):
    token = _download_observer.set(observer)
    try:
        yield
    finally:
        _download_observer.reset(token)


def download_event(event, **fields):
    if observer := _download_observer.get():
        observer(event, **fields)


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


@contextmanager
def track_results(executor, handler, values, *, window=MAX_DOWNLOADS):
    # One executor feeds all albums in the lot. Keep only a window of
    # submitted work per album, and drain it before releasing its lease or
    # closing its input manifest, even when a pause or error interrupts it.
    if executor is None:
        yield map(handler, values)
        return
    pending = deque()
    values = iter(values)

    def admit():
        checkpoint()
        for value in values:
            pending.append(executor.submit(copy_context().run, handler, value))
            break

    def results():
        while pending:
            result = pending[0].result()
            pending.popleft()
            admit()
            yield result

    try:
        for _ in range(window):
            admit()
        yield results()
    finally:
        wait_futures([future for future in pending if not future.cancel()])


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
def lease(path, *, wait=True, shared=False, poll_interval=.1):
    path.parent.mkdir(mode=0o770, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o660)
    try:
        try:
            operation = LOCK_SH if shared else LOCK_EX
            if wait:
                lock_file(fd, operation, poll_interval=poll_interval)
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


def lock_file(fd, operation=LOCK_EX, *, poll_interval=.1):
    if current_pause() is None:
        flock(fd, operation)
        return
    while True:
        checkpoint()
        try:
            flock(fd, operation | LOCK_NB)
            return
        except BlockingIOError:
            wait(poll_interval)


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


def memory_download_limit():
    try:
        available = next(int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
                         if line.startswith("MemAvailable:")) // 1024
        pressure = float(Path("/proc/pressure/memory").read_text().splitlines()[0].split()[1].split("=")[1])
    except (OSError, ValueError, StopIteration, IndexError):
        return MEMORY_PRESSURE_SLOTS, "Mesure de la mémoire indisponible"
    # Leave memory for Strawberry, the desktop and video. The allowance
    # covers track workers and buffers. YouTube separately bounds its live
    # processes, including the ones waiting to acquire a conversion slot.
    limit = max(1, min(MAX_DOWNLOADS, (available - 1280) // 192))
    if pressure >= 1:
        limit = min(limit, MEMORY_PRESSURE_SLOTS)
    return limit, "Pression mémoire" if limit < MAX_DOWNLOADS else ""


def storage_download_limit(path):
    return max(0, min(MAX_DOWNLOADS, (disk_usage(path).free - DISK_RESERVE) // WORKING_RESERVE))


def service_delay(service):
    path = RATE_DIRECTORY / service
    if not path.exists():
        return 0.
    with lease(path) as fd:
        return max(0., rate_record(fd) - time.time())


def read_youtube_gate(path):
    try:
        with path.open("rb") as stream:
            return json.loads(stream.read(2048))
    except FileNotFoundError:
        return {"until": 0, "reason": "", "recent": {}}


def check_youtube():
    path = RATE_DIRECTORY / "youtube-gate.json"
    if path.exists():
        with lease(RATE_DIRECTORY / "youtube-gate.lock", shared=True):
            gate = read_youtube_gate(path)
        if gate["until"] > time.time():
            raise ImportIssue("provider_hold", f"{YOUTUBE_HOLD_MESSAGE} · {gate['reason']}.")


def observe_youtube(*, detail="", extracted=False, retry_after=0):
    delay = max(retry_after, provider_rate_delay(detail))
    ambiguous = YOUTUBE_AMBIGUOUS.search(detail) if not delay and not extracted else None
    if not (delay or ambiguous or extracted):
        return
    path = RATE_DIRECTORY / "youtube-gate.json"
    if extracted and not path.exists():
        return
    # Resolution and transfer processes share this state. An explicit
    # provider delay can extend a hold. Success only clears the ambiguous
    # predecessors, so a process already in flight cannot reopen admission.
    with lease(RATE_DIRECTORY / "youtube-gate.lock"):
        gate = read_youtube_gate(path)
        now = time.time()
        if delay:
            if gate["reason"] != YOUTUBE_RATE or now + delay > gate["until"]:
                gate.update(until=max(gate["until"], now + delay), reason=YOUTUBE_RATE, recent={})
            else:
                return
        elif gate["until"] > now:
            return gate["reason"]
        elif extracted:
            if not gate["recent"]:
                return
            gate["recent"] = {}
        else:
            recent = {video: stamp for video, stamp in gate["recent"].items() if stamp > now - YOUTUBE_FAILURE_WINDOW}
            recent[ambiguous[1]] = now
            if len(recent) >= YOUTUBE_FAILURE_LIMIT:
                gate.update(until=now + YOUTUBE_AMBIGUOUS_PAUSE, reason=YOUTUBE_AVAILABILITY, recent={})
            else:
                gate["recent"] = recent
        # Replace the bounded record atomically without replacing the inode
        # locked by other workers. A killed writer leaves the last valid gate.
        temporary = path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="ascii") as stream:
            json.dump(gate, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        if gate["until"] > now:
            return gate["reason"]


def pending_downloads(directory):
    # Tickets exist only while a living worker waits. A killed owner leaves
    # an unlocked file which the next observer can safely discard.
    waiting = directory / "waiting"
    if not waiting.is_dir():
        return
    for path in sorted(waiting.iterdir()):
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            continue
        try:
            try:
                flock(fd, LOCK_EX | LOCK_NB)
            except BlockingIOError:
                # tempfile's random part may itself contain underscores.
                yield path, path.name.rsplit("_", 1)[-1]
            else:
                path.unlink(missing_ok=True)
        finally:
            os.close(fd)


@contextmanager
def audio_slot(directory, service, interval=0., notice=None, *, provider_fd=None):
    # Acquisition across competing processes is serialized. The leases
    # themselves outlive admission and are inherited by an external worker.
    # A capped or cooling provider cannot hold a global transfer slot.
    # A resolver may already own the provider lease. Its ticket must then
    # compete for the global slot without trying to acquire a second lease.
    profile, max_wait = AUDIO_PROVIDERS[service]
    provider_slots = directory / service
    waiting = directory / "waiting"
    waiting.mkdir(parents=True, mode=0o770, exist_ok=True)
    mutex = directory / "admission.lock"
    settings = _downloads.get() or (lambda: (DEFAULT_DOWNLOADS, True))
    _, manual = settings()
    prefix = f"{0 if manual else 1}_{time.monotonic_ns():020d}-"
    suffix = "_" + service + ("+reserved" if provider_fd is not None else "")
    with ExitStack() as transferred:
        with lease(mutex, poll_interval=.01):
            ticket = transferred.enter_context(NamedTemporaryFile(
                dir=waiting, prefix=prefix, suffix=suffix, delete=False))
            flock(ticket.fileno(), LOCK_EX)
        path = Path(ticket.name)
        try:
            while True:
                checkpoint()
                if service == "youtube":
                    check_youtube()
                with lease(mutex, poll_interval=.01), ExitStack() as claim:
                    effective, manual = settings()
                    if manual and path.name.startswith("1"):
                        promoted = path.with_name("0" + path.name[1:])
                        path.rename(promoted)
                        path = promoted
                    delay = service_delay(service)
                    if delay > max_wait:
                        raise ImportIssue("network", f"Temporisation du fournisseur {service}.")
                    # Reserve worst-case space for currently active transfers
                    # and their drafts. Count high-numbered slots when the
                    # effective cap has just been reduced.
                    active = slot_count(directory, MAX_DOWNLOADS)
                    global_fd = None
                    if active < effective:
                        memory, _ = memory_download_limit()
                        free = storage_download_limit(directory)
                        if not free:
                            check_space(directory, extra=WORKING_RESERVE)
                    if active < effective and active < min(memory, free):
                        available = {}
                        for waiter, kind in pending_downloads(directory):
                            host, _, reserved = kind.partition("+")
                            if host not in AUDIO_PROVIDERS:
                                continue
                            if kind not in available:
                                cap, _ = AUDIO_PROVIDERS[host]
                                available[kind] = (service_delay(host) <= 0 and
                                                   (reserved == "reserved" or slot_count(directory / host, cap) < cap))
                            if not available[kind]:
                                continue
                            if waiter == path and reserve_service(service, interval):
                                if provider_fd is None:
                                    provider_fd = claim.enter_context(resource_slot(provider_slots, profile))
                                global_fd = claim.enter_context(resource_slot(directory, MAX_DOWNLOADS))
                                transferred.enter_context(claim.pop_all())
                            break
                    if global_fd is not None:
                        break
                if notice:
                    notice()
                    notice = None
                # The mutex protects only local admission. A short wait
                # prevents tickets from waking in slow lock-step batches.
                wait(.025)
        finally:
            path.unlink(missing_ok=True)
        download_event("audio-started")
        yield global_fd, provider_fd


def download_admission(directory):
    """Inspect provider tickets, occupied leases and gates without waiting for admission."""
    if not directory.is_dir():
        return {}
    with lease(directory / "admission.lock", wait=False) as fd:
        waiting = Counter(kind.partition("+")[0] for _, kind in pending_downloads(directory)) if fd is not None else {}
    return {service: (waiting.get(service, 0), slot_count(directory / service, cap), service_delay(service))
            for service, (cap, _) in AUDIO_PROVIDERS.items()}


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


def reserve_service(service, interval):
    with lease(RATE_DIRECTORY / service) as fd:
        now = time.time()
        if rate_record(fd) > now:
            return False
        rate_record(fd, now + interval)
        return True


def wait_service(service, interval, *, max_wait=60):
    path = RATE_DIRECTORY / service
    deadline = time.monotonic() + max_wait
    while True:
        if service == "youtube":
            check_youtube()
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
        # A later failure can request a shorter retry. It cannot cancel the
        # longer delay already in force for other workers of this source.
        rate_record(fd, max(rate_record(fd), time.time() + delay))
