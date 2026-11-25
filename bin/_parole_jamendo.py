"""Jamendo's authorized download catalogue. The application owns its client ID."""
from contextlib import contextmanager
from dataclasses import dataclass
import os
import re
from urllib.parse import urlencode, urlsplit

from _kara_import_local import Entry
from _kara_import_resources import DOWNLOADS, resource_slot
from _kara_import_result import ImportIssue
from _parole_import_providers import JAMENDO, JAMENDO_ID, license_for
from _parole_sources import ProviderUnavailable, fetch, open_audio

API = "https://api.jamendo.com/v3.0"
DOMAIN = "jamendo.com"
CLIENT = re.compile(r"[A-Za-z0-9]{6,64}")
INTERVAL = 1.2


def client_id():
    value = os.environ.get("KARA_JAMENDO_CLIENT_ID", "").strip()
    return value if CLIENT.fullmatch(value) else ""


def release_url(kind, value):
    return f"https://www.jamendo.com/{kind}/{value}"


def api_results(index, endpoint, **params):
    identity = client_id()
    if not identity:
        raise ImportIssue("dependency", "L’identifiant d’application Jamendo est absent.")
    query = urlencode(dict(params, client_id=identity, format="json"))
    try:
        value = fetch(f"{API}/{endpoint}/?{query}", registry=index, provider=JAMENDO, interval=INTERVAL)
    except (ProviderUnavailable, ValueError) as error:
        # The query contains an application identifier. Keep it out of
        # exceptions, journals and console output even on protocol failure.
        raise ImportIssue("network", "Catalogue Jamendo indisponible.", stage="Catalogue Jamendo") from error
    headers = value.get("headers") if isinstance(value, dict) else None
    if not isinstance(headers, dict):
        raise ImportIssue("processing", "Réponse Jamendo invalide.")
    if headers.get("status") != "success" or headers.get("code") != 0:
        code = headers.get("code")
        message = f"Accès au catalogue Jamendo refusé (code {code})." if type(code) is int else "Accès Jamendo refusé."
        raise ImportIssue("path", message, stage="Catalogue Jamendo")
    results = value.get("results")
    if not isinstance(results, list) or len(results) > 30:
        raise ImportIssue("processing", "Résultats du catalogue Jamendo invalides.")
    return results


@dataclass(frozen=True)
class Track:
    identifier: str
    position: int
    title: str
    duration: int
    license: str


@dataclass(frozen=True)
class Release:
    kind: str
    identifier: str
    title: str
    artist: str
    revision: str
    tracks: tuple[Track, ...]


def safe_text(value):
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:180]


def track_metadata(row, position):
    if not isinstance(row, dict):
        return None
    value = row.get("id")
    duration = str(row.get("duration", ""))
    title = safe_text(row.get("name"))
    license_url = license_for(row.get("license_ccurl"))
    # The streaming URL never constitutes download permission. Check both the
    # exact JSON boolean and the presence of a matching provider download URL.
    supplied = row.get("audiodownload")
    try:
        link = urlsplit(supplied) if isinstance(supplied, str) else None
    except ValueError:
        return None
    if (row.get("audiodownload_allowed") is not True or not isinstance(value, str) or not JAMENDO_ID.fullmatch(value)
            or not duration.isascii() or not duration.isdecimal() or len(duration) > 5 or int(duration) <= 0
            or not title or not license_url or link is None or link.scheme != "https"
            or not (link.netloc == DOMAIN or link.netloc.endswith("." + DOMAIN))):
        return None
    return Track(value, position, title, int(duration), license_url)


def parse_release(kind, row):
    if not isinstance(row, dict):
        return None
    edition = row.get("id")
    title, artist = safe_text(row.get("name")), safe_text(row.get("artist_name"))
    if not isinstance(edition, str) or not JAMENDO_ID.fullmatch(edition) or not title or not artist:
        return None
    if kind == "track":
        track = track_metadata(row, 1)
        if track is None:
            return None
        tracks = [track]
    else:
        tracks = []
        members = row.get("tracks")
        # Album and track rights are independent. An incomplete or partially
        # forbidden catalogue response cannot be offered as an album.
        if row.get("zip_allowed") is not True or not isinstance(members, list) or not 0 < len(members) <= 256:
            return None
        for part in members:
            position = str(part.get("position", "")) if isinstance(part, dict) else ""
            if not re.fullmatch(r"[1-9][0-9]{0,2}", position):
                return None
            track = track_metadata(part, int(position))
            if track is None:
                return None
            tracks.append(track)
        if ({track.position for track in tracks} != set(range(1, len(tracks) + 1))
                or len({track.identifier for track in tracks}) != len(tracks)):
            return None
    return Release(kind, edition, title, artist, safe_text(row.get("releasedate")),
                   tuple(sorted(tracks, key=lambda t: t.position)))


def item(index, kind, identifier):
    endpoint = "albums/tracks" if kind == "album" else "tracks"
    rows = api_results(index, endpoint, id=identifier, limit=2, audiodlformat="flac")
    release = parse_release(kind, rows[0]) if len(rows) == 1 else None
    if release is None or release.identifier != identifier:
        raise ImportIssue("path", "Téléchargement intégral et licence compatibles absents de la fiche Jamendo.")
    return release


def search(index, query):
    query = " ".join(query.split())
    if not query or len(query.encode()) > 115:
        return []
    results = {}
    for field in ("artist_name", "namesearch"):
        for row in api_results(index, "albums/tracks", limit=8, audiodlformat="flac", **{field: query}):
            album = parse_release("album", row)
            if album is None:
                continue
            identity = album.identifier
            results[identity] = {
                "title": album.title, "artist": album.artist, "url": release_url("album", identity),
                "tracks": len(album.tracks), "formats": ["FLAC"], "provider": JAMENDO, "price": 0,
                "license": album.tracks[0].license if len({track.license for track in album.tracks}) == 1 else "",
            }
    return list(results.values())


@contextmanager
def open_track(index, inbox, track):
    identity = client_id()
    if not identity:
        raise ImportIssue("dependency", "L’identifiant d’application Jamendo est absent.")
    query = urlencode(dict(client_id=identity, id=track.identifier, audioformat="flac", action="download"))
    url = f"{API}/tracks/file/?{query}"
    with resource_slot(inbox / ".kara-transferts", DOWNLOADS), open_audio(
            url, registry=index, provider=JAMENDO, service="api.jamendo.com", hosts=(DOMAIN,), interval=INTERVAL) as stream:
        yield stream


@contextmanager
def entries(index, inbox, release):
    # Track identifiers are independent of a ZIP or a particular server.
    # Downloads use the documented endpoint after both rights checks above.
    yield {f"{track.position:03d}-{track.identifier}.flac": Entry(
        track.title, None, [track.identifier, track.duration, "flac", release.revision],
        lambda track=track: open_track(index, inbox, track),
        external_id=track.identifier, format="flac", license=track.license)
        for track in release.tracks}
