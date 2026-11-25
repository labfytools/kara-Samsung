"""Catalogue d’albums YouTube Music et contrôle d’import de kara."""
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from _kara_import_resources import defer_service, wait_service
from _kara_import_result import ImportIssue
from _musique_oz import canonical_album_artist, INBOX, audio_files, component, probe, source_video

CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
ALBUM = re.compile(r"MPRE[A-Za-z0-9_-]+")
PLAYLIST = re.compile(r"OLAK5uy_[A-Za-z0-9_-]+")
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")
YOUTUBE_COVER_HOSTS = {"lh3.googleusercontent.com", "yt3.googleusercontent.com"}


def client():
    from requests import Session
    from ytmusicapi import YTMusic

    registry = None
    if os.environ.get("KARA_PAROLE_COVER") == "1":
        from _kara_paths import music_library
        from _parole_index import Index
        from _parole_sources import wait_provider

        registry = Index(music_library())

    class CatalogueSession(Session):
        def request(self, method, url, **options):
            from email.utils import parsedate_to_datetime
            from time import time
            from requests import exceptions

            # Desktop suggestions, mobile lookup, covers and imports share
            # pacing with yt-dlp. A Retry-After applies to the next callers too.
            if registry is not None:
                wait_provider(registry, "YouTube Music", "youtube", .8, max_wait=12)
            else:
                wait_service("youtube", 0.5)
            options.setdefault("timeout", (5, 12))
            try:
                response = super().request(method, url, **options)
            except (exceptions.Timeout, exceptions.ConnectionError):
                defer_service("youtube", 12)
                raise
            if response.status_code in (429, 500, 502, 503, 504):
                delay = 60 if response.status_code == 429 else 12
                header = response.headers.get("Retry-After", "")
                try:
                    requested = int(header) if header.isdecimal() else parsedate_to_datetime(header).timestamp() - time()
                    delay = max(delay, requested)
                except (TypeError, ValueError, OverflowError):
                    pass
                defer_service("youtube", delay)
                response.raise_for_status()
            return response

    return YTMusic(language="fr", location="FR", requests_session=CatalogueSession())


def key(value):
    return component(value, "").casefold()


def artist_search(yt, query):
    artists = {}
    for item in yt.search(query, filter="artists", limit=12):
        identifier = item.get("browseId") or ""
        name = (item.get("artist") or "").strip()
        if name and CHANNEL.fullmatch(identifier):
            artists[identifier] = {"id": identifier, "name": name, "followers": item.get("subscribers") or ""}
    return list(artists.values())


def releases(yt, identifier):
    if not CHANNEL.fullmatch(identifier):
        raise ValueError("Identifiant d’artiste invalide.")
    artist = yt.get_artist(identifier)
    shelf = artist.get("albums") or {}
    if shelf.get("browseId") and shelf.get("params"):
        albums = yt.get_artist_albums(shelf["browseId"], shelf["params"], limit=None)
    else:
        albums = shelf.get("results") or []
    result = {}
    for album in albums:
        album_id = album.get("browseId") or ""
        if ALBUM.fullmatch(album_id) and album.get("title"):
            result[album_id] = {"id": album_id, "title": album["title"], "year": album.get("year") or "",
                                "url": f"https://music.youtube.com/browse/{album_id}"}
    return {"name": artist.get("name") or "", "artist_id": identifier, "albums": list(result.values())}


def names(row):
    return [entry["name"] for entry in (row.get("artists") or []) if entry.get("name")]


def is_youtube_cover_url(url):
    if not isinstance(url, str):
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    # The catalogue and its authenticated image proxy share the same exact
    # origins. Matching the full authority excludes credentials and ports.
    return parsed.scheme == "https" and parsed.netloc in YOUTUBE_COVER_HOSTS


def album_edition(yt, identifier, edition="édition du catalogue"):
    """Use one verified catalogue edition for preview and eventual import."""
    if not ALBUM.fullmatch(identifier):
        raise ValueError("Identifiant d’album invalide.")
    album = yt.get_album(identifier)
    tracks = album.get("tracks") or []
    total = album.get("trackCount")
    if not album.get("title") or not tracks or len(tracks) > 150 or (type(total) is int and total != len(tracks)):
        return None
    artists = names(album)
    details = [{"title": row.get("title") or "", "disc": row.get("discNumber"),
                "track": row.get("trackNumber") or index, "artists": names(row) or artists}
               for index, row in enumerate(tracks, 1)]
    cover = next((pic["url"] for pic in (album.get("thumbnails") or [])
                  if is_youtube_cover_url(pic.get("url"))), None)
    return {"id": identifier, "title": album["title"], "artists": artists, "edition": album.get("type") or edition,
            "year": album.get("year"), "tracks": details, "cover": cover}


def track_albums(yt, title, artist):
    """Only return editions whose album track list actually contains this title."""
    identifiers = {}
    for row in yt.search(f"{title} {artist}", filter="songs", limit=12):
        identifier = (row.get("album") or {}).get("id") or ""
        if key(row.get("title")) == key(title) and key(artist) in {key(name) for name in names(row)}:
            if ALBUM.fullmatch(identifier):
                identifiers[identifier] = "album de la piste"
    for row in yt.search(f"{title} {artist}", filter="albums", limit=8):
        identifier = row.get("browseId") or ""
        if ALBUM.fullmatch(identifier):
            identifiers.setdefault(identifier, row.get("type") or "édition du catalogue")
    results = []
    for identifier, edition in list(identifiers.items())[:6]:
        album = album_edition(yt, identifier, edition)
        if album and any(key(row["title"]) == key(title) and key(artist) in {key(name) for name in row["artists"]}
                         for row in album["tracks"]):
            results.append({**album, "requested_title": title, "requested_artist": artist})
    return results


def track_number(value):
    first = (value or "").partition("/")[0].strip()
    return int(first) if first.isdecimal() else None


def collection_tracks(root, album):
    title = key(album.get("title"))
    names = [artist["name"] for artist in album.get("artists") or [] if artist.get("name")]
    artist_keys = {key(canonical_album_artist(", ".join(names), album.get("title")))}
    tracks = album.get("tracks") or []
    count = album.get("trackCount")
    if (not title or not names or not root.is_dir() or not tracks
            or (isinstance(count, int) and count != len(tracks))
            or any(not key(track.get("title")) for track in tracks)):
        return []

    expected = [(t.get("videoId") or "", t.get("trackNumber") or index, key(t["title"]))
                for index, t in enumerate(tracks, 1)]
    videos = {}
    local = {}
    for artist in root.iterdir():
        if artist.name in {INBOX, "YouTube"} or artist.name.startswith(".") or artist.is_symlink() or not artist.is_dir():
            continue
        for directory in artist.iterdir():
            if directory.is_symlink() or not directory.is_dir() or key(directory.name) != title:
                continue
            same_artist = key(canonical_album_artist(artist.name, album.get("title"))) in artist_keys
            for audio in audio_files(directory):
                try:
                    tags, _ = probe(audio)
                    video = source_video(tags.get("purl"))
                except (OSError, ValueError, subprocess.SubprocessError):
                    continue
                if VIDEO.fullmatch(video):
                    videos.setdefault(video, str(audio))
                if same_artist and key(tags.get("album")) == title:
                    number = track_number(tags.get("track") or tags.get("tracknumber"))
                    local.setdefault((number, key(tags.get("title"))), str(audio))
    return [
        {"id": video, "title": track["title"], "path": videos.get(video) or local.get((number, name))}
        for track, (video, number, name) in zip(tracks, expected, strict=True)
    ]


def prepare_album(yt, url, root):
    parsed = urlsplit(url)
    requested_playlist = parse_qs(parsed.query).get("list", [""])[0]
    if parsed.path.startswith("/browse/"):
        identifier = parsed.path.removeprefix("/browse/").strip("/")
    else:
        identifier = yt.get_album_browse_id(requested_playlist) if PLAYLIST.fullmatch(requested_playlist) else ""
    if not identifier or not ALBUM.fullmatch(identifier):
        # A supplied playlist remains resolvable by yt-dlp without a browse ID.
        if not parsed.path.startswith("/browse/") and PLAYLIST.fullmatch(requested_playlist):
            return None
        raise PlaylistUnavailable("Impossible d’identifier cet album dans le catalogue YouTube Music.")
    album = yt.get_album(identifier)
    playlist = album.get("audioPlaylistId") or ""
    if not parsed.path.startswith("/browse/") and playlist != requested_playlist:
        # Use the explicitly supplied edition when the catalogue points elsewhere.
        return None
    if not PLAYLIST.fullmatch(playlist):
        raise PlaylistUnavailable("La playlist audio de cet album est indisponible.")
    tracks = collection_tracks(root, album)
    title = album.get("title") or ""
    artists = [artist["name"] for artist in (album.get("artists") or []) if artist.get("name")]
    credit = canonical_album_artist(", ".join(artists), title)
    return {"id": identifier, "title": title, "artist": credit, "present": bool(tracks) and all(track["path"] for track in tracks),
            "tracks": tracks, "url": f"https://music.youtube.com/playlist?list={playlist}"}



def cover_artwork(yt, title, artist, count, sources):
    title_key = key(title.replace("’", "'"))
    artist_key = key(canonical_album_artist(artist, title))
    videos = {value for url in sources if VIDEO.fullmatch(value := source_video(url))}
    seen = set()
    matches = []
    queries = [f"{artist} {title}", title] if videos else [f"{artist} {title}"]
    for query in queries:
        for result in yt.search(query, filter="albums", limit=8, ignore_spelling=True):
            identifier = result.get("browseId") or ""
            if identifier in seen or not ALBUM.fullmatch(identifier):
                continue
            if key((result.get("title") or "").replace("’", "'")) != title_key:
                continue
            seen.add(identifier)
            if len(seen) > 5:
                break
            album = yt.get_album(identifier)
            if key((album.get("title") or "").replace("’", "'")) != title_key:
                continue
            tracks = album.get("tracks") or []
            if len(tracks) < count:
                continue
            names = [entry["name"] for entry in album.get("artists") or [] if entry.get("name")]
            same_artist = bool(names) and key(canonical_album_artist(", ".join(names), title)) == artist_key
            overlap = len(videos.intersection(track.get("videoId") for track in tracks))
            if not overlap and not (same_artist and len(tracks) == count):
                continue
            thumbnails = [thumb for thumb in (album.get("thumbnails") or [])
                          if thumb.get("url") and isinstance(thumb.get("width"), int)
                          and thumb["width"] == thumb.get("height") and thumb["width"] >= 150]
            if thumbnails:
                url = max(thumbnails, key=lambda thumb: thumb["width"])["url"]
                matches.append(((overlap, same_artist, len(tracks) == count), url))
        if any(rank[0] for rank, _ in matches) or len(seen) >= 5:
            break
    if not matches:
        return None
    best = max(rank for rank, _ in matches)
    choices = {url for rank, url in matches if rank == best}
    return choices.pop() if len(choices) == 1 else None


class PlaylistUnavailable(ValueError):
    pass


def main(argv):
    from urllib.error import URLError

    try:
        action, value, *rest = argv
        yt = client()
        if action == "artistes":
            data = artist_search(yt, value)
        elif action == "albums":
            data = releases(yt, value)
        elif action == "edition" and not rest:
            data = album_edition(yt, value)
        elif action == "morceau" and len(rest) == 1:
            data = track_albums(yt, value, rest[0])
        elif action == "preparer" and len(rest) == 1:
            data = prepare_album(yt, value, Path(rest[0]))
        elif action == "pochette" and len(rest) >= 2:
            artist, count, *sources = rest
            data = cover_artwork(yt, value, artist, int(count), sources)
        else:
            raise ValueError("Recherche de catalogue inconnue.")
        print(json.dumps({"data": data}, ensure_ascii=False))
        return 0
    except Exception as error:
        if isinstance(error, ModuleNotFoundError):
            category = "dependency"
        elif isinstance(error, ImportIssue):
            category = error.category
        elif isinstance(error, PlaylistUnavailable):
            category = "playlist"
        elif isinstance(error, (ConnectionError, TimeoutError, URLError)) or type(error).__module__.startswith("requests."):
            category = "network"
        else:
            category = "processing"
        print(json.dumps({"category": category, "error": f"{type(error).__name__}: {error}"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
