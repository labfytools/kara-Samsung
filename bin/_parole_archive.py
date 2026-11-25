"""Authorized audio files from the public Internet Archive item API."""
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
import re
from urllib.parse import quote, urlencode

from _kara_import_local import Entry, MAX_AUDIO, MAX_ENTRIES
from _kara_import_resources import AUDIO_INTERVALS, audio_slot
from _kara_import_result import ImportIssue
from _musique_oz import AUDIO
from _parole_import_providers import ARCHIVE, ARCHIVE_ID as IDENTIFIER, license_for
from _parole_music_identity import position
from _parole_sources import ProviderUnavailable, download_interval, fetch, open_audio

HOST = "archive.org"
INTERVAL = AUDIO_INTERVALS[HOST]
SITE = "https://archive.org"
DOCS = SITE + "/developers/md-read.html"
SHA1 = re.compile(r"[0-9a-f]{40}")


def release_url(value):
    return f"{SITE}/details/{value}"


def restricted(fields):
    return any(fields.get(key) not in (None, False, 0, "0", "", "false")
               for key in ("access-restricted-item", "private", "is_dark", "nodownload", "no-download"))


@dataclass(frozen=True)
class Original:
    name: str
    size: int
    sha1: str
    facts: dict


@dataclass(frozen=True)
class Album:
    identifier: str
    title: str
    artist: str
    license: str
    originals: tuple[Original, ...]
    facts: dict

    @property
    def size(self):
        return sum(file.size for file in self.originals)

    @property
    def formats(self):
        return sorted({PurePosixPath(file.name).suffix[1:].upper() for file in self.originals})


def parse_item(item_id, value):
    if not isinstance(value, dict):
        raise ImportIssue("path", "Édition Internet Archive introuvable.")
    metadata = value.get("metadata")
    if (not isinstance(metadata, dict) or metadata.get("identifier") != item_id
            or metadata.get("mediatype") != "audio" or restricted(metadata) or restricted(value)):
        raise ImportIssue("path", "Fiche audio publique Internet Archive indisponible.")
    license_url = license_for(metadata.get("licenseurl"))
    if not license_url:
        raise ImportIssue("path", "Licence compatible absente de la fiche Internet Archive.")
    files = value.get("files")
    if not isinstance(files, list) or len(files) > MAX_ENTRIES:
        raise ImportIssue("path", "Inventaire Internet Archive absent ou hors limites.")
    audio = {}
    for file in files:
        if not isinstance(file, dict) or file.get("source") != "original":
            continue
        name = file.get("name")
        if not isinstance(name, str) or PurePosixPath(name).suffix.lower() not in AUDIO:
            continue
        # A claimed original must be accessible and have an integrity value.
        # Receiving just the public subset must not mark a release complete.
        size = str(file.get("size", ""))
        checksum = file.get("sha1")
        if (len(name.encode()) > 350 or any(ch in name for ch in ("\\", "?", "#", ":"))
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in name)
                or any(part in {"", ".", ".."} or part.startswith(".") for part in name.split("/"))
                or name in audio or not re.fullmatch(r"[0-9]{1,10}", size) or not 0 < int(size) <= MAX_AUDIO
                or not isinstance(checksum, str) or not SHA1.fullmatch(checksum) or restricted(file)):
            raise ImportIssue("path", "Fichier original Internet Archive non vérifiable ou non accessible.")
        audio[name] = Original(name, int(size), checksum, {
            "title": file.get("title"), "disc": file.get("disc") or file.get("discnumber") or 1,
            "number": file.get("track"), "duration": file.get("length"), "artist": file.get("artist"),
            "recording_id": file.get("musicbrainz_recordingid"), "isrc": file.get("isrc"),
        })
    if not audio:
        raise ImportIssue("path", "Aucun fichier audio original disponible pour cette fiche.")
    title, artist = metadata.get("title"), metadata.get("creator")
    if not isinstance(title, str) or not title.strip() or not isinstance(artist, str) or not artist.strip():
        raise ImportIssue("path", "Titre ou artiste absent de la fiche Internet Archive.")
    originals = tuple(audio[name] for name in sorted(audio))
    title, artist = title.strip()[:180], artist.strip()[:180]
    facts = {"title": title, "artist": artist, "year": metadata.get("date"),
             "count": len(originals), "release_id": metadata.get("musicbrainz_releaseid"),
             "group_id": metadata.get("musicbrainz_releasegroupid"), "artist_id": metadata.get("musicbrainz_artistid"),
             "barcode": metadata.get("barcode") or metadata.get("upc"),
             "edition": metadata.get("edition"), "tracks": [file.facts for file in originals]}
    return Album(item_id, title, artist, license_url, originals, facts)


def item(index, item_id):
    if not IDENTIFIER.fullmatch(item_id):
        raise ImportIssue("path", "Identifiant Internet Archive invalide.")
    try:
        value = fetch(f"{SITE}/metadata/{item_id}", registry=index, provider=ARCHIVE, interval=INTERVAL)
    except ProviderUnavailable as error:
        raise ImportIssue("network", str(error), stage="Catalogue Internet Archive") from error
    return parse_item(item_id, value)


def search(index, query):
    query = " ".join(query.split())
    if not query or len(query.encode()) > 115:
        return []
    expression = query.replace("\\", "\\\\").replace('"', '\\"')
    fields = urlencode([("q", f'mediatype:audio AND (creator:"{expression}" OR title:"{expression}")'),
                        ("rows", "16"), ("output", "json"), ("fl[]", "identifier"), ("fl[]", "licenseurl")])
    try:
        data = fetch(f"{SITE}/advancedsearch.php?{fields}", registry=index, provider=ARCHIVE, interval=INTERVAL)
    except ProviderUnavailable as error:
        raise ImportIssue("network", str(error), stage="Catalogue Internet Archive") from error
    if not isinstance(data, dict) or not isinstance(data.get("response"), dict):
        raise ImportIssue("processing", "Réponse de recherche Internet Archive invalide.")
    candidates = data["response"].get("docs", [])
    if not isinstance(candidates, list):
        raise ImportIssue("processing", "Réponse de recherche Internet Archive invalide.")
    results = []
    checked = set()
    unavailable = None
    for candidate in candidates[:16]:
        if not isinstance(candidate, dict) or not license_for(candidate.get("licenseurl")):
            continue
        value = candidate.get("identifier")
        if not isinstance(value, str) or not IDENTIFIER.fullmatch(value) or value in checked:
            continue
        checked.add(value)
        try:
            album = item(index, value)
        except ImportIssue as error:
            if error.category == "network":
                unavailable = error
            elif error.category != "path":
                raise
        else:
            results.append({"title": album.title, "artist": album.artist, "url": release_url(value),
                            "tracks": len(album.originals), "formats": album.formats, "bytes": album.size,
                            "license": album.license, "provider": ARCHIVE, "price": 0, "facts": album.facts})
        if len(checked) >= 4:
            break
    if not results and unavailable:
        raise unavailable
    return results


@contextmanager
def open_original(index, inbox, album, file, offset=0):
    url = f"{SITE}/download/{album.identifier}/{quote(file.name, safe='/')}"
    interval = download_interval(index, ARCHIVE, INTERVAL)
    with audio_slot(inbox / ".kara-transferts", HOST, interval), open_audio(
            url, registry=index, provider=ARCHIVE, service=HOST, hosts=(HOST,), expected_size=file.size,
            interval=INTERVAL, paced=True, **({"offset": offset} if offset else {})) as stream:
        yield stream


@contextmanager
def entries(index, inbox, album):
    # A SHA-1 from the item manifest keys a staged download and verifies
    # transport integrity. Local proof and cache reuse still use PCM and SHA-256.
    result = {}
    for file in album.originals:
        slot = tuple(position(file.facts.get(key)) for key in ("disc", "number"))
        result[file.name] = Entry(file.name, file.size, [file.sha1, file.size],
                                 lambda file=file: open_original(index, inbox, album, file),
                                 sha1=file.sha1, external_id=f"{album.identifier}/{file.name}",
                                 position=slot if all(slot) else None,
                                 open_range=lambda offset, file=file: open_original(index, inbox, album, file, offset))
    yield result
