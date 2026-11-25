"""Salle éphémère multi-téléphones pour Paroles Strawberry.

Ce module ne pilote ni le lecteur ni GTK depuis les threads HTTP. Tous les
changements arrivent au contrôleur par GLib.idle_add. Aucune voix ni donnée
personnelle n'est conservée sur disque par la salle.
"""
from hashlib import sha256
from secrets import compare_digest, randbelow, token_hex
from threading import RLock
import time

from gi.repository import GLib
from _kara_party import search_key

MAX_PLAYERS = 20
HEARTBEAT_SECONDS = 35
MAX_SONGS = 12
ORGANIZER_CODE_SECONDS = 5 * 60
ORGANIZER_ATTEMPTS = 5


class RoomError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class MobileRoom:
    def __init__(self, on_event, reserved_names=(), *, diagnostics=None, admin_code=None):
        self.on_event = on_event
        self._lock = RLock()
        self._members = {}
        self._catalogue = []
        self._songs = {}
        self._covers = {}
        self._prepared = set()
        self._preparation_failures = {}
        self._salt = token_hex(16)
        self._reserved = {search_key(name) for name in reserved_names}
        self._locked = False
        self._editable = set()
        self._game_mode = False
        self._current = ''
        self._catalogue_ready = False
        self.diagnostics = diagnostics
        self.admin_code = admin_code
        self._admin_expires = time.monotonic() + ORGANIZER_CODE_SECONDS if admin_code else 0
        self._attempts = 0
        self._party = {}
        self._catalogue_revision = 0
        self._registrations = {}

    def emit(self, event, member=None, **data):
        if self.diagnostics is not None:
            self.diagnostics.emit("mobile", event, player=member["name"] if member else None, **data)

    def publish(self, state):
        with self._lock:
            self._party = state

    def update_catalogue(self, rows):
        catalogue, songs, covers = [], {}, {}
        album_art = {}
        for song, normalized in rows:
            key = sha256((self._salt + '\x00' + str(song.path)).encode()).hexdigest()[:20]
            catalogue.append((key, song, normalized, song.path.with_suffix('.lrc').is_file()))
            songs[key] = song
            folder = song.path.parent
            if folder not in album_art:
                cover = folder / "cover.jpg"
                album_art[folder] = cover if cover.is_file() and not cover.is_symlink() else None
            if album_art[folder]:
                covers[key] = album_art[folder]
        with self._lock:
            self._catalogue = catalogue
            self._songs = songs
            self._covers = covers
            self._catalogue_ready = True
            self._catalogue_revision += 1

    def set_mode(self, *, locked, game_mode, current, editable=()):
        with self._lock:
            self._locked = bool(locked)
            self._editable = set(editable)
            self._game_mode = bool(game_mode)
            self._current = current or ''

    def _emit(self, action, member):
        if self.on_event:
            GLib.idle_add(self.on_event, action, dict(member))

    def _require(self, token):
        if not isinstance(token, str) or len(token) != 48:
            raise RoomError('Session non reconnue. Rejoins la salle.', 401)
        with self._lock:
            member = self._members.get(token)
            if member is None:
                raise RoomError('Session expirée. Rejoins la salle.', 401)
            if time.monotonic() - member['last_seen'] >= HEARTBEAT_SECONDS:
                self.emit("reconnect", member)
                self._emit("reconnect", member)
            member['last_seen'] = time.monotonic()
            member['online'] = True
            return member

    def join(self, name, token=None, registration=None):
        if token:
            member = self._require(token)
            return {'token': token, 'name': member['name']}
        if not isinstance(name, str):
            raise RoomError('Saisis un pseudo.')
        name = ' '.join(name.strip().split())
        if not 1 <= len(name) <= 35 or any(ord(c) < 32 for c in name):
            raise RoomError('Le pseudo doit contenir entre 1 et 35 caractères.')
        if registration is not None and (not isinstance(registration, str) or len(registration) != 48
                                         or any(c not in "0123456789abcdef" for c in registration)):
            raise RoomError("Inscription invalide.")
        with self._lock:
            old = self._registrations.get(registration)
            if old in self._members and self._members[old]["name"] == name:
                return {"token": old, "name": name}
            if len(self._members) >= MAX_PLAYERS:
                raise RoomError('La salle est pleine.', 409)
            normalized = search_key(name)
            if normalized in self._reserved or any(search_key(m['name']) == normalized for m in self._members.values()):
                raise RoomError('Ce pseudo est déjà utilisé. Choisis-en un autre.', 409)
            secret = token_hex(24)
            member = {'token': secret, 'name': name, 'song': None, 'songs': [], 'ready': False,
                      'organizer': False, 'online': True, 'last_seen': time.monotonic()}
            self._members[secret] = member
            if registration:
                self._registrations[registration] = secret
            self.emit("join", member)
        self._emit('join', member)
        return {'token': secret, 'name': name}

    def check(self, token):
        with self._lock:
            member = self._require(token)
            participants = [{'name': m['name'], 'song':
                             self._songs[m['song']].title if m['song'] in self._songs else '',
                             'preparation': self.describe(m['song'], self._songs[m['song']])['preparation']
                             if m['song'] in self._songs else None,
                             'ready': m['ready'], 'count': len(m['songs']),
                             'online': time.monotonic() - m['last_seen'] < HEARTBEAT_SECONDS}
                            for m in self._members.values()]
            if member["organizer"]:
                for row, participant in zip(participants, self._members.values()):
                    row["songs"] = [self.describe(key, self._songs[key]) for key in participant["songs"]
                                    if key in self._songs]
            selection = member['song']
            selected = self._songs.get(selection)
            return {
                'me': {'name': member['name'], 'organizer': member['organizer'], 'ready': member['ready'],
                       'mix': self._party.get('mix', {}).get(member['name'], {}),
                   'unscored': self._party.get('unscored', {}).get(member['name'], []),
                       'songs': [self.describe(key, self._songs[key]) for key in member['songs'] if key in self._songs],
                       'song': selected.title if selected else '',
                       'artist': selected.artist if selected else ''},
                'participants': participants,
                'game_mode': self._game_mode,
                'locked': self._locked and token not in self._editable,
                'current': self._current,
                'catalogue_ready': self._catalogue_ready,
                'catalogue_revision': self._catalogue_revision,
                'party': {**self._party.get("public", {}),
                          **(self._party.get("admin", {}) if member["organizer"] else {}),
                          "results": self._party.get("results", {}).get(member["name"], [])},
            }

    def issue_admin_code(self):
        """Open a short enrollment window from Parole. Existing admins keep their session."""
        with self._lock:
            self.admin_code = f"{randbelow(10_000):04d}"
            self._admin_expires = time.monotonic() + ORGANIZER_CODE_SECONDS
            self._attempts = 0
            return self.admin_code

    def current_admin_code(self):
        """Called only by the authenticated TV display, outside room results."""
        with self._lock:
            if time.monotonic() >= self._admin_expires:
                self.issue_admin_code()
            return self.admin_code

    def authorize(self, token, code=None, *, enroll=False):
        with self._lock:
            member = self._require(token)
            if member["organizer"]:
                return member["name"]
            if enroll:
                if time.monotonic() >= self._admin_expires:
                    raise RoomError("Attends l’affichage du nouveau code sur la télévision.", 403)
                # The budget belongs to the short code, across all browser sessions.
                if self._attempts >= ORGANIZER_ATTEMPTS:
                    raise RoomError("Trop de tentatives. Attends le renouvellement du code sur la télévision.", 429)
                if (self.admin_code and isinstance(code, str) and code.isascii()
                        and compare_digest(code.upper(), self.admin_code)):
                    member["organizer"] = True
                    self.emit("organizer", member)
                    return member["name"]
                self._attempts += 1
            self.emit("error", member, operation="organizer-access")
            raise RoomError("Accès organisateur refusé.", 403)

    def describe(self, key, song, lyrics=None):
        return {'id': key, 'title': song.title, 'artist': song.artist, 'album': song.album,
                'duration': song.duration, 'lyrics': lyrics, 'cover': key in self._covers,
                'preparation': ('ready' if song.path in self._prepared else
                                'failed' if song.path in self._preparation_failures else None)}

    def set_prepared(self, paths, *, failures=None):
        with self._lock:
            self._prepared = set(paths)
            self._preparation_failures = dict(failures or {})

    def cover(self, token, key):
        with self._lock:
            if token is not None:
                self._require(token)
            return self._covers.get(key)

    def player_token(self, name):
        with self._lock:
            return next((token for token, member in self._members.items() if member["name"] == name), None)

    def is_online(self, token):
        with self._lock:
            member = self._members.get(token)
            return bool(member and time.monotonic() - member["last_seen"] < HEARTBEAT_SECONDS)

    def reserve_turn(self, token, song):
        with self._lock:
            member = self._members.get(token)
            if not member or not member["ready"] or all(self._songs.get(key) != song for key in member["songs"]):
                return False
            # Freeze the active singer atomically with the authoritative selection check.
            self._locked = True
            self._editable.discard(token)
            return True

    def search(self, token, query, offset=0, *, mode="all", artist="", album=""):
        member = self._require(token) if token is not None else None
        if not isinstance(query, str) or len(query) > 120:
            raise RoomError('Recherche invalide.')
        if not isinstance(offset, int) or offset < 0 or offset > 20000:
            raise RoomError('Pagination invalide.')
        if mode not in ("all", "title", "artist", "album") or max(len(artist), len(album)) > 180:
            raise RoomError('Filtre musical invalide.')
        terms = search_key(query).split()
        with self._lock:
            matches = []
            for entry in self._catalogue:
                _, song, normalized, _ = entry
                if (artist and song.artist != artist) or (album and song.album != album):
                    continue
                field = normalized if mode == "all" else search_key(getattr(song, mode))
                if all(term in field for term in terms):
                    matches.append(entry)
            page = matches[offset:offset+30]
            if offset == 0:
                self.emit("search", member, found=len(matches), mode=mode, query_length=len(query))
            return {'total': len(matches), 'offset': offset, 'results': [
                self.describe(sid, song, lyrics)
                for sid, song, _, lyrics in page], 'ready': self._catalogue_ready}

    def choose(self, token, song_id, *, confirm=False, replace=False):
        if not isinstance(song_id, str):
            raise RoomError('Choix de chanson invalide.')
        with self._lock:
            member = self._require(token)
            if self._locked and token not in self._editable:
                raise RoomError('Le tour est en cours. Attends avant de modifier ta chanson.', 409)
            song = self._songs.get(song_id)
            if song is None or not song.path.is_file():
                raise RoomError('Cette chanson n’est plus disponible.', 404)
            if song_id in member['songs'] and not replace and not confirm:
                return {'name': member['name'], 'song': song.title, 'artist': song.artist}
            if not replace and song_id not in member['songs'] and len(member['songs']) >= MAX_SONGS:
                raise RoomError('Ta sélection est pleine.', 409)
            if replace:
                member['songs'] = [song_id]
            elif song_id not in member['songs']:
                member['songs'] = [*member['songs'], song_id]
            member['song'] = member['songs'][0]
            member['ready'] = confirm
            result = {'name': member['name'], 'song': song.title, 'artist': song.artist}
            self.emit("selection", member, song=song_id, action="add")
            self._emit('choose', member)
        return result

    def clear_choice(self, token, song_id=None):
        with self._lock:
            member = self._require(token)
            if self._locked and token not in self._editable:
                raise RoomError('Le tour est en cours. Attends avant de modifier ta chanson.', 409)
            if member['song'] is None:
                return {'ok': True}
            if song_id is not None and song_id not in member['songs']:
                return {'ok': True}
            member['songs'] = [sid for sid in member['songs'] if song_id is not None and sid != song_id]
            member['song'] = next(iter(member['songs']), None)
            member['ready'] = False
            self.emit("selection", member, song=song_id, action="remove")
            self._emit('choose' if member['song'] is not None else 'clear', member)
        return {'ok': True}

    def reorder(self, token, ids):
        with self._lock:
            member = self._require(token)
            if self._locked and token not in self._editable:
                raise RoomError("La sélection est verrouillée.", 409)
            if (not isinstance(ids, list) or len(ids) != len(member["songs"])
                    or any(not isinstance(sid, str) for sid in ids) or set(ids) != set(member["songs"])):
                raise RoomError("Ordre des chansons invalide.")
            member.update(songs=ids, song=next(iter(ids), None), ready=False)
            self._emit('choose' if ids else 'clear', member)
        return {'ok': True}

    def ready(self, token, ready):
        with self._lock:
            member = self._require(token)
            if ((self._locked and token not in self._editable) or type(ready) is not bool
                    or (ready and not member["songs"] and not member["organizer"])):
                raise RoomError("Choisis une chanson avant de te déclarer prêt.", 409)
            member["ready"] = ready
            self.emit("readiness", member, ready=ready)
            self._emit("choose", member)
        return {"ok": True}

    def selections(self):
        with self._lock:
            return {token: (member["ready"], member["organizer"],
                            [self._songs.get(sid) for sid in member["songs"]])
                    for token, member in self._members.items()}

    def reset_ready(self):
        with self._lock:
            for member in self._members.values():
                member["ready"] = False

    def identity(self, token):
        return self._require(token)["name"]

    def song_for(self, song_id):
        with self._lock:
            return self._songs.get(song_id)

    def match_song_paths(self, paths, title, artist):
        with self._lock:
            return [sid for sid, song in self._songs.items() if song.path in paths
                    and search_key(song.title).strip() == search_key(title).strip()
                    and search_key(song.artist).strip() == search_key(artist).strip()]

    def leave(self, token):
        with self._lock:
            self._require(token)
            member = self._members.pop(token, None)
            self._registrations = {key: owner for key, owner in self._registrations.items() if owner != token}
            if member:
                self.emit("leave", member)
                self._emit('leave', member)
        return {'ok': True}

    def kick(self, token):
        with self._lock:
            member = self._members.pop(token, None)
            self._registrations = {key: owner for key, owner in self._registrations.items() if owner != token}
        if member:
            self._emit('leave', member)

    def public_roster(self):
        now = time.monotonic()
        with self._lock:
            # Keep a disconnected player's slot and selection. The LAN
            # heartbeat contributes one diagnostic edge per loss, not per poll.
            for member in self._members.values():
                if member["online"] and now - member['last_seen'] >= HEARTBEAT_SECONDS:
                    member["online"] = False
                    self.emit("disconnect", member, cause="heartbeat")
            return [{'name': m['name'], 'song':
                     self._songs[m['song']].title if m['song'] in self._songs else '',
                     'online': now - m['last_seen'] < HEARTBEAT_SECONDS}
                    for m in self._members.values() if now - m['last_seen'] < HEARTBEAT_SECONDS]
