"""Résultats d’import partagés par la commande kara et son interface GTK."""
import json
from pathlib import Path
import re
from tempfile import NamedTemporaryFile
from threading import Lock

from _kara_paths import STATE

IMPORT_LOGS = STATE / "kara/imports"
CATEGORIES = {
    "dependency": "Dépendance manquante",
    "path": "Chemin invalide",
    "playlist": "Playlist indisponible",
    "network": "Erreur réseau",
    "audio_unavailable": "Format audio indisponible sur YouTube",
    "download": "Échec de téléchargement",
    "processing": "Échec de traitement",
    "present": "Album déjà présent",
    "partial": "Importation partielle",
}


class ImportIssue(ValueError):
    def __init__(self, category, detail):
        super().__init__(detail)
        self.category = category


def failure_category(detail, default):
    text = detail.casefold()
    if any(term in text for term in ("modulenotfounderror", "not installed", "ffmpeg not found",
                                     "no supported javascript runtime", "python dependencies")):
        return "dependency"
    if any(term in text for term in ("could not find firefox cookies", "no such file or directory",
                                     "permission denied", "no space left on device", "invalid path")):
        return "path"
    if any(term in text for term in ("playlist does not exist", "playlist is not available",
                                     "playlist is private", "failed to resolve album to playlist",
                                     "channel/playlist does not exist")):
        return "playlist"
    if re.search(r"\bhttp(?: error)? (?:429|5\d\d)\b", text) or any(term in text for term in (
        "timed out", "timeout", "name resolution", "connection reset", "connection refused",
        "network is unreachable", "certificate verify failed", "failed to establish a new connection",
    )):
        return "network"
    if default == "download" and "requested format is not available" in text:
        return "audio_unavailable"
    return default


class ImportLog:
    def __enter__(self):
        IMPORT_LOGS.mkdir(parents=True, mode=0o700, exist_ok=True)
        self.file = NamedTemporaryFile(mode="w", encoding="utf-8", dir=IMPORT_LOGS,
                                       prefix="import-", suffix=".jsonl", delete=False)
        self.path = Path(self.file.name)
        self.lock = Lock()
        return self

    def __exit__(self, *_args):
        self.file.close()

    def write(self, payload):
        with self.lock:
            self.file.write(json.dumps(payload, ensure_ascii=False) + "\n")
            self.file.flush()


def retry_albums(log_path, library):
    # The journal is scoped to the configured collection. Consumers must still
    # validate each track against that collection before any retry can mutate it.
    path = Path(log_path)
    if (not path.is_absolute() or path.is_symlink() or path.parent.resolve() != IMPORT_LOGS.resolve()
            or not path.name.startswith("import-") or path.suffix != ".jsonl"):
        raise ImportIssue("path", f"Journal d’import invalide : {path}")
    outcomes = {}
    with path.open(encoding="utf-8") as journal:
        header = json.loads(next(journal, "null"))
        if (not isinstance(header, dict) or header.get("library") != str(library) or header.get("version") != 1
                or not isinstance(header.get("urls"), list) or not all(isinstance(url, str) for url in header["urls"])):
            raise ImportIssue("path", f"Ce journal ne correspond pas à cette bibliothèque : {path}")
        for line in journal:
            try:
                record = json.loads(line)
            except ValueError:
                # A killed process can leave an incomplete final record.
                continue
            if (isinstance(record, dict) and record.get("event") == "album"
                    and isinstance(record.get("url"), str) and isinstance(record.get("status"), str)):
                outcomes[record["url"]] = record
    return set(header["urls"]), outcomes
