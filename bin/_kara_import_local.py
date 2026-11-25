"""Read explicitly selected albums without moving originals or extracting paths."""
from contextlib import contextmanager
from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
from pathlib import Path, PurePosixPath
from stat import S_IFDIR, S_IFREG, S_IFMT, S_ISREG
from struct import unpack_from
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from zipfile import BadZipFile, ZipFile
import os

from _kara_import_resources import MAX_MEDIA, PROCESSING, check_space
from _kara_import_result import ImportIssue
from _musique_oz import AUDIO, share_file
from _parole_import_providers import MANUAL
from _paroles_lrc import MAX_LRC_BYTES, parse_lrc

LOCAL = "Fichiers locaux"
MAX_ENTRIES = 4096
MAX_DIRECTORY = 8 * 1024**2
MAX_AUDIO = 2 * 1024**3
MAX_DEPTH = 12
BLOCK = 1024**2


def local_url(value):
    return value.startswith("file:")


def local_selection(value):
    parts = urlsplit(value)
    path = Path(unquote(parts.path, errors="strict"))
    if parts.scheme != "file" or parts.netloc or parts.fragment or not path.is_absolute():
        raise ImportIssue("path", "Un fichier audio, dossier ou ZIP local est attendu.")
    if "\0" in str(path) or ".." in path.parts or path == path.parent:
        raise ImportIssue("path", "Le chemin de l’album local est invalide.")
    if len(parts.query) > 1200:
        raise ImportIssue("path", "Provenance de téléchargement trop longue.")
    try:
        query = parse_qs(parts.query, keep_blank_values=True, strict_parsing=True, max_num_fields=3)
    except ValueError as error:
        raise ImportIssue("path", "Provenance de téléchargement invalide.") from error
    if query.keys() - {"provider", "edition", "license"} or any(len(values) != 1 for values in query.values()):
        raise ImportIssue("path", "Provenance de téléchargement invalide.")
    provider = query.get("provider", [LOCAL])[0]
    reference = query.get("edition", [""])[0]
    if provider in MANUAL:
        return path, provider, *MANUAL[provider].provenance(reference, query.get("license", [""])[0])
    if provider != LOCAL or query:
        raise ImportIssue("path", "Fournisseur de téléchargement inconnu.")
    return path, LOCAL, "", ""


def local_target(uri, provider, reference, license_url=""):
    if provider not in MANUAL:
        return uri
    canonical, license_url = MANUAL[provider].provenance(reference, license_url)
    return uri + "?" + urlencode({"provider": provider, "edition": canonical, "license": license_url})


def stamp(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


@contextmanager
def open_member(root, name):
    # All components below the selected directory are opened relative to the
    # pinned root, so replacing a child with a symlink cannot redirect a read.
    current = os.dup(root)
    try:
        parts = PurePosixPath(name).parts
        for part in parts[:-1]:
            child = os.open(part, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=current)
        with os.fdopen(fd, "rb") as source:
            if not S_ISREG(os.fstat(source.fileno()).st_mode):
                raise OSError("Fichier non ordinaire")
            yield source
    finally:
        os.close(current)


def check_zip_directory(source):
    # Check the central-directory size before ZipFile allocates its full index.
    size = source.seek(0, os.SEEK_END)
    offset = max(0, size - 65_557)
    source.seek(offset)
    tail = source.read(65_557)
    end = tail.rfind(b"PK\x05\x06")
    if end < 0 or end + 22 > len(tail):
        raise BadZipFile("Répertoire ZIP absent")
    disk, central_disk, disk_count, count, length, _, comment = unpack_from("<4H2IH", tail, end + 4)
    if disk or central_disk or count != disk_count or end + 22 + comment != len(tail):
        raise BadZipFile("Répertoire ZIP invalide ou réparti sur plusieurs fichiers")
    locator = offset + end - 20
    if locator >= 0:
        source.seek(locator)
        data = source.read(20)
        if data.startswith(b"PK\x06\x07"):
            start_disk, start, disks = unpack_from("<IQI", data, 4)
            if start_disk or disks != 1 or start != locator - 56:
                raise BadZipFile("Répertoire ZIP64 invalide")
            source.seek(start)
            data = source.read(56)
            if len(data) != 56 or not data.startswith(b"PK\x06\x06"):
                raise BadZipFile("Répertoire ZIP64 absent")
            disk, central_disk, disk_count, count, length = unpack_from("<IIQQQ", data, 16)
            if disk or central_disk or count != disk_count:
                raise BadZipFile("ZIP64 réparti sur plusieurs fichiers")
    if count > MAX_ENTRIES or length > MAX_DIRECTORY:
        raise BadZipFile("Répertoire ZIP trop volumineux")


@dataclass(frozen=True)
class Entry:
    name: str
    size: int
    revision: list
    open: object


def directory_entries(fd):
    entries = {}
    visited = 0

    def descend(parent, base):
        nonlocal visited
        with os.scandir(parent) as children:
            for child in children:
                visited += 1
                if visited > MAX_ENTRIES:
                    raise ImportIssue("path", "Sélectionne le dossier d’un album ou d’un coffret.")
                if child.name.startswith("."):
                    continue
                key = base / child.name
                if child.is_dir(follow_symlinks=False) and len(key.parts) < MAX_DEPTH:
                    subdir = os.open(child.name, os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
                    try:
                        descend(subdir, key)
                    finally:
                        os.close(subdir)
                elif wanted(child.name):
                    metadata = child.stat(follow_symlinks=False)
                    if S_ISREG(metadata.st_mode):
                        name = str(key)
                        entries[name] = Entry(name, metadata.st_size, stamp(metadata),
                                              lambda name=name: open_member(fd, name))

    descend(fd, PurePosixPath())
    return entries


@contextmanager
def zip_member(source, archive, info, revision):
    if stamp(os.fstat(source.fileno())) != revision:
        raise BadZipFile("L’archive a été modifiée avant lecture")
    with archive.open(info) as member:
        yield member
    if stamp(os.fstat(source.fileno())) != revision:
        raise BadZipFile("L’archive a été modifiée pendant sa lecture")


@contextmanager
def local_entries(path):
    if path.is_symlink():
        raise ImportIssue("path", "La sélection locale doit désigner directement le dossier ou le ZIP.")
    if path.is_dir():
        fd = os.open(path, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            yield directory_entries(fd)
        finally:
            os.close(fd)
        return
    if path.suffix.lower() in AUDIO and path.is_file():
        fd = os.open(path.parent, os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            entries = {}
            # Selecting a single file cannot implicitly select the rest of its folder.
            for name in (path.name, path.with_suffix(".lrc").name):
                try:
                    metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                if S_ISREG(metadata.st_mode):
                    entries[name] = Entry(name, metadata.st_size, stamp(metadata),
                                          lambda name=name: open_member(fd, name))
            yield entries
        finally:
            os.close(fd)
        return
    if path.suffix.lower() != ".zip" or not path.is_file():
        raise ImportIssue("path", "Fichier audio, dossier ou archive ZIP introuvable.")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not S_ISREG(metadata.st_mode):
            raise ImportIssue("path", "La sélection ZIP n’est pas un fichier ordinaire.")
        check_zip_directory(source)
        original = stamp(metadata)
        with ZipFile(source) as archive:
            entries = {}
            seen = set()
            if len(archive.filelist) > MAX_ENTRIES:
                raise BadZipFile("Trop d’entrées dans le ZIP")
            for info in archive.infolist():
                name = info.orig_filename
                parts = name.removesuffix("/").split("/")
                kind = S_IFMT(info.external_attr >> 16)
                if (len(parts) > MAX_DEPTH or any(part in {"", ".", ".."} for part in parts)
                        or "\\" in name or "\0" in name or ":" in name
                        or kind not in {0, S_IFDIR, S_IFREG} or info.flag_bits & 1):
                    raise BadZipFile(f"Entrée ZIP incompatible : {name!r}")
                key = "/".join(parts)
                if key in seen:
                    raise BadZipFile(f"Entrée ZIP en double : {name!r}")
                seen.add(key)
                if not info.is_dir() and not any(p.startswith(".") for p in parts) and wanted(key):
                    revision = [*original, info.CRC, info.file_size]
                    entries[key] = Entry(key, info.file_size, revision,
                                         lambda info=info: zip_member(source, archive, info, original))
            yield entries


def wanted(name):
    path = PurePosixPath(name)
    return path.suffix.lower() in (AUDIO | {".lrc"}) or path.name.casefold() == "cover.jpg"


def read_entry(entry, destination, stop):
    if not 0 < entry.size <= MAX_AUDIO:
        raise ImportIssue("path", "Taille audio locale hors limites.")
    check_space(destination.parent, extra=entry.size + MAX_MEDIA * PROCESSING)
    digest = sha256()
    with entry.open() as source, NamedTemporaryFile(dir=destination.parent, prefix=".local-") as draft:
        revision = entry.revision
        if len(revision) == 5 and stamp(os.fstat(source.fileno())) != revision:
            raise OSError("Le fichier source a changé avant lecture.")
        remaining = entry.size
        while remaining:
            if stop.is_set():
                return None
            data = source.read(min(remaining, BLOCK))
            if not data:
                raise OSError("Fichier source incomplet.")
            remaining -= len(data)
            digest.update(data)
            draft.write(data)
        if source.read(1) or (len(revision) == 5 and stamp(os.fstat(source.fileno())) != revision):
            raise OSError("Le fichier source a changé pendant la lecture.")
        draft.flush()
        os.fsync(draft.fileno())
        os.link(draft.name, destination)
    return digest.hexdigest()


def publish_sidecar(entry, target):
    if target.exists() or target.is_symlink():
        return
    lyrics = target.suffix == ".lrc"
    limit = MAX_LRC_BYTES if lyrics else 8 * BLOCK
    if not 0 < entry.size <= limit:
        raise ValueError("Taille de la pochette ou du LRC local hors limites.")
    with entry.open() as source:
        content = source.read(limit + 1)
    if len(content) != entry.size:
        raise ValueError("Fichier associé local incomplet.")
    if lyrics:
        if not any(line for _, line in parse_lrc(content.decode("utf-8-sig"))):
            raise ValueError("Le LRC local ne contient aucun repère avec paroles.")
    else:
        from PIL import Image

        with Image.open(BytesIO(content)) as image:
            if image.format != "JPEG" or image.width * image.height > 30_000_000:
                raise ValueError("Format ou dimensions de la pochette locale invalides.")
            image.verify()
    with NamedTemporaryFile(dir=target.parent, prefix=".import-local-") as draft:
        draft.write(content)
        draft.flush()
        os.fsync(draft.fileno())
        share_file(draft.name, target)
        try:
            os.link(draft.name, target)
        except FileExistsError:
            return
        fd = os.open(target.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
