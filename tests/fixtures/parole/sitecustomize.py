"""Inject catalogue, cover and lyrics fixtures at the external service boundaries."""
from email.message import Message
from io import BytesIO
import json
import os
from pathlib import Path
import re
import sys
from types import ModuleType
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit
from urllib import request


if "PAROLE_TEST_CONFIG" in os.environ:
    config = json.loads(Path(os.environ["PAROLE_TEST_CONFIG"]).read_text())

    def response(payload, content_type="application/json"):
        result = BytesIO(payload if isinstance(payload, bytes) else json.dumps(payload).encode())
        result.headers = Message()
        result.headers["Content-Type"] = content_type
        return result

    class FixtureOpener:
        def open(self, query, **_kwargs):
            url = urlsplit(query.full_url)
            parameters = parse_qs(url.query)
            if url.hostname == "musicbrainz.org":
                title = re.search(r'releasegroup:"([^"]+)"', parameters["query"][0])[1]
                return response({"release-groups": [{
                    "id": "b8700905-85ce-435c-b633-c472cafefefe", "primary-type": "Album",
                    "title": title, "artist-credit": [{"name": config["artist"]}],
                }]})
            if url.hostname == "coverartarchive.org":
                return response(Path(config["cover"]).read_bytes(), "image/jpeg")
            if url.hostname == "lrclib.net" and url.path == "/api/get":
                album = parameters.get("album_name", [""])[0]
                if album in config["lyric_failures"]:
                    return response("Fixture: le serveur des paroles ne répond pas en JSON.".encode(), "text/plain")
                return response({"trackName": parameters["track_name"][0], "artistName": config["artist"],
                                 "albumName": album, "duration": 4, "plainLyrics": "Son créé pour le test.",
                                 "syncedLyrics": "[00:00.20]Son créé pour le test."})
            raise HTTPError(query.full_url, 404, "Service inexistant pour ce test", None, None)

    if Path(sys.argv[0]).name in {"musique-pochettes", "paroles-musique", "_parole_work.py"}:
        request.build_opener = lambda *_args: FixtureOpener()

    class FixtureYTMusic:
        def __init__(self, **_kwargs):
            pass

        def search(self, _query, **_kwargs):
            return [{"browseId": "UC" + "a" * 22, "artist": config["artist"]}]

        def get_artist(self, _identifier):
            return {"name": config["artist"], "albums": {"results": [
                {"title": album["title"], "year": "2026", "browseId": album["browse"]}
                for album in config["playlists"].values() if "browse" in album
            ]}}

        def get_album_browse_id(self, playlist):
            return config["playlists"].get(playlist, {}).get("browse")

        def get_album(self, identifier):
            playlist, album = next((playlist, album) for playlist, album in config["playlists"].items()
                                   if album.get("browse") == identifier)
            return {"title": album["title"], "artists": [{"name": config["artist"]}],
                    "audioPlaylistId": playlist, "trackCount": len(album["tracks"]),
                    "tracks": [{"title": track["title"], "videoId": track.get("catalogue_id", track["id"]),
                                "trackNumber": index} for index, track in enumerate(album["tracks"], 1)]}

    module = ModuleType("ytmusicapi")
    module.YTMusic = FixtureYTMusic
    sys.modules["ytmusicapi"] = module

    if config.get("archive_item") and Path(sys.argv[0]).name == "kara":
        from contextlib import contextmanager

        sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
        import _parole_archive as archive
        from _kara_import_result import ImportIssue

        def archive_fetch(url, **_kwargs):
            parsed = urlsplit(url)
            if parsed.path == "/advancedsearch.php":
                metadata = config["archive_item"]["metadata"]
                return {"response": {"docs": [{"identifier": metadata["identifier"], "licenseurl": metadata["licenseurl"]}]}}
            return config["archive_item"]

        @contextmanager
        def open_archive_file(_index, _inbox, _album, file):
            with Path(config["requests"]).open("a") as journal:
                journal.write(file.name + "\n")
            if file.name in config.get("archive_failures", []):
                raise ImportIssue("network", "Serveur de stockage d’essai indisponible.")
            with Path(config["archive_originals"][file.name]).open("rb") as stream:
                yield stream

        archive.fetch = archive_fetch
        archive.open_original = open_archive_file

    if config.get("jamendo_release") and Path(sys.argv[0]).name == "kara":
        from contextlib import contextmanager

        sys.path.insert(0, str(Path(sys.argv[0]).resolve().parent))
        import _parole_jamendo as jamendo
        from _kara_import_result import ImportIssue

        def jamendo_fetch(url, **_kwargs):
            path = urlsplit(url).path
            row = config["jamendo_release"]
            result = row["tracks"][0] | {"artist_name": row["artist_name"]} if path == "/v3.0/tracks/" else row
            return {"headers": {"status": "success", "code": 0}, "results": [result]}

        @contextmanager
        def open_jamendo_track(_index, _inbox, track):
            with Path(config["requests"]).open("a") as journal:
                journal.write(track.identifier + "\n")
            if track.identifier in config.get("jamendo_failures", []):
                raise ImportIssue("network", "Transfert d’essai interrompu.")
            with Path(config["jamendo_originals"][track.identifier]).open("rb") as stream:
                yield stream

        jamendo.fetch = jamendo_fetch
        jamendo.open_track = open_jamendo_track
