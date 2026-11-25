"""Requests and catalogue editions for the shared Parole import queue."""
from collections import OrderedDict
from contextlib import nullcontext
import json
import sqlite3
from pathlib import Path
from subprocess import DEVNULL, run, SubprocessError
from tempfile import TemporaryFile
from threading import BoundedSemaphore, Lock
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, build_opener
from uuid import uuid4

from _kara_catalogue import ALBUM, CHANNEL, is_youtube_cover_url
from _kara_import_queue import digest, import_key
from _kara_import_resources import check_space
from _kara_import_result import CATEGORIES, ImportIssue
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
        self.tokens = {}
        self.queue = None

    def bind_queue(self, queue):
        self.queue = queue
        if queue is None:
            return
        for request in queue.requests():
            request["urls"] = frozenset(url for _, url in request["targets"])
            self.jobs[request["id"]] = request

    def copy_for_storage(self, job):
        return {key: value for key, value in job.items() if key != "urls"}

    def persist(self, job):
        if self.queue is None:
            raise RoomError("La file Parole est indisponible.", 503)
        try:
            self.queue.save_request(self.copy_for_storage(job))
        except (OSError, sqlite3.Error) as error:
            raise RoomError("Impossible d’enregistrer la demande dans Parole.", 503) from error

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
            selected_artist = None
            if data.get("all") is True:
                discography = self.discographies.get(handle)
                if discography is None:
                    raise RoomError("Rouvre la liste des albums de l’artiste.")
                title = discography["name"] or "Discographie"
                albums = discography["albums"]
                if discography.get("name") and discography.get("artist_id"):
                    selected_artist = discography["name"], discography["artist_id"]
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
            routes = ({url: (*selected_artist, album["id"]) for (_, url), album in zip(targets, albums)}
                      if selected_artist else {})
            direct = data.get("direct") is True and data.get("all") is not True and data.get("add") is False
            urls = frozenset(url for _, url in targets)
            if self.queue is None:
                raise RoomError("La file Parole est indisponible.", 503)
            try:
                check_space(self.queue.directory, state=True)
            except (OSError, ImportIssue) as error:
                raise RoomError("Espace disque insuffisant pour enregistrer la demande.", 507) from error
            # Keep room credentials in memory. Stored approvals carry only the
            # reference used to find a member while that room is still alive.
            member = digest(token)
            self.tokens[member] = token
            if requested[1] is not None:
                selected = requested[1]
                requested = (name, {field: selected.get(field) for field in
                                    ("requested_title", "requested_artist")}, requested[2])
            for existing in self.jobs.values():
                if existing["state"] not in FINISHED and urls == existing["urls"]:
                    revised = {**existing, "requests": {**existing["requests"], member: requested}}
                    if routes and existing["state"] == "approval":
                        revised["artist_routes"] = {**routes, **existing.get("artist_routes", {})}
                    if direct and existing["state"] == "approval":
                        self.enqueue(revised)
                    else:
                        self.persist(revised)
                    existing.update(revised)
                    return {"job": existing["id"], "state": existing["state"]}
            identity = uuid4().hex
            job = {"id": identity, "title": title, "targets": targets, "urls": urls,
                   "tracks": track_count, "state": "approval", "progress": 0.,
                   "message": "En attente de l’organisateur",
                   "requests": {member: requested}, "notes": {}, "results": {}, "artist_routes": routes}
            if direct:
                self.enqueue(job)
            else:
                self.persist(job)
            self.jobs[identity] = job
        self.control.blackbox.emit("imports", "requested", player=name, job=identity, album=title, albums=len(targets))
        return {"job": identity, "state": job["state"]}

    def enqueue(self, job):
        # The shared scheduler picks up the committed request on its next
        # poll. Commit the receipt and lot together before replying to mobile.
        revised = {**job, "state": "queued", "message": "Dans la file Parole"}
        try:
            task_id = self.queue.enqueue(job["targets"], request=self.copy_for_storage(revised),
                                         artist_routes=job.get("artist_routes"))
        except (OSError, sqlite3.Error) as error:
            raise RoomError("Impossible d’enregistrer ce lot dans Parole.", 503) from error
        job.update(revised, task_id=task_id)

    def pending(self):
        with self.lock:
            return any(job["state"] in ("approval", "queued", "running", "stopping", "integrating")
                       for job in self.jobs.values())

    def queue_status(self):
        return self.control.album_import.imports_snapshot[1]

    def snapshot(self, *, organizer=False, token=None, history=False, before=None):
        if organizer and history and self.queue:
            return [dict(id=f"queue:{row['id']}", title=row["targets"][0][0] or "Import par lien",
                         albums=len(row["targets"]), state=row["state"], message=row["message"],
                         cursor=str(row["seq"])) for row in self.queue.history(before)]
        queue_rows, _ = self.control.album_import.imports_snapshot
        member = digest(token) if token else None
        by_id = {row["id"]: row for row in queue_rows}
        result = []
        requesters = {}
        seeking = history and before is not None
        with self.lock, (self.queue.connect() if history and self.queue else nullcontext()) as db:
            if db is not None:
                db.execute("BEGIN")
            for job in reversed(self.jobs.values()):
                if not organizer and member not in job["requests"]:
                    continue
                if (job["state"] in ("done", "cancelled")) != history:
                    continue
                if seeking:
                    seeking = job["id"] != before
                    continue
                if db is not None and job.get("task_id") and self.queue.history_removed(job["task_id"], db):
                    continue
                key = f"queue:{job.get('task_id')}"
                task = by_id.get(key, {})
                if organizer and task and not history:
                    requesters.setdefault(key, set()).update(request[0] for request in job["requests"].values())
                    continue
                state = job["state"]
                message = job["message"]
                if task and (state in ("queued", "running") or state != "done" and
                             task["state"] in ("queued", "running", "suspending", "suspended")):
                    state, message = task["state"], task["message"]
                result.append(dict(id=job["id"], title=job["title"], tracks=job["tracks"],
                                   albums=len(job["targets"]), state=state, message=message,
                                   progress=task.get("progress", job["progress"]),
                                   completed=task.get("completed", sum(imported(outcome) for outcome in job["results"].values())),
                                   processed=task.get("processed", len({path for outcome in job["results"].values()
                                                                        for path in outcome.get("tracks", [])})),
                                   errors=task.get("errors", 0), note=job["notes"].get(member, ""),
                                   remaining=task.get("remaining", []),
                                   players=[request[0] for request in job["requests"].values()] if organizer else []))
                if history and len(result) == 25:
                    break
        if not organizer or history:
            return result
        return result + [{**row, "players": sorted(requesters.get(row["id"], ()))}
                         for row in queue_rows]

    def action(self, identity, command, *, owner=None):
        panel = self.control.album_import
        if not isinstance(identity, str):
            raise RoomError("Demande inconnue.", 404)
        if identity.startswith("queue:"):
            if owner is not None:
                raise RoomError("Demande inconnue.", 404)
            apply = {"cancel": panel.cancel_job, "retry": panel.retry_job}.get(command)
            if apply and apply(identity.removeprefix("queue:")):
                return
            raise RoomError("Cette action est indisponible pour ce lot.", 409)
        with self.lock:
            job = self.jobs.get(identity)
            if job is None or owner is not None and (command != "retry" or owner not in job["requests"]):
                raise RoomError("Demande inconnue.", 404)
            state = job["state"]
            if command == "cancel" and state == "approval":
                revised = {**job, "state": "cancelled", "message": "Demande annulée"}
                self.persist(revised)
                job.update(revised)
                return
            task_id = job.get("task_id")
            if command == "approve" and self.control.remote_party.locked:
                raise RoomError("La demande ne peut pas être lancée maintenant.", 409)
            if command == "approve" and state == "approval":
                revised = {**job, "state": "queued", "message": "Dans la file Parole"}
                try:
                    if self.queue is None:
                        raise OSError("File indisponible")
                    check_space(self.queue.directory, state=True)
                    task_id = self.queue.enqueue(job["targets"], request=self.copy_for_storage(revised))
                except (OSError, ImportIssue, sqlite3.Error) as error:
                    raise RoomError("Impossible d’enregistrer ce lot dans Parole.", 503) from error
                job.update(revised, task_id=task_id)
            elif command not in ("retry", "cancel") or not task_id:
                raise RoomError("La demande ne peut pas être lancée maintenant.", 409)
        if command == "approve":
            panel.launch_next(force=True)
        elif command == "retry" and panel.retry_job(task_id):
            with self.lock:
                job.update(state="queued", message="Relance en attente…", progress=0.)
                self.persist(job)
        elif command == "cancel":
            task = panel.by_id.get(task_id)
            if task and task.keys == {import_key(url) for _, url in job["targets"]} and panel.cancel_job(task_id):
                return
            raise RoomError("Ce lot ne peut pas être annulé depuis cette demande.", 409)
        else:
            raise RoomError("Aucun album en échec à relancer.", 409)

    def update(self, task, kind, payload):
        if kind not in ("settled", "recovered"):
            return
        integrating = []
        # Only sanitized states reach the room. The journal and audio paths
        # remain local for diagnostics and catalogue integration.
        outcomes_by_key = {import_key(url): outcome for url, outcome in task.outcomes.items()}
        with self.lock:
            for job in self.jobs.values():
                if job.get("task_id") != task.identity or job["state"] == "done":
                    continue
                results = {url: outcomes_by_key[import_key(url)] for _, url in job["targets"]
                           if import_key(url) in outcomes_by_key}
                outcomes = [results.get(url, {}) for _, url in job["targets"]]
                complete = sum(imported(outcome) for outcome in outcomes)
                if complete == len(outcomes):
                    state, message = "integrating", "Actualisation de la bibliothèque"
                    integrating.append(job)
                elif task.final_event == "cancelled":
                    state, message = "cancelled", "Demande annulée"
                elif complete or any(outcome.get("status") == "partial" for outcome in outcomes):
                    state, message = "partial", "Import partiel"
                else:
                    unavailable = all(outcome.get("category") == "audio_unavailable" for outcome in outcomes)
                    state, message = "failed", CATEGORIES["audio_unavailable"] if unavailable else "Échec de l’import"
                job.update(state=state, message=message, results=results, progress=task.fraction)
                self.persist(job)
        self.control.blackbox.emit("imports", "finished", status=task.final_event,
                                   progress=task.fraction)
        self.control.refresh_after_import(task, integrating)

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
                for member, (_, requested, add) in job["requests"].items():
                    token = self.tokens.get(member)
                    if not ready or not add or receiver is None or token is None:
                        continue
                    ids = receiver.room.match_song_paths(paths, requested["requested_title"],
                                                         requested["requested_artist"])
                    if len(ids) != 1:
                        job["notes"][member] = "Le morceau demandé n’a pas été retrouvé de façon certaine."
                        continue
                    try:
                        receiver.room.choose(token, ids[0])
                    except RoomError:
                        job["notes"][member] = "Ajoute le morceau depuis la bibliothèque."
                    else:
                        job["notes"][member] = "Le morceau a été ajouté à ta sélection."
                self.persist(job)
