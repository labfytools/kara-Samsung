"""Sert une vue de la partie en lecture seule sur le réseau local."""
from base64 import b64encode
from collections import OrderedDict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, ip_network
import json
import os
import re
import ssl
import sys
from pathlib import Path
from secrets import compare_digest, token_hex
import subprocess
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore, RLock, Thread
import time
from urllib.parse import parse_qs, urlsplit

from _kara_mobile_room import MobileRoom, RoomError
from _kara_paths import BIN, CONFIG, STATE


LAN_RANGES = ("10.", "192.168.", "172.")
DISPLAY_ROUTE = "/tv/etat"
DISPLAY_DIAGNOSTIC = "/tv/diagnostic"


def lan_interface():
    selector = os.environ.get("KARA_LAN_ADDRESS")
    settings = CONFIG / "kara/https.json"
    if selector is None and settings.is_file():
        config = json.loads(settings.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Configuration HTTPS invalide.")
        selector = config.get("lan_address")
    if selector is not None:
        if not isinstance(selector, str):
            raise ValueError("L’adresse LAN Kara doit être une IPv4 locale.")
        selector = str(IPv4Address(selector))
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
            if (address.is_private and str(address).startswith(LAN_RANGES)
                    and (selector is None or str(address) == selector)):
                candidates.append((name in preferred, name.startswith(("wl", "en", "eth")),
                                   str(address), item["prefixlen"]))
    if not candidates:
        raise OSError("L’adresse LAN choisie pour Kara est absente." if selector else
                      "Le PC n’a pas d’adresse sur un réseau local.")
    _, _, address, prefix = max(candidates)
    return address, ip_network(f"{address}/{prefix}", strict=False)


def lan_address():
    return lan_interface()[0]


class RoomListener(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 32

    def __init__(self, address, port, *, network=None, tls=None):
        self.network = network
        self.tls = tls
        self.slots = BoundedSemaphore(24)
        super().__init__((address, port), Page)

    def verify_request(self, request, address):
        return self.network is None or IPv4Address(address[0]) in self.network

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except BaseException:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        secured = None
        try:
            # A slow TLS handshake occupies one bounded worker, never the listener.
            if self.tls:
                try:
                    secured = self.tls.wrap_socket(request, server_side=True)
                except OSError:
                    return
            super().process_request_thread(secured or request, address)
        finally:
            self.close_request(secured or request)
            self.slots.release()

    def get_request(self):
        sock, address = super().get_request()
        sock.settimeout(3)
        return sock, address

    def handle_error(self, request, address):
        # A released TV or browser socket can vanish during request parsing.
        if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError, TimeoutError)):
            super().handle_error(request, address)

    def start(self):
        Thread(target=self.serve_forever, kwargs={"poll_interval": 0.10}, daemon=True).start()


def https_configured():
    return bool(os.environ.get("KARA_HTTPS_ORIGIN") or (CONFIG / "kara/https.json").is_file())


def https_settings():
    path = CONFIG / "kara/https.json"
    if not https_configured():
        return None
    settings = {}
    if "KARA_HTTPS_ORIGIN" not in os.environ:
        settings = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            raise ValueError("Configuration HTTPS invalide.")
    origin = os.environ.get("KARA_HTTPS_ORIGIN") or settings.get("origin")
    if not isinstance(origin, str):
        raise ValueError("Origine HTTPS manquante.")
    parsed = urlsplit(origin)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
            or parsed.path or parsed.query or parsed.fragment
            or not re.fullmatch(r"[a-zA-Z0-9.-]+(?::[0-9]+)?", parsed.netloc)):
        raise ValueError("KARA_HTTPS_ORIGIN doit être une origine HTTPS sans chemin.")
    port = parsed.port if parsed.port is not None else 443
    tls_dir = STATE / "kara/https"
    active = tls_dir / "current" if (tls_dir / "current").is_symlink() else tls_dir
    fullchain = os.environ.get("KARA_HTTPS_FULLCHAIN") or settings.get("fullchain") or active / "fullchain.pem"
    key = os.environ.get("KARA_HTTPS_KEY") or settings.get("key") or active / "privkey.pem"
    if not all(isinstance(value, (str, Path)) and Path(value).is_absolute() for value in (fullchain, key)):
        raise ValueError("Les chemins du certificat et de la clé HTTPS doivent être absolus.")
    fullchain, key = Path(fullchain), Path(key)
    context = rotating_context(fullchain, key)
    return origin, port, context


def rotating_context(fullchain, key):
    def pair():
        if fullchain.parent == key.parent:
            # Resolve the generation once. A simultaneous atomic publish
            # cannot pair the old leaf with the new key.
            folder = fullchain.parent.resolve(strict=True)
            return folder / fullchain.name, folder / key.name
        return fullchain, key

    def stamp(paths):
        return tuple((entry.st_ino, entry.st_mtime_ns, entry.st_size)
                     for entry in (path.stat() for path in paths))

    def load(paths):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(*paths)
        return context

    paths = pair()
    current, version = load(paths), stamp(paths)
    initial = current
    lock = RLock()

    def refresh(connection, _name, _context):
        nonlocal current, version
        with lock:
            try:
                paths = pair()
                fresh = stamp(paths)
                if fresh != version:
                    # Swap the per-handshake context. Existing HTTPS and WSS
                    # connections keep the generation they negotiated.
                    current, version = load(paths), fresh
            except (OSError, ValueError):
                pass  # Keep the last verified pair until publication succeeds.
            connection.context = current

    initial.sni_callback = refresh
    return initial


class Receiver(RoomListener):

    def __init__(self, address, html, *, port=0, code=None, on_mobile=None,
                 reserved_names=(), mobile_html=b'', network=None, https=None,
                 diagnostics=None, commands=None, admin_code=None, imports=None, audio=None,
                 display_key=None):
        super().__init__(address, port, network=network)
        self.receiver = self
        self.https_listener = None
        self.host = f"{address}:{self.server_port}"
        self.origin = f"http://{self.host}"
        self.mobile_html = mobile_html
        self.state = b"{}"
        self.state_no_qr = b"{}"
        self._state_lock = RLock()
        self.tv_reload = 0
        self.tv_close = 0
        # A previously open Tizen client may still hold a close screen from
        # the previous room. The first player can clear it without SDB.
        self._tv_needs_open = True
        self.tv_seen = 0.
        self.secure_tv_seen = 0.
        self.display_key = display_key
        self.tv_diagnostic_seen = 0.
        self.capture_turn = None
        self.capture_position = None
        self.capture_revisions = OrderedDict()
        self.diagnostics = diagnostics
        self.commands = commands
        self.imports = imports
        self.audio = audio
        self.address = address
        self.code = code or token_hex(4)
        self.path = "/" + self.code
        self.url = self.origin + self.path
        if https:
            origin, https_port, context = https
            try:
                listener = RoomListener(address, https_port, network=network, tls=context)
            except OSError:
                self.server_close()
                raise
            self.https_listener = listener
            listener.receiver = self
            listener.origin = origin
            listener.host = urlsplit(origin).netloc
        self.join_url = (https[0] if https else self.origin) + self.path + '/jouer'
        # QR/URL insérés côté serveur : fonctionnent sans JavaScript sur les TV.
        self.html = (html.replace(b'__MOBILE_QR_PATH__', (self.path + '/qr').encode('ascii'))
                         .replace(b'__MOBILE_JOIN_URL__', self.join_url.encode('ascii')))
        self.room = MobileRoom(on_mobile, reserved_names, diagnostics=diagnostics, admin_code=admin_code)
        self.mobile_script = (BIN / "_kara_mobile.js").read_bytes() if commands else b""
        try:
            self.qr = subprocess.check_output(
                ('qrencode', '-t', 'PNG', '-s', '7', '-m', '3', '-o', '-', self.join_url), timeout=3)
        except (OSError, subprocess.SubprocessError):
            self.qr = b''
        # Image intégrée à l'état : contournement fiable des restrictions
        # d'images réseau de certains moteurs Web Samsung/Tizen.
        self.qr_data = b64encode(self.qr).decode('ascii') if self.qr else ''
        self.start()
        if self.https_listener:
            self.https_listener.start()
            if self.audio:
                try:
                    self.audio.start(address, network, origin, int(os.environ.get("KARA_HTTPS_AUDIO_PORT", https_port + 1)),
                                     context, silent=os.environ.get("KARA_SILENT") == "1")
                except (OSError, ValueError):
                    self.audio.failure = "Configuration de l’écouteur WSS invalide."

    def update(self, state):
        with self._state_lock:
            # Révision persistante à travers les rafraîchissements de l’état.
            # Elle ne recrée pas la salle ni ses sessions mobiles.
            payload = {**state, 'tv_reload': self.tv_reload, 'tv_close': self.tv_close,
                       'join_url': self.join_url}
            self._update_states(payload)

    def _update_states(self, payload):
        self.capture_turn = payload.get("turn")
        self.capture_position = payload.get("position")
        revision = payload.get("line_revision")
        if revision is not None and revision not in self.capture_revisions:
            self.capture_revisions[revision] = (self.capture_turn, self.capture_position, time.monotonic_ns(),
                                                payload.get("line"))
            if len(self.capture_revisions) > 128:
                self.capture_revisions.popitem(last=False)
        self.state_no_qr = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        self.state = json.dumps({**payload, 'qr_data': self.qr_data}, ensure_ascii=False, separators=(",", ":")).encode()

    def request_tv_reload(self):
        """Demande au navigateur TV de recharger sa page au prochain poll."""
        with self._state_lock:
            self._tv_needs_open = False
            self.tv_reload += 1
            try:
                current = json.loads(self.state_no_qr)
            except (ValueError, TypeError):
                current = {}
            current['tv_reload'] = self.tv_reload
            self._update_states(current)
            return self.tv_reload

    def resume_tv_display(self):
        """Reopen the display once after room startup or a TV close command."""
        with self._state_lock:
            if self._tv_needs_open:
                self.request_tv_reload()

    def request_tv_close(self):
        """Demande à l'écran TV de quitter le karaoké, sans fermer la salle."""
        with self._state_lock:
            self._tv_needs_open = True
            self.tv_close += 1
            try:
                current = json.loads(self.state_no_qr)
            except (ValueError, TypeError):
                current = {}
            current['tv_close'] = self.tv_close
            self._update_states(current)
            return self.tv_close

    def close(self):
        if self.audio:
            self.audio.shutdown()
        if self.https_listener:
            self.https_listener.shutdown()
            self.https_listener.server_close()
        self.shutdown()
        self.server_close()

    def tv_request(self, client=False, *, include_qr=True, secure_display=False):
        if client:
            with self._state_lock:
                clock = time.monotonic()
                if clock - self.tv_seen > 4 and self.diagnostics:
                    self.diagnostics.emit("television", "reconnect" if self.tv_seen else "connect",
                                          turn=self.capture_turn)
                self.tv_seen = clock
                if secure_display:
                    self.secure_tv_seen = clock
        raw = self.state if include_qr else self.state_no_qr
        if not secure_display:
            return raw
        state = json.loads(raw)
        state["join_url"] = self.join_url
        if include_qr:
            state["qr_data"] = self.qr_data
        if state.get("party_phase", "lobby") in ("lobby", "preparing", "ready"):
            state["organizer_pin"] = self.room.current_admin_code()
        return state

    def tv_report(self, params):
        event = params.get("event", [""])[0]
        if event not in ("render-error", "rendered", "remote-back", "remote-reopen", "display-disconnect"):
            raise RoomError("Diagnostic TV inconnu.")
        clock = time.monotonic()
        with self._state_lock:
            if clock - self.tv_diagnostic_seen < .2:
                return
            self.tv_diagnostic_seen = clock
        fields = {}
        for name in ("revision", "browser_ms", "rtt_ms"):
            value = params.get(name, [""])[0]
            if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,3})?", value):
                fields[name] = float(value)
        if self.diagnostics:
            with self._state_lock:
                evidence = self.capture_revisions.get(fields.get("revision"))
                turn, position = self.capture_turn, self.capture_position
            if evidence:
                turn, position, published, line = evidence
                fields.update(line=line, software_observation_ns=time.monotonic_ns() - published)
            elif event == "rendered":
                # A stale or unknown revision cannot be assigned to the
                # currently displayed line or to a different turn.
                turn, position = None, None
            self.diagnostics.emit("television", "error" if event == "render-error" else event,
                                  turn=turn, position=position, **fields)


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
        if self.server.tls and self.path.partition("?")[0] in (DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC):
            # A packaged Tizen widget can have an opaque origin. Only the
            # bearer-authenticated, read-only display route accepts it.
            self.send_header("Access-Control-Allow-Origin", "*")
        audio = self.server.receiver.audio
        allowed_audio = ' ' + audio.url.removesuffix('/audio') if audio and audio.url else ''
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'" + allowed_audio + "; object-src 'none'; base-uri 'none'")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def _request_path(self):
        if self.headers.get('Host') != self.server.host:
            raise RoomError('Adresse de salle invalide.', 403)
        if len(self.path) > 4096:
            raise RoomError("Adresse de salle trop longue.", 414)
        try:
            request = urlsplit(self.path)
        except ValueError as error:
            raise RoomError("Adresse de salle invalide.") from error
        base = self.server.receiver.path
        if not request.path.startswith(base + '/') and request.path not in (base, DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC):
            raise RoomError('Salle introuvable.', 404)
        return request

    def _authorized_token(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            raise RoomError('Rejoins d’abord la salle.', 401)
        return auth.removeprefix('Bearer ').strip()

    def _private_transport(self):
        if not (self.server.tls or IPv4Address(self.client_address[0]).is_loopback
                and IPv4Address(self.server.receiver.address).is_loopback):
            raise RoomError("La commande exige le lien HTTPS de la salle.", 403)

    def do_GET(self):
        try:
            request = self._request_path()
            path = request.path
            receiver = self.server.receiver
            base = receiver.path
            if path in (DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC):
                key = self.headers.get("Authorization", "")
                if (not self.server.tls or not receiver.display_key or not key.isascii()
                        or not compare_digest(key, "Bearer " + receiver.display_key)):
                    raise RoomError("Écran Kara non autorisé.", 403)
                params = parse_qs(request.query)
                if path == DISPLAY_DIAGNOSTIC:
                    receiver.tv_report(params)
                    return self._send(200, {"ok": True})
                include_qr = params.get("qr", ["1"])[0] != "0"
                return self._send(200, receiver.tv_request(client=True, include_qr=include_qr, secure_display=True))
            if path == base:
                return self._send(200, receiver.html, 'text/html')
            if path == base + '/etat':
                include_qr = parse_qs(request.query).get("qr", ["1"])[0] != "0"
                return self._send(200, receiver.tv_request(client=True, include_qr=include_qr), 'application/json-raw')
            if path == base + '/tv-diagnostic':
                receiver.tv_report(parse_qs(request.query))
                return self._send(200, {"ok": True})
            if path == base + '/jouer':
                if receiver.https_listener and not self.server.tls:
                    self.send_response(307)
                    self.send_header('Location', receiver.join_url)
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                return self._send(200, receiver.mobile_html, 'text/html')
            if path == base + '/mobile.js' and receiver.mobile_script:
                return self._send(200, receiver.mobile_script, 'text/javascript')
            if path == base + '/logo.png':
                return self._send(200, (BIN.parent / "tizen/app/icon.png").read_bytes(), 'image/png')
            if path == base + '/micro-worklet.js' and self.server.tls and receiver.audio:
                return self._send(200, (BIN / "_kara_micro_worklet.js").read_bytes(), 'text/javascript')
            if path == base + '/qr':
                if not receiver.qr:
                    raise RoomError('QR code indisponible.', 404)
                return self._send(200, receiver.qr, 'image/png')
            if path.startswith(base + '/api/') and receiver.https_listener and not self.server.tls:
                raise RoomError('Utilise le lien HTTPS de la salle.', 403)
            if path == base + '/api/etat':
                token = self._authorized_token()
                state = receiver.room.check(token)
                if receiver.imports:
                    state["party"]["imports"] = receiver.imports.snapshot(
                        organizer=state["me"]["organizer"], token=token)
                    state["party"]["import_queue"] = receiver.imports.queue_status()
                return self._send(200, state)
            if path == base + '/api/historique-imports' and receiver.imports:
                self._private_transport()
                token = self._authorized_token()
                state = receiver.room.check(token)
                params = parse_qs(request.query)
                organizer = state["me"]["organizer"] and params.get("mine") != ["1"]
                before = params.get("before", [None])[0]
                if before is not None:
                    if not re.fullmatch(r"[0-9]{1,18}" if organizer else r"[a-f0-9]{32}", before):
                        raise RoomError("Page d’historique invalide.")
                    before = int(before) if organizer else before
                return self._send(200, receiver.imports.snapshot(
                    organizer=organizer, token=token, history=True, before=before))
            if path == base + '/api/recherche':
                params = parse_qs(request.query, keep_blank_values=True)
                query = params.get('q', [''])[0]
                try:
                    offset = int(params.get('offset', ['0'])[0])
                except (ValueError, TypeError):
                    raise RoomError('Pagination invalide.')
                if not self.headers.get("Authorization"):
                    self._private_transport()
                return self._send(200, receiver.room.search(
                    self._authorized_token() if self.headers.get("Authorization") else None,
                    query, offset, mode=params.get('mode', ['all'])[0],
                    artist=params.get('artist', [''])[0], album=params.get('album', [''])[0]))
            if path.startswith(base + '/api/pochette/'):
                key = path.rsplit('/', 1)[-1]
                if not self.headers.get("Authorization"):
                    self._private_transport()
                cover = receiver.room.cover(self._authorized_token() if self.headers.get("Authorization") else None, key)
                if cover and not cover.is_symlink():
                    try:
                        with open(cover, "rb", opener=lambda name, flags: os.open(name, flags | os.O_NOFOLLOW)) as stream:
                            data = stream.read(2 * 1024 * 1024 + 1)
                        if data.startswith(b'\xff\xd8\xff') and len(data) <= 2 * 1024 * 1024:
                            return self._send(200, data, 'image/jpeg')
                    except OSError:
                        pass
                raise RoomError("Pochette indisponible.", 404)
            if receiver.imports and path.startswith(base + '/api/pochette-externe/'):
                self._private_transport()
                receiver.room.identity(self._authorized_token())
                data, mime = receiver.imports.cover(path.rsplit("/", 1)[-1])
                return self._send(200, data, mime)
            raise RoomError('Page introuvable.', 404)
        except RoomError as error:
            return self._send(error.status, {'error': str(error)})

    def do_OPTIONS(self):
        if self.server.tls and self.path.partition("?")[0] in (DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC):
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Authorization")
            self.send_header("Access-Control-Allow-Methods", "GET")
            self.send_header("Access-Control-Max-Age", "600")
            self.end_headers()
        else:
            self.send_error(404)

    def do_POST(self):
        try:
            request = self._request_path()
            receiver = self.server.receiver
            base = receiver.path
            if receiver.https_listener and not self.server.tls:
                raise RoomError('Utilise le lien HTTPS de la salle.', 403)
            if not request.path.startswith(base + '/api/'):
                raise RoomError('Action inconnue.', 404)
            # Origine strictement identique : une page malveillante externe ne
            # peut pas utiliser le navigateur pour rejoindre ou modifier la salle.
            origin = self.headers.get('Origin')
            if origin != self.server.origin:
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
            except (UnicodeError, ValueError, RecursionError):
                raise RoomError('JSON invalide.')
            if not isinstance(data, dict):
                raise RoomError('Requête invalide.')
            if request.path == base + '/api/inscription':
                return self._send(200, receiver.room.join(data.get('name'), data.get('token'), data.get("registration")))
            token = self._authorized_token()
            if request.path == base + '/api/choix':
                return self._send(200, receiver.room.choose(token, data.get('song_id'),
                                  confirm=data.get("confirm") is True, replace=data.get("replace") is True))
            if request.path == base + '/api/retirer-choix':
                return self._send(200, receiver.room.clear_choice(token, data.get('song_id')))
            if request.path == base + '/api/quitter':
                if receiver.room.check(token)['locked']:
                    raise RoomError("La partie est en cours.", 409)
                return self._send(200, receiver.room.leave(token))
            if request.path == base + '/api/ordre':
                return self._send(200, receiver.room.reorder(token, data.get('songs')))
            if request.path == base + '/api/pret':
                return self._send(200, receiver.room.ready(token, data.get('ready')))
            if request.path == base + '/api/organisateur':
                self._private_transport()
                return self._send(200, {"name": receiver.room.authorize(token, data.get("code"), enroll=True)})
            if request.path == base + '/api/signaler':
                name = receiver.room.identity(token)
                if receiver.diagnostics:
                    with receiver._state_lock:
                        context = dict(turn=receiver.capture_turn, position=receiver.capture_position)
                    try:
                        receiver.diagnostics.mark(data.get("kind", "problem"), data.get("note", ""),
                                                  player=name, **context)
                    except ValueError as error:
                        raise RoomError(str(error)) from error
                return self._send(200, {"ok": True})
            if request.path == base + '/api/diagnostic':
                name = receiver.room.identity(token)
                kind, view = data.get("kind"), data.get("view")
                if kind not in ("screen", "error", "micro-permission", "micro-interruption") or view not in (
                        "party", "library", "selection", "imports", "results", "mic", "admin"):
                    raise RoomError("Diagnostic mobile invalide.")
                if receiver.diagnostics:
                    receiver.diagnostics.emit("mobile", kind, player=name, view=view)
                return self._send(200, {"ok": True})
            if request.path == base + '/api/reglages-joueur' and receiver.commands is not None:
                self._private_transport()
                name = receiver.room.identity(token)
                return self._send(200, receiver.commands("audio-personal", name, data))
            if request.path == base + '/api/micro-ticket' and receiver.audio:
                self._private_transport()
                receiver.room.identity(token)
                return self._send(200, receiver.audio.ticket(token, consent=data.get("consent") is True))
            if receiver.commands is not None and request.path == base + '/api/demarrer':
                self._private_transport()
                name = receiver.room.identity(token)
                return self._send(200, receiver.commands("player-start", name, data))
            if receiver.commands is not None and request.path == base + '/api/relancer-import' and receiver.imports:
                self._private_transport()
                name = receiver.room.identity(token)
                return self._send(200, receiver.commands("player-retry-import", name, data))
            if receiver.commands is not None and request.path == base + '/api/relancer-chanson':
                self._private_transport()
                name = receiver.room.identity(token)
                return self._send(200, receiver.commands("player-retry-song", name, data))
            if receiver.commands is not None and request.path == base + '/api/couper-retour':
                self._private_transport()
                name = receiver.room.identity(token)
                return self._send(200, receiver.commands("audio-return-cut", name, data))
            if receiver.imports is not None and request.path == base + '/api/albums-externes':
                self._private_transport()
                receiver.room.identity(token)
                return self._send(200, receiver.imports.lookup(data.get("title"), data.get("artist")))
            if receiver.imports is not None and request.path in (
                    base + '/api/artistes-externes', base + '/api/albums-artiste', base + '/api/edition-externe'):
                self._private_transport()
                receiver.room.identity(token)
                action = request.path.rsplit('/', 1)[-1]
                if action == "artistes-externes":
                    result = receiver.imports.artists(data.get("query"))
                elif action == "albums-artiste":
                    result = receiver.imports.albums(data.get("artist"))
                else:
                    result = receiver.imports.edition(data.get("album"))
                return self._send(200, result)
            if receiver.imports is not None and request.path == base + '/api/importer-album':
                self._private_transport()
                state = receiver.room.check(token)
                if state["locked"]:
                    raise RoomError("Attends la fin de la partie.", 409)
                member = state["me"]
                result = receiver.imports.request(token, member["name"], data)
                needs_approval = result.pop("state") == "approval"
                if data.get("all") is True and member["organizer"] and receiver.commands is not None:
                    # An authenticated organizer already confirmed rights. Use
                    # the shared GTK approval path and a stable receipt for this lot.
                    if needs_approval:
                        receiver.commands("import-approve", member["name"],
                                          {"job": result["job"], "request": result["job"]})
                    result["direct"] = True
                return self._send(200, result)
            if receiver.commands is not None and request.path.startswith(base + '/api/admin/'):
                self._private_transport()
                name = receiver.room.authorize(token)
                return self._send(200, receiver.commands(request.path.rsplit('/', 1)[-1], name, data))
            raise RoomError('Action inconnue.', 404)
        except RoomError as error:
            return self._send(error.status, {'error': str(error)})


def pairing_code():
    path = STATE / "kara/tv-code"
    legacy = Path.home() / ".local/state/kara/tv-code"
    if path != legacy and not path.exists() and legacy.is_file():
        path = legacy
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


def start_receiver(on_mobile=None, reserved_names=(), *, diagnostics=None, commands=None, admin_code=None,
                   imports=None, audio=None):
    html = (BIN / '_kara_tv.html').read_bytes()
    mobile_html = (BIN / '_kara_mobile.html').read_bytes()
    address, network = lan_interface()
    key_path = CONFIG / "kara/tv-display-key"
    display_key = key_path.read_text().strip() if key_path.is_file() else None
    if display_key is not None and not re.fullmatch(r"[0-9a-f]{64}", display_key):
        raise ValueError("La clé de la télévision Kara est invalide.")
    return Receiver(address, html, port=8765, code=pairing_code(), network=network, https=https_settings(),
                    on_mobile=on_mobile, reserved_names=reserved_names,
                    mobile_html=mobile_html, diagnostics=diagnostics, commands=commands, admin_code=admin_code,
                    imports=imports, audio=audio, display_key=display_key)
