"""Catalogue d’albums YouTube Music et contrôle d’import de kara."""
import json
import re
import subprocess
import sys
from functools import partial
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from requests import Session
from ytmusicapi import YTMusic

from _musique_oz import canonical_album_artist, INBOX, audio_files, component, probe

CHANNEL = re.compile(r"UC[A-Za-z0-9_-]{22}")
ALBUM = re.compile(r"MPRE[A-Za-z0-9_-]+")
PLAYLIST = re.compile(r"OLAK5uy_[A-Za-z0-9_-]+")
VIDEO = re.compile(r"[A-Za-z0-9_-]{11}")


def client():
    session = Session()
    session.request = partial(session.request, timeout=(5, 12))
    return YTMusic(language="fr", location="FR", requests_session=session)


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
        identifier = album.get("browseId") or ""
        if ALBUM.fullmatch(identifier) and album.get("title"):
            result[identifier] = {"title": album["title"], "year": album.get("year") or "",
                                  "url": f"https://music.youtube.com/browse/{identifier}"}
    return {"name": artist.get("name") or "", "albums": list(result.values())}


def source_video(url):
    parsed = urlsplit(url or "")
    if parsed.hostname == "youtu.be":
        return parsed.path.strip("/")
    if parsed.hostname in {"youtube.com", "www.youtube.com", "music.youtube.com"}:
        return parse_qs(parsed.query).get("v", [""])[0]
    return ""


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
    videos = set()
    local = set()
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
                    videos.add(video)
                if same_artist and key(tags.get("album")) == title:
                    local.add((track_number(tags.get("track") or tags.get("tracknumber")), key(tags.get("title"))))
    return [
        {"id": video, "title": track["title"], "present": video in videos or (number, name) in local}
        for track, (video, number, name) in zip(tracks, expected, strict=True)
    ]


def prepare_album(yt, url, root):
    parsed = urlsplit(url)
    if parsed.path.startswith("/browse/"):
        identifier = parsed.path.removeprefix("/browse/").strip("/")
    else:
        playlist = parse_qs(parsed.query).get("list", [""])[0]
        identifier = yt.get_album_browse_id(playlist) if PLAYLIST.fullmatch(playlist) else ""
    if not identifier or not ALBUM.fullmatch(identifier):
        raise ValueError("Impossible d’identifier cet album dans le catalogue YouTube Music.")
    album = yt.get_album(identifier)
    playlist = album.get("audioPlaylistId") or ""
    if not PLAYLIST.fullmatch(playlist):
        raise ValueError("La playlist audio de cet album est indisponible.")
    tracks = collection_tracks(root, album)
    title = album.get("title") or ""
    artists = [artist["name"] for artist in (album.get("artists") or []) if artist.get("name")]
    credit = canonical_album_artist(", ".join(artists), title)
    return {"title": title, "artist": credit, "present": bool(tracks) and all(track["present"] for track in tracks),
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


def main(argv):
    try:
        action, value, *rest = argv
        yt = client()
        if action == "artistes":
            data = artist_search(yt, value)
        elif action == "albums":
            data = releases(yt, value)
        elif action == "preparer" and len(rest) == 1:
            data = prepare_album(yt, value, Path(rest[0]))
        elif action == "pochette" and len(rest) >= 2:
            artist, count, *sources = rest
            data = cover_artwork(yt, value, artist, int(count), sources)
        else:
            raise ValueError("Recherche de catalogue inconnue.")
        print(json.dumps({"data": data}, ensure_ascii=False))
        return 0
    except ValueError as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 1
    except Exception:
        print(json.dumps({"error": "Le catalogue YouTube Music est momentanément indisponible."}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
