"""Affichage Strawberry en lecture seule sur le réseau local."""
from base64 import b64encode
from collections import OrderedDict
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from ipaddress import IPv4Address, ip_network
import json
import os
import re
import ssl
import stat
import sys
from pathlib import Path
from secrets import compare_digest, token_hex
import subprocess
from tempfile import NamedTemporaryFile
from threading import BoundedSemaphore, RLock, Thread
import time
from urllib.parse import parse_qs, unquote, urlsplit

from _kara_paths import BIN, CONFIG, STATE


LAN_RANGES = ("10.", "192.168.", "172.")
DISPLAY_ROUTE = "/tv/etat"
DISPLAY_DIAGNOSTIC = "/tv/diagnostic"


class DisplayError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


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


class DisplayListener(ThreadingHTTPServer):
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


def current_cover(track, art_url):
    """Return a bounded inline image for the current MPRIS track.

    Strawberry exports embedded art as a user-owned temporary file. Only
    that format and the album's own sidecar may cross the TV boundary.
    """
    temp = Path(os.environ.get("TMPDIR", "/tmp")).resolve()
    try:
        url = urlsplit(art_url or "")
        path = Path(unquote(url.path))
    except ValueError:
        url, path = urlsplit(""), Path()
    candidates = []
    if (url.scheme == "file" and url.netloc in ("", "localhost") and path.parent == temp
            and re.fullmatch(r"strawberry-cover-[A-Za-z0-9_-]{1,64}\.(?:jpeg|jpg|png)", path.name)):
        candidates.append(path)
    if track:
        candidates.extend(track.parent / name for name in ("cover.jpg", "cover.png"))
    for candidate in candidates:
        try:
            with open(candidate, "rb", opener=lambda name, flags: os.open(
                    name, flags | os.O_NOFOLLOW | os.O_NONBLOCK)) as source:
                info = os.fstat(source.fileno())
                if (not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= 524288
                        or candidate.parent == temp and info.st_uid != os.getuid()):
                    continue
                data = source.read(524289)
        except OSError:
            continue
        if len(data) > 524288:
            continue
        if data.startswith(b"\xff\xd8\xff"):
            media = "image/jpeg"
        elif data.startswith(b"\x89PNG\r\n\x1a\n"):
            media = "image/png"
        else:
            continue
        return sha256(data).hexdigest()[:16], f"data:{media};base64," + b64encode(data).decode("ascii")
    return "", ""


class Receiver(DisplayListener):

    def __init__(self, address, *, port=0, code=None, network=None, https=None,
                 diagnostics=None, display_key=None):
        super().__init__(address, port, network=network)
        self.receiver = self
        self.https_listener = None
        self.host = f"{address}:{self.server_port}"
        self.path = "/" + (code or token_hex(4))
        self.url = "http://" + self.host + self.path + "/"
        self.cover = "", ""
        self.state = {}
        self._state_lock = RLock()
        self.tv_reload = 0
        self.tv_close = 0
        self._tv_needs_open = True
        self.tv_seen = 0.
        self.secure_tv_seen = 0.
        self.display_key = display_key
        self.tv_diagnostic_seen = 0.
        self.capture_revisions = OrderedDict()
        self.diagnostics = diagnostics
        if https:
            origin, https_port, context = https
            try:
                listener = DisplayListener(address, https_port, network=network, tls=context)
            except OSError:
                self.server_close()
                raise
            self.https_listener = listener
            listener.receiver = self
            listener.host = urlsplit(origin).netloc
        self.start()
        if self.https_listener:
            self.https_listener.start()

    def update(self, state, *, cover=("", "")):
        with self._state_lock:
            self.cover = cover
            self.state = dict(state)
            revision = state.get("line_revision")
            if revision is not None and revision not in self.capture_revisions:
                self.capture_revisions[revision] = (state.get("position"), time.monotonic_ns(), state.get("line"))
                if len(self.capture_revisions) > 128:
                    self.capture_revisions.popitem(last=False)

    def request_tv_reload(self):
        with self._state_lock:
            self._tv_needs_open = False
            self.tv_reload += 1

    def resume_tv_display(self):
        with self._state_lock:
            if self._tv_needs_open:
                self.request_tv_reload()

    def request_tv_close(self):
        with self._state_lock:
            self._tv_needs_open = True
            self.tv_close += 1

    def close(self):
        if self.https_listener:
            self.https_listener.shutdown()
            self.https_listener.server_close()
        self.shutdown()
        self.server_close()

    def tv_request(self, *, secure_display=False, cover_revision=None):
        with self._state_lock:
            clock = time.monotonic()
            if clock - self.tv_seen > 4 and self.diagnostics:
                self.diagnostics.emit("television", "reconnect" if self.tv_seen else "connect")
            self.tv_seen = clock
            if secure_display:
                self.secure_tv_seen = clock
            revision, cover = self.cover
            state = dict(self.state, tv_reload=self.tv_reload, tv_close=self.tv_close,
                         cover_revision=revision)
        if cover_revision != revision and cover:
            state["cover_data"] = cover
        return state

    def tv_report(self, params, *, secure_display=False):
        event = params.get("event", [""])[0]
        screen_events = ("screensaver-on", "screensaver-off", "screensaver-error")
        if event in screen_events:
            if not secure_display:
                raise DisplayError("Diagnostic réservé à la Samsung.", 403)
        elif event not in ("render-error", "rendered", "remote-back", "remote-reopen", "display-disconnect"):
            raise DisplayError("Diagnostic TV inconnu.")
        clock = time.monotonic()
        with self._state_lock:
            if clock - self.tv_diagnostic_seen < .2:
                return
            self.tv_diagnostic_seen = clock
        if event in screen_events:
            # Only the authenticated Samsung can report its API result.
            print(f"Kara TV {event}", flush=True)
        fields = {}
        for name in ("revision", "browser_ms", "rtt_ms"):
            value = params.get(name, [""])[0]
            if re.fullmatch(r"[0-9]{1,12}(?:\.[0-9]{1,3})?", value):
                fields[name] = float(value)
        if self.diagnostics:
            with self._state_lock:
                evidence = self.capture_revisions.get(fields.get("revision"))
                position = self.state.get("position")
            if evidence:
                position, published, line = evidence
                fields.update(line=line, software_observation_ns=time.monotonic_ns() - published)
            elif event == "rendered":
                # An unknown revision cannot be linked to a current position.
                position = None
            self.diagnostics.emit("television", "error" if event == "render-error" else event,
                                  position=position, **fields)


class Page(BaseHTTPRequestHandler):
    server_version = "KaraTV"

    def log_message(self, *_args):
        pass

    def send_data(self, status, content, kind="application/json"):
        if kind == "application/json":
            content = json.dumps(content, ensure_ascii=False, separators=(",", ":")).encode()
        self.send_response(status)
        self.send_header("Content-Type", kind + ("; charset=utf-8" if kind != "image/png" else ""))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy",
                         "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'")
        if self.server.tls and self.path.partition("?")[0] in (DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        try:
            self.wfile.write(content)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass

    def do_GET(self):
        receiver = self.server.receiver
        try:
            if self.headers.get("Host") != self.server.host:
                raise DisplayError("Adresse de télévision invalide.", 403)
            if len(self.path) > 4096:
                raise DisplayError("Adresse de télévision trop longue.", 414)
            request = urlsplit(self.path)
            path = request.path
            base = receiver.path
            secure = path in (DISPLAY_ROUTE, DISPLAY_DIAGNOSTIC)
            if secure:
                key = self.headers.get("Authorization", "")
                if (not self.server.tls or not receiver.display_key or not key.isascii()
                        or not compare_digest(key, "Bearer " + receiver.display_key)):
                    raise DisplayError("Écran Kara non autorisé.", 403)
            params = parse_qs(request.query, keep_blank_values=True)
            if path in (DISPLAY_ROUTE, base + "/etat"):
                return self.send_data(200, receiver.tv_request(
                    secure_display=secure, cover_revision=params.get("cover", [None])[0]))
            if path in (DISPLAY_DIAGNOSTIC, base + "/diagnostic"):
                receiver.tv_report(params, secure_display=secure)
                return self.send_data(200, {"ok": True})
            # The LAN browser uses the packaged renderer, with a relative
            # endpoint and no copy of the private Samsung bearer key.
            files = {"": ("index.html", "text/html"), "index.html": ("index.html", "text/html"),
                     "app.js": ("app.js", "text/javascript"), "style.css": ("style.css", "text/css"),
                     "icon.png": ("icon.png", "image/png")}
            if path == base:
                self.send_response(308)
                self.send_header("Location", base + "/")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path == base + "/display-config.js":
                return self.send_data(200, b"window.KARA_DISPLAY_ENDPOINT = './etat';", "text/javascript")
            if path.startswith(base + "/") and (asset := files.get(path[len(base) + 1:])):
                name, kind = asset
                return self.send_data(200, (BIN.parent / "tizen/app" / name).read_bytes(), kind)
            raise DisplayError("Page introuvable.", 404)
        except DisplayError as error:
            return self.send_data(error.status, {"error": str(error)})
        except (OSError, ValueError):
            return self.send_data(400, {"error": "Requête d’affichage invalide."})

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
        self.send_data(404, {"error": "Page introuvable."})


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


def start_receiver(*, diagnostics=None):
    address, network = lan_interface()
    key_path = CONFIG / "kara/tv-display-key"
    display_key = key_path.read_text().strip() if key_path.is_file() else None
    if display_key is not None and not re.fullmatch(r"[0-9a-f]{64}", display_key):
        raise ValueError("La clé de la télévision Kara est invalide.")
    return Receiver(address, port=8765, code=pairing_code(), network=network, https=https_settings(),
                    diagnostics=diagnostics, display_key=display_key)
