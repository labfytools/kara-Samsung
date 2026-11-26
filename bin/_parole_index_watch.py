"""Bounded Linux directory notifications for the existing Parole inventory."""
import ctypes
import os
from pathlib import Path
import struct
from time import monotonic

from _parole_index import ARTWORK, MEDIA, REMOVE, SIDECAR, SKIP_TOP, affected_tracks, cover_path, index_path

ATTRIB, CLOSE_WRITE = 0x04, 0x08
MOVED_FROM, MOVED_TO, CREATE, DELETE = 0x40, 0x80, 0x100, 0x200
DELETE_SELF, MOVE_SELF, UNMOUNT, OVERFLOW, IGNORED = 0x400, 0x800, 0x2000, 0x4000, 0x8000
ONLYDIR, DONT_FOLLOW, ISDIR = 0x01000000, 0x02000000, 0x40000000
MASK = ATTRIB | CLOSE_WRITE | MOVED_FROM | MOVED_TO | CREATE | DELETE | DELETE_SELF | MOVE_SELF | ONLYDIR | DONT_FOLLOW
EVENT = struct.Struct("=iIII")
MAX_PATHS = 4096
MOVE_GRACE = .5


def kernel_watches():
    try:
        return max(1, int(Path("/proc/sys/fs/inotify/max_user_watches").read_text()) // 8)
    except (OSError, ValueError):
        return 1024


class Watch:
    def __init__(self, index, *, limit=None):
        self.index = index
        self.root = index.root
        self.limit = min(16_384, kernel_watches()) if limit is None else limit
        self.fd = None
        self.paths = {}
        self.pending_moves = {}
        self.next_start = 0.
        self.reason = ""

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
        self.fd = None
        self.paths.clear()
        self.pending_moves.clear()

    def start(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1")
        self.fd = fd
        try:
            self.add_tree(self.root)
        except (OSError, ValueError):
            self.close()
            raise

    def directory(self, path):
        if not path.is_relative_to(self.root):
            return False
        rel = path.relative_to(self.root).parts
        return not rel or rel[0] not in SKIP_TOP and not any(part.startswith(".") for part in rel)

    def add_tree(self, root, changes=None):
        # During startup only the directory tree is visited. A newly arrived
        # subtree is visited once for audio that predates its watch.
        def fail(error):
            raise error

        for folder, children, files in os.walk(root, onerror=fail):
            path = Path(folder)
            children[:] = [name for name in children if self.directory(path / name)]
            if len(self.paths) >= self.limit:
                raise ValueError("limite de surveillance des dossiers")
            wd = self.libc.inotify_add_watch(self.fd, os.fsencode(path), MASK)
            if wd < 0:
                raise OSError(ctypes.get_errno(), "inotify_add_watch")
            self.paths[wd] = path
            if changes is not None:
                for name in files:
                    candidate = path / name
                    if index_path(self.root, candidate):
                        self.add(changes, candidate, MEDIA)

    @staticmethod
    def add(changes, path, mask):
        changes[path] = changes.get(path, 0) | mask
        if len(changes) > MAX_PATHS:
            raise ValueError("trop de changements à regrouper")

    def remove_tree(self, folder, changes):
        for wd, path in list(self.paths.items()):
            if path.is_relative_to(folder):
                self.paths.pop(wd)
                self.libc.inotify_rm_watch(self.fd, wd)
        prefix = str(folder) + "/"
        with self.index.db(wait=.15) as db:
            for path, in db.execute("SELECT path FROM songs WHERE path>=? AND path<? LIMIT ?",
                                    (prefix, prefix[:-1] + "0", MAX_PATHS + 1)):
                self.add(changes, Path(path), REMOVE)

    def receive(self, data, changes, now):
        offset = 0
        while offset < len(data):
            wd, mask, cookie, length = EVENT.unpack_from(data, offset)
            offset += EVENT.size
            name = os.fsdecode(data[offset:offset + length].split(b"\0", 1)[0])
            offset += length
            if mask & (OVERFLOW | UNMOUNT):
                raise ValueError("événements inotify perdus ou stockage démonté")
            directory = self.paths.get(wd)
            if directory is None:
                continue
            if mask & (DELETE_SELF | MOVE_SELF | IGNORED) and directory == self.root:
                raise ValueError("racine de surveillance déplacée")
            if not name or name.startswith("."):
                continue
            path = directory / name
            if mask & ISDIR:
                if not self.directory(path):
                    continue
                if mask & MOVED_FROM and cookie:
                    self.pending_moves[cookie] = path, now
                    if len(self.pending_moves) > MAX_PATHS:
                        raise ValueError("trop de déplacements simultanés")
                elif mask & (CREATE | MOVED_TO):
                    if mask & MOVED_TO and cookie in self.pending_moves:
                        old, _ = self.pending_moves.pop(cookie)
                        for descriptor, prior in list(self.paths.items()):
                            if prior.is_relative_to(old):
                                self.paths[descriptor] = path / prior.relative_to(old)
                        self.remove_tree(old, changes)
                    if path.is_dir() and not path.is_symlink():
                        self.add_tree(path, changes)
                elif mask & DELETE:
                    self.remove_tree(path, changes)
            elif index_path(self.root, path):
                self.add(changes, path, REMOVE if mask & (DELETE | MOVED_FROM) else MEDIA)
            elif cover_path(path) or path.suffix.casefold() in (".txt", ".lrc"):
                art = cover_path(path)
                for track in affected_tracks(self.index, directory):
                    if art or track.stem == path.stem:
                        self.add(changes, track, ARTWORK if art else SIDECAR)

    def poll(self):
        now = monotonic()
        if self.fd is None:
            if now < self.next_start:
                return {}, False
            try:
                self.start()
            except (AttributeError, OSError, ValueError) as error:
                new = str(error) if isinstance(error, ValueError) else "surveillance des dossiers indisponible"
                changed = new != self.reason
                self.reason = new
                self.next_start = now + 60
                return {}, changed
            recovered = bool(self.reason)
            self.reason = ""
            return {}, recovered
        changes = {}
        try:
            try:
                data = os.read(self.fd, 64 * 1024)
            except BlockingIOError:
                data = b""
            self.receive(data, changes, now)
            for cookie, (folder, started) in list(self.pending_moves.items()):
                if now - started >= MOVE_GRACE:
                    self.remove_tree(folder, changes)
                    del self.pending_moves[cookie]
        except (OSError, ValueError) as error:
            self.reason = str(error) if isinstance(error, ValueError) else "surveillance interrompue"
            self.close()
            self.next_start = now + 5
            return {}, True
        return changes, False
