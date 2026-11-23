"""Salle éphémère multi-téléphones pour Paroles Strawberry.

Ce module ne pilote ni le lecteur ni GTK depuis les threads HTTP. Tous les
changements arrivent au contrôleur par GLib.idle_add. Aucune voix ni donnée
personnelle n'est conservée sur disque par la salle.
"""
from hashlib import sha256
from secrets import token_hex
from threading import RLock
import time

from gi.repository import GLib
from _kara_party import search_key

MAX_PLAYERS = 20
HEARTBEAT_SECONDS = 35


class RoomError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


class MobileRoom:
    def __init__(self, on_event, reserved_names=()):
        self.on_event = on_event
        self._lock = RLock()
        self._members = {}
        self._catalogue = []
        self._songs = {}
        self._salt = token_hex(16)
        self._reserved = {search_key(name) for name in reserved_names}
        self._locked = False
        self._game_mode = False
        self._current = ''
        self._catalogue_ready = False

    def update_catalogue(self, rows):
        catalogue, songs = [], {}
        for song, normalized in rows:
            if not song.path.with_suffix('.lrc').is_file():
                continue  # Le score exige les paroles. Autres titres disponibles dans la bibliothèque PC.
            key = sha256((self._salt + '\x00' + str(song.path)).encode()).hexdigest()[:20]
            catalogue.append((key, song, normalized))
            songs[key] = song
        with self._lock:
            self._catalogue = catalogue
            self._songs = songs
            self._catalogue_ready = True

    def set_mode(self, *, locked, game_mode, current):
        with self._lock:
            self._locked = bool(locked)
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
            member['last_seen'] = time.monotonic()
            return member

    def join(self, name, token=None):
        if token:
            with self._lock:
                member = self._members.get(token)
                if member:
                    member['last_seen'] = time.monotonic()
                    return {'token': token, 'name': member['name']}
        if not isinstance(name, str):
            raise RoomError('Saisis un pseudo.')
        name = ' '.join(name.strip().split())
        if not 1 <= len(name) <= 35 or any(ord(c) < 32 for c in name):
            raise RoomError('Le pseudo doit contenir entre 1 et 35 caractères.')
        with self._lock:
            if len(self._members) >= MAX_PLAYERS:
                raise RoomError('La salle est pleine.', 409)
            normalized = search_key(name)
            if normalized in self._reserved or any(search_key(m['name']) == normalized for m in self._members.values()):
                raise RoomError('Ce pseudo est déjà utilisé. Choisis-en un autre.', 409)
            secret = token_hex(24)
            member = {'token': secret, 'name': name, 'song': None, 'last_seen': time.monotonic()}
            self._members[secret] = member
        self._emit('join', member)
        return {'token': secret, 'name': name}

    def check(self, token):
        member = self._require(token)
        with self._lock:
            participants = [{'name': m['name'], 'song':
                             self._songs[m['song']].title if m['song'] in self._songs else '',
                             'online': time.monotonic() - m['last_seen'] < HEARTBEAT_SECONDS}
                            for m in self._members.values()]
            selection = member['song']
            selected = self._songs.get(selection)
            return {
                'me': {'name': member['name'],
                       'song': selected.title if selected else '',
                       'artist': selected.artist if selected else ''},
                'participants': participants,
                'game_mode': self._game_mode,
                'locked': self._locked,
                'current': self._current,
                'catalogue_ready': self._catalogue_ready,
            }

    def search(self, token, query, offset=0):
        self._require(token)
        if not isinstance(query, str) or len(query) > 120:
            raise RoomError('Recherche invalide.')
        if not isinstance(offset, int) or offset < 0 or offset > 20000:
            raise RoomError('Pagination invalide.')
        terms = search_key(query).split()
        with self._lock:
            matches = [entry for entry in self._catalogue if all(t in entry[2] for t in terms)]
            page = matches[offset:offset+30]
            return {'total': len(matches), 'offset': offset, 'results': [
                {'id': sid, 'title': song.title, 'artist': song.artist, 'album': song.album}
                for sid, song, _ in page], 'ready': self._catalogue_ready}

    def choose(self, token, song_id):
        member = self._require(token)
        if not isinstance(song_id, str):
            raise RoomError('Choix de chanson invalide.')
        with self._lock:
            if self._locked:
                raise RoomError('Le tour est en cours. Attends avant de modifier ta chanson.', 409)
            song = self._songs.get(song_id)
            if song is None or not song.path.is_file() or not song.path.with_suffix('.lrc').is_file():
                raise RoomError('Cette chanson n’est plus disponible avec paroles.', 404)
            member['song'] = song_id
            result = {'name': member['name'], 'song': song.title, 'artist': song.artist}
        self._emit('choose', member)
        return result

    def clear_choice(self, token):
        member = self._require(token)
        with self._lock:
            if self._locked:
                raise RoomError('Le tour est en cours. Attends avant de modifier ta chanson.', 409)
            if member['song'] is None:
                return {'ok': True}
            member['song'] = None
        self._emit('clear', member)
        return {'ok': True}

    def song_for(self, song_id):
        with self._lock:
            return self._songs.get(song_id)

    def leave(self, token):
        self._require(token)
        with self._lock:
            member = self._members.pop(token, None)
        if member:
            self._emit('leave', member)
        return {'ok': True}

    def kick(self, token):
        with self._lock:
            member = self._members.pop(token, None)
        if member:
            self._emit('leave', member)

    def public_roster(self):
        now = time.monotonic()
        with self._lock:
            return [{'name': m['name'], 'song':
                     self._songs[m['song']].title if m['song'] in self._songs else '',
                     'online': now - m['last_seen'] < HEARTBEAT_SECONDS}
                    for m in self._members.values() if now - m['last_seen'] < HEARTBEAT_SECONDS]
