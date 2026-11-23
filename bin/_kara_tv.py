"""Sert une vue de la partie en lecture seule sur le réseau local."""
from base64 import b64encode
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address
import json
import os
import re
from pathlib import Path
from secrets import token_hex
import subprocess
from tempfile import NamedTemporaryFile
from threading import RLock, Thread
from urllib.parse import parse_qs, urlsplit

from _kara_mobile_room import MobileRoom, RoomError


LAN_RANGES = ("10.", "192.168.", "172.")


def lan_address():
    addresses = json.loads(subprocess.check_output(
        ("ip", "-j", "-4", "address", "show", "up", "scope", "global"), timeout=3
    ))
    routes = json.loads(subprocess.check_output(("ip", "-j", "-4", "route", "show", "default"), timeout=3))
    preferred = {route.get("dev") for route in routes}
    candidates = []
    for interface in addresses:
        name = interface["ifname"]
        if not name.startswith(("en", "eth", "wl", "br", "bond")) or name.startswith(("br-",)):
            continue
        for item in interface.get("addr_info", ()):
            address = IPv4Address(item["local"])
            if address.is_private and str(address).startswith(LAN_RANGES):
                candidates.append((name in preferred, name.startswith(("wl", "en", "eth")), str(address)))
    if not candidates:
        raise OSError("Le PC n’a pas d’adresse sur un réseau local.")
    return max(candidates)[2]


class Receiver(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, html, *, port=0, code=None, on_mobile=None,
                 reserved_names=(), mobile_html=b''):
        super().__init__((address, port), Page)
        self.mobile_html = mobile_html
        self.state = b"{}"
        self._state_lock = RLock()
        self.tv_reload = 0
        self.tv_close = 0
        self.address = address
        self.code = code or token_hex(4)
        self.path = "/" + self.code
        self.url = f"http://{address}:{self.server_port}{self.path}"
        self.join_url = self.url + '/jouer'
        # QR/URL insérés côté serveur : fonctionnent sans JavaScript sur les TV.
        self.html = (html.replace(b'__MOBILE_QR_PATH__', (self.path + '/qr').encode('ascii'))
                         .replace(b'__MOBILE_JOIN_URL__', self.join_url.encode('ascii')))
        self.room = MobileRoom(on_mobile, reserved_names)
        try:
            self.qr = subprocess.check_output(
                ('qrencode', '-t', 'PNG', '-s', '7', '-m', '3', '-o', '-', self.join_url), timeout=3)
        except (OSError, subprocess.SubprocessError):
            self.qr = b''
        # Image intégrée à l'état : contournement fiable des restrictions
        # d'images réseau de certains moteurs Web Samsung/Tizen.
        self.qr_data = b64encode(self.qr).decode('ascii') if self.qr else ''
        Thread(target=self.serve_forever, kwargs={"poll_interval": 0.10}, daemon=True).start()

    def update(self, state):
        with self._state_lock:
            # Révision persistante à travers les rafraîchissements de l’état.
            # Elle ne recrée pas la salle ni ses sessions mobiles.
            payload = {**state, 'tv_reload': self.tv_reload, 'tv_close': self.tv_close,
                       'qr_data': self.qr_data}
            self.state = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()

    def request_tv_reload(self):
        """Demande au navigateur TV de recharger sa page au prochain poll."""
        with self._state_lock:
            self.tv_reload += 1
            try:
                current = json.loads(self.state)
            except (ValueError, TypeError):
                current = {}
            current['tv_reload'] = self.tv_reload
            self.state = json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode()
            return self.tv_reload

    def request_tv_close(self):
        """Demande à l'écran TV de quitter le karaoké, sans fermer la salle."""
        with self._state_lock:
            self.tv_close += 1
            try:
                current = json.loads(self.state)
            except (ValueError, TypeError):
                current = {}
            current['tv_close'] = self.tv_close
            self.state = json.dumps(current, ensure_ascii=False, separators=(",", ":")).encode()
            return self.tv_close

    def close(self):
        self.shutdown()
        self.server_close()

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)
        return sock, address


class Page(BaseHTTPRequestHandler):
    server_version = 'ParolesRoom/1'

    def log_message(self, *_args):
        pass

    def _send(self, status, content, kind='application/json'):
        if kind == 'application/json':
            content = json.dumps(content, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', ('application/json' if kind == 'application/json-raw' else kind) + '; charset=utf-8')
        self.send_header('Content-Length', str(len(content)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _request_path(self):
        if self.headers.get('Host') != f'{self.server.address}:{self.server.server_port}':
            raise RoomError('Adresse de salle invalide.', 403)
        request = urlsplit(self.path)
        if not request.path.startswith(self.server.path + '/') and request.path != self.server.path:
            raise RoomError('Salle introuvable.', 404)
        return request

    def _authorized_token(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            raise RoomError('Rejoins d’abord la salle.', 401)
        return auth.removeprefix('Bearer ').strip()

    def do_GET(self):
        try:
            request = self._request_path()
            path = request.path
            base = self.server.path
            if path == base:
                return self._send(200, self.server.html, 'text/html')
            if path == base + '/etat':
                return self._send(200, self.server.state, 'application/json-raw')
            if path == base + '/jouer':
                return self._send(200, self.server.mobile_html, 'text/html')
            if path == base + '/qr':
                if not self.server.qr:
                    raise RoomError('QR code indisponible.', 404)
                return self._send(200, self.server.qr, 'image/png')
            if path == base + '/api/etat':
                return self._send(200, self.server.room.check(self._authorized_token()))
            if path == base + '/api/recherche':
                params = parse_qs(request.query, keep_blank_values=True)
                query = params.get('q', [''])[0]
                try:
                    offset = int(params.get('offset', ['0'])[0])
                except (ValueError, TypeError):
                    raise RoomError('Pagination invalide.')
                return self._send(200, self.server.room.search(self._authorized_token(), query, offset))
            raise RoomError('Page introuvable.', 404)
        except RoomError as error:
            return self._send(error.status, {'error': str(error)})

    def do_POST(self):
        try:
            request = self._request_path()
            base = self.server.path
            if not request.path.startswith(base + '/api/'):
                raise RoomError('Action inconnue.', 404)
            # Origine strictement identique : une page malveillante externe ne
            # peut pas utiliser le navigateur pour rejoindre ou modifier la salle.
            origin = self.headers.get('Origin')
            if origin != f'http://{self.server.address}:{self.server.server_port}':
                raise RoomError('Origine non autorisée.', 403)
            if self.headers.get_content_type() != 'application/json':
                raise RoomError('Type de contenu invalide.', 415)
            try:
                count = int(self.headers.get('Content-Length', '-1'))
            except ValueError:
                count = -1
            if not 0 <= count <= 2048:
                raise RoomError('Requête trop longue.', 413)
            try:
                data = json.loads(self.rfile.read(count))
            except (UnicodeError, ValueError):
                raise RoomError('JSON invalide.')
            if not isinstance(data, dict):
                raise RoomError('Requête invalide.')
            if request.path == base + '/api/inscription':
                return self._send(200, self.server.room.join(data.get('name'), data.get('token')))
            token = self._authorized_token()
            if request.path == base + '/api/choix':
                return self._send(200, self.server.room.choose(token, data.get('song_id')))
            if request.path == base + '/api/retirer-choix':
                return self._send(200, self.server.room.clear_choice(token))
            if request.path == base + '/api/quitter':
                return self._send(200, self.server.room.leave(token))
            raise RoomError('Action inconnue.', 404)
        except RoomError as error:
            return self._send(error.status, {'error': str(error)})


def pairing_code():
    path = Path.home() / ".local/state/kara/tv-code"
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists():
        with NamedTemporaryFile(mode="w", dir=path.parent, prefix=".tv-code-") as draft:
            draft.write(token_hex(4) + "\n")
            draft.flush()
            try:
                os.link(draft.name, path)
            except FileExistsError:
                pass
    code = path.read_text().strip()
    if not re.fullmatch(r"[0-9a-f]{8}", code):
        raise ValueError("Le code de jumelage TV est illisible.")
    return code


def start_receiver(on_mobile=None, reserved_names=()):
    html = Path(__file__).with_name('_kara_tv.html').read_bytes()
    mobile_html = Path(__file__).with_name('_kara_mobile.html').read_bytes()
    return Receiver(lan_address(), html, port=8765, code=pairing_code(),
                    on_mobile=on_mobile, reserved_names=reserved_names,
                    mobile_html=mobile_html)
