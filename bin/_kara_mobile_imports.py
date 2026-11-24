"""Requests and catalogue editions for the shared Parole import queue."""
from collections import OrderedDict
import json
from pathlib import Path
from subprocess import DEVNULL, run, SubprocessError
from tempfile import TemporaryFile
from threading import BoundedSemaphore, Lock
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4

from _kara_catalogue import ALBUM, CHANNEL, is_youtube_cover_url
from _kara_import_result import CATEGORIES
from _kara_mobile_room import RoomError
from _kara_party import search_key
from _kara_paths import catalogue_command


FINISHED = ("done", "failed", "partial", "cancelled")


def imported(outcome):
    return outcome.get("status") in ("success", "present") and outcome.get("audio_complete") is True


def title_key(value):
    return " ".join(search_key(value).split())


class NoCoverRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args):
        return None


class MobileImports:
    def __init__(self, control):
        self.control = control
        self.lock = Lock()
        self.slots = BoundedSemaphore(2)
        self.editions = OrderedDict()
        self.discographies = OrderedDict()
        self.jobs = OrderedDict()

    def lookup(self, title, artist):
        if not all(isinstance(value, str) and 1 <= len(value.strip()) <= 120 for value in (title, artist)):
            raise RoomError("Renseigne le titre et l’artiste.")
        albums = self.catalogue("morceau", title.strip(), artist.strip(), expected=list)
        self.control.blackbox.emit("imports", "external-search", count=len(albums))
        return [self.publish_edition(album) for album in albums]

    def artists(self, query):
        if not isinstance(query, str) or not 2 <= len(query.strip()) <= 150:
            raise RoomError("Saisis au moins deux caractères du nom de l’artiste.")
        return self.catalogue("artistes", query.strip(), expected=list)

    def albums(self, identifier):
        if not isinstance(identifier, str) or not CHANNEL.fullmatch(identifier):
            raise RoomError("Choisis un artiste dans les résultats.")
        releases = self.catalogue("albums", identifier, expected=dict)
        if not releases["albums"]:
            return releases
        handle = uuid4().hex
        # Approval uses this exact set of releases. It never repeats an
        # external search that might return a different set of editions.
        with self.lock:
            self.discographies[handle] = releases
            if len(self.discographies) > 16:
                self.discographies.popitem(last=False)
        return {**releases, "handle": handle}

    def edition(self, identifier):
        if not isinstance(identifier, str) or len(identifier) > 128 or not ALBUM.fullmatch(identifier):
            raise RoomError("Choisis un album dans les résultats.")
        album = self.catalogue("edition", identifier, expected=(dict, type(None)))
        if album is None:
            raise RoomError("La liste complète des pistes de cette édition est indisponible.", 404)
        self.control.blackbox.emit("imports", "external-edition")
        return self.publish_edition(album)

    def catalogue(self, action, *values, expected):
        # Mobile suggestions share the existing per-room admission limit with
        # album details and covers. Only bounded catalogue data leaves the subprocess.
        if not self.slots.acquire(blocking=False):
            raise RoomError("Deux recherches sont déjà en cours.", 429)
        try:
            with TemporaryFile() as stdout:
                result = run(catalogue_command(action, *values),
                             timeout=40, stdout=stdout, stderr=DEVNULL, check=False)
                stdout.seek(0)
                raw = stdout.read(512 * 1024 + 1)
            if result.returncode or len(raw) > 512 * 1024:
                raise ValueError("Catalogue indisponible")
            payload = json.loads(raw)
            if not isinstance(payload, dict) or "data" not in payload or not isinstance(payload["data"], expected):
                raise ValueError("Catalogue indisponible")
            return payload["data"]
        except (OSError, ValueError, SubprocessError):
            self.control.blackbox.emit("imports", "error", stage="catalogue")
            raise RoomError("YouTube Music ne répond pas à la recherche.", 502) from None
        finally:
            self.slots.release()

    def publish_edition(self, album):
        with self.lock:
            # Only a server-issued handle can reach the import queue. The
            # preview and later approval refer to the same immutable edition.
            handle = uuid4().hex
            self.editions[handle] = album
            if len(self.editions) > 128:
                self.editions.popitem(last=False)
        return {**album, "cover": bool(album.get("cover")), "source": "YouTube Music", "handle": handle}

    def cover(self, handle):
        with self.lock:
            url = self.editions.get(handle, {}).get("cover")
        if not is_youtube_cover_url(url):
            raise RoomError("Pochette indisponible.", 404)
        if not self.slots.acquire(blocking=False):
            raise RoomError("Catalogue occupé.", 429)
        try:
            opener = build_opener(NoCoverRedirect())
            with opener.open(url, timeout=3) as response:
                payload = response.read(384 * 1024 + 1)
                mime = response.headers.get_content_type()
            headers = {"image/jpeg": b"\xff\xd8\xff", "image/png": b"\x89PNG\r\n\x1a\n", "image/webp": b"RIFF"}
            if (mime in headers and len(payload) <= 384 * 1024 and payload.startswith(headers[mime])
                    and (mime != "image/webp" or payload[8:12] == b"WEBP")):
                return payload, mime
        except (URLError, ValueError, TimeoutError):
            pass
        finally:
            self.slots.release()
        raise RoomError("Pochette indisponible.", 404)

    def request(self, token, name, data):
        with self.lock:
            handle = data.get("handle")
            if not isinstance(handle, str) or data.get("rights") is not True:
                raise RoomError("Choisis un import et confirme l’autorisation.")
            if data.get("all") is True:
                discography = self.discographies.get(handle)
                if discography is None:
                    raise RoomError("Rouvre la liste des albums de l’artiste.")
                title = discography["name"] or "Discographie"
                albums = discography["albums"]
                track_count = None
                requested = (name, None, False)
            else:
                album = self.editions.get(handle)
                if album is None or type(data.get("add")) is not bool:
                    raise RoomError("Choisis une édition et confirme l’autorisation d’importer.")
                if data["add"]:
                    index = data.get("track")
                    if type(index) is int and 0 <= index < len(album["tracks"]):
                        track = album["tracks"][index]
                        album = {**album, "requested_title": track["title"],
                                 "requested_artist": ", ".join(track["artists"])}
                    elif "track" in data or "requested_title" not in album:
                        raise RoomError("Choisis la piste à ajouter dans cette édition.")
                title = album["title"]
                albums = (album,)
                track_count = len(album["tracks"])
                requested = (name, album, data["add"])
            targets = tuple((album["title"], f"https://music.youtube.com/browse/{album['id']}") for album in albums)
            urls = frozenset(url for _, url in targets)
            for existing in self.jobs.values():
                if existing["state"] in FINISHED or not urls & existing["urls"]:
                    continue
                if urls == existing["urls"]:
                    existing["requests"][token] = requested
                    return {"job": existing["id"], "state": existing["state"]}
                raise RoomError("Une autre demande en cours contient déjà certains de ces albums.", 409)
            if len(self.jobs) >= 32:
                finished = next((job for job in self.jobs.values() if job["state"] in FINISHED), None)
                if finished:
                    self.jobs.pop(finished["id"])
                else:
                    raise RoomError("La file des demandes est pleine.", 409)
            identity = uuid4().hex
            self.jobs[identity] = {"id": identity, "title": title, "targets": targets, "urls": urls,
                                   "tracks": track_count, "state": "approval", "progress": 0.,
                                   "message": "En attente de l’organisateur",
                                   "requests": {token: requested}, "notes": {}, "results": {}}
        self.control.blackbox.emit("imports", "requested", player=name, job=identity, album=title, albums=len(targets))
        return {"job": identity, "state": "approval"}

    def pending(self):
        with self.lock:
            return any(job["state"] in ("approval", "queued", "running", "stopping", "integrating")
                       for job in self.jobs.values())

    def snapshot(self, *, organizer=False, token=None):
        with self.lock:
            return [{"id": job["id"], "title": job["title"], "tracks": job["tracks"], "albums": len(job["targets"]),
                     "state": job["state"], "progress": job["progress"], "message": job["message"],
                     "note": job["notes"].get(token, ""),
                     **({"requesters": [entry[0] for entry in job["requests"].values()]} if organizer else {})}
                    for job in reversed(self.jobs.values()) if organizer or token in job["requests"]]

    def action(self, identity, command):
        if not isinstance(identity, str):
            raise RoomError("Demande inconnue.", 404)
        with self.lock:
            job = self.jobs.get(identity)
            if job is None:
                raise RoomError("Demande inconnue.", 404)
            state = job["state"]
            if command == "cancel" and state == "approval":
                job.update(state="cancelled", message="Demande annulée")
                return
            if command == "cancel" and state in ("queued", "running"):
                task = job.get("task")
                if task is None or task.done or task.process is not None and not task.stop_ready:
                    raise RoomError("Attends le prochain point d’arrêt de l’import.", 409)
                job.update(state="stopping", message="Arrêt demandé")
            else:
                task = None
            if task is not None:
                if len(task.jobs) > 1 and len(job["targets"]) == 1:
                    job.update(state=state)
                    raise RoomError("L’album appartient à un lot lancé dans Parole.", 409)
            elif ((command, state) not in (("approve", "approval"), ("retry", "failed"), ("retry", "partial"))
                  or self.control.remote_party.locked):
                raise RoomError("La demande ne peut pas être lancée maintenant.", 409)
            if task is None:
                targets = job["targets"]
                journal = job.get("journal") if command == "retry" else None
                if journal:
                    targets = tuple((title, url) for title, url in targets
                                    if not imported(job["results"].get(url, {}))
                                    and job["results"].get(url, {}).get("status") != "pending")
                    if not targets:
                        raise RoomError("Aucun album en échec à relancer.", 409)
                job.update(state="queued", message="Dans la file Parole", progress=0.)
        if task is not None:
            if task.process is None:
                task.finish_import("cancelled", "Demande annulée", None, "")
            else:
                task.stop_batch(None)
            return
        from _kara_import import import_key
        keys = {import_key(url) for _, url in targets}
        occupied = next((item for item in self.control.album_import.imports
                         if not item.done and keys.intersection(item.keys)), None)
        if occupied is not None and len(job["targets"]) > 1:
            with self.lock:
                job.update(state=state, message="Des albums du lot sont déjà en cours dans Parole")
            raise RoomError(job["message"], 409)
        task = occupied or self.control.album_import.enqueue(targets, journal=journal)
        with self.lock:
            if task is None:
                job.update(state=state, message="La file Parole est pleine")
                raise RoomError(job["message"], 409)
            job["task"] = task
            if task.process is not None:
                job.update(state="running", message="Import en cours")
        if task.done:
            self.update(task, "finished", {"event": "failure"})

    def update(self, task, kind, payload):
        # The original import journal remains the full local diagnostic. Public
        # snapshots only expose sanitized stage/summary fields, never subprocess text.
        with self.lock:
            jobs = [job for job in self.jobs.values() if job.get("task") is task]
            for job in jobs:
                if kind == "running":
                    job.update(state="running", message="Import en cours")
                if kind == "progress":
                    fraction = payload.get("fraction")
                    if type(fraction) in (int, float) and 0 <= fraction <= 1:
                        job["progress"] = max(job["progress"], fraction)
                    message = payload.get("message", "")
                    if any(word in message.casefold() for word in ("collection", "piste", "paroles", "tri", "pochette")):
                        job["message"] = message[:200]
                if kind == "finished":
                    # A retry only reports the albums it ran. Retain the
                    # successful releases so the whole request can integrate.
                    job["results"].update((url, outcome) for url, outcome in task.outcomes.items()
                                          if url in job["urls"])
                    outcomes = [job["results"].get(url, {}) for _, url in job["targets"]]
                    statuses = [outcome.get("status") for outcome in outcomes]
                    complete = sum(imported(outcome) for outcome in outcomes)
                    job["journal"] = task.journal
                    if complete == len(outcomes):
                        job.update(state="integrating", message="Actualisation de la bibliothèque")
                    elif all(status == "pending" for status in statuses) or payload.get("event") == "cancelled":
                        job.update(state="cancelled", message="Demande annulée")
                    elif complete or "partial" in statuses:
                        job.update(state="partial", message="Import partiel")
                    else:
                        audio_unavailable = all(outcome.get("category") == "audio_unavailable" for outcome in outcomes)
                        job.update(state="failed", message=CATEGORIES["audio_unavailable"] if audio_unavailable
                                   else "Échec de l’import")
        self.control.blackbox.emit("imports", "error" if kind == "finished" and payload.get("event") == "failure"
                                   else kind or "progress", progress=payload.get("fraction"),
                                   status=payload.get("status"), category=payload.get("category"))
        if kind == "finished":
            self.control.refresh_after_import(task, [job for job in jobs if job["state"] == "integrating"])

    def integrated(self, jobs, receiver, visible_paths):
        with self.lock:
            for job in jobs:
                albums = len(job["targets"])
                tracks = [job["results"].get(url, {}).get("tracks", []) for _, url in job["targets"]]
                paths = {Path(path).resolve() for album_tracks in tracks for path in album_tracks}
                ready = all(tracks) and paths <= visible_paths
                job.update(state="done" if ready else "partial", progress=1.,
                           message=f"{albums} albums dans la bibliothèque" if ready and albums > 1 else
                           "Album dans la bibliothèque" if ready else "Albums manquants dans l’index local")
                for token, (_, requested, add) in job["requests"].items():
                    if not ready or not add or receiver is None:
                        continue
                    ids = receiver.room.match_song_paths(paths, requested["requested_title"],
                                                         requested["requested_artist"])
                    if len(ids) != 1:
                        job["notes"][token] = "Le morceau demandé n’a pas été retrouvé de façon certaine."
                        continue
                    try:
                        receiver.room.choose(token, ids[0])
                    except RoomError:
                        job["notes"][token] = "Ajoute le morceau depuis la bibliothèque."
                    else:
                        job["notes"][token] = "Le morceau a été ajouté à ta sélection."
