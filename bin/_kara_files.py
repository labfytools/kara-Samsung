"""File identities and durable atomic writes shared with Parole."""
import os
from pathlib import Path
import stat
from tempfile import NamedTemporaryFile


def file_token(path):
    info = path.stat()
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Un fichier ordinaire est requis.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def lrc_token(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("Le fichier LRC doit être un fichier ordinaire.")
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]


def atomic(path, data):
    # Rename is the publication point. Flush both data and directory so a
    # process or machine interruption cannot publish a partial generation.
    draft = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            draft = Path(stream.name)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(draft, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)
