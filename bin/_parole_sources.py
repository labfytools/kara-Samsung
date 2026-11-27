"""Registre des fournisseurs et transport HTTPS."""
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from email.utils import parsedate_to_datetime
from hashlib import sha256
import http.client
import ipaddress
import json
from math import ceil, isfinite
import re
import socket
import ssl
from time import monotonic, time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit

from _kara_import_resources import (AUDIO_INTERVALS, RATE_DIRECTORY, checkpoint, current_pause, defer_service,
                                    download_event, resource_slot, wait_service)
from _kara_import_result import ImportIssue
from _parole_index import document
from _parole_import_providers import ARCHIVE, AUDIO_HOSTS, JAMENDO, MANUAL, YOUTUBE
from _parole_import_metrics import Transfer, limited, observe
from _parole_lrc_credit import RED, UNISON, red_contact
from _parole_lyrics_lookup import backoff

BUILTINS = {
    "LRCLIB": ("lrclib.net", "lyrics,lrc", "https://lrclib.net/docs"),
    "lyrics.ovh": ("api.lyrics.ovh", "lyrics", "https://lyricsovh.docs.apiary.io/"),
    "NetEase": ("music.163.com", "lyrics,lrc", ""),
    "QQ Music": ("y.qq.com", "lyrics,lrc", ""),
    "Genius": ("genius.com", "lyrics", ""),
    "LyricDown": ("lyricdown.com", "lrc", ""),
    RED: ("lrc.red", "lrc", "https://lrc.red/api"),
    UNISON: ("unison.betterlyrics.org", "lyrics_candidate,lrc", "https://docs.betterlyrics.org/unison"),
    "YouTube Music": ("music.youtube.com", "catalogue,cover,import", ""),
    ARCHIVE: ("archive.org", "catalogue,import", "https://archive.org/developers/md-read.html"),
    JAMENDO: ("api.jamendo.com", "catalogue,import", "https://developer.jamendo.com/v3.0/authentication"),
    "MusicBrainz": ("musicbrainz.org", "catalogue,discography", "https://musicbrainz.org/doc/MusicBrainz_API"),
    "Cover Art Archive": ("coverartarchive.org", "cover", ""),
    "Deezer": ("api.deezer.com", "cover", ""),
    "Apple Music": ("itunes.apple.com", "cover", ""),
    "TheAudioDB": ("www.theaudiodb.com", "cover", ""),
}
HOST = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{1,62}")
PATH = re.compile(r"/[a-zA-Z0-9_/-]{0,200}")
MAX_RESPONSE = 1_000_000
DEFAULT_REQUESTS = 200
RECENT_SOURCE_LIMIT = 128
RECENT_SOURCE_KIND = "source_latency_samples"
LYRICS_STATS = "lyrics_source_statistics"
LYRICS_RATE = "lyrics_source_remote_quota"
LYRICS_BROKER_UPSTREAMS = ("LRCLIB", "NetEase")
LYRICS_DEFAULT_ORDER = ("LRCLIB", RED, "NetEase", "QQ Music", UNISON, "Genius", "LyricDown", "lyrics.ovh")

SOURCE_OUTCOMES = frozenset(("found", "absent", "error", "ambiguous", "different_edition"))
COVER_PROVIDERS = {host: identity for identity, (host, category, _) in BUILTINS.items()
                   if "cover" in category or identity == "MusicBrainz"}


class ProviderUnavailable(OSError):
    pass


@contextmanager
def request_slot(host, timeout):
    # LRCLIB requests must finish before another starts. Both discovery and
    # the built-in lyrics client share the same interprocess slot.
    if host == "lrclib.net":
        with resource_slot(RATE_DIRECTORY / "lrclib-http", 1, timeout=timeout):
            try:
                yield
            finally:
                defer_service(host, .25)
    else:
        yield


def provider_limits(config):
    limits = config.get("rate", {})
    # LRCLIB documents per-minute, not daily, allowances. Give its anonymous
    # client a bounded local budget and a conservative 30-request/min pace.
    # Explicit user settings and the transport's Retry-After still win.
    lrclib = config.get("host") == "lrclib.net"
    daily = limits.get("daily", 2000 if lrclib else DEFAULT_REQUESTS)
    seconds = limits.get("seconds", 2 if lrclib else 0)
    if type(daily) is not int or not 0 <= daily <= 2000 or type(seconds) is not int or not 0 <= seconds <= 300:
        raise ValueError("Limites de recherche fournisseur invalides")
    return daily, seconds


def configure_limits(index, identity, daily, seconds):
    limits = {"daily": daily, "seconds": seconds}
    provider_limits({"rate": limits})
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT config FROM sources WHERE id=?", (identity,)).fetchone()
        if not row:
            raise ValueError("Fournisseur inconnu")
        config = json.loads(row[0])
        config["rate"] = limits
        db.execute("UPDATE sources SET config=? WHERE id=?", (document(config), identity))


def lyrics_priority(row):
    configured = row["decoded"].get("lyrics_priority")
    defaults = {name: i for i, name in enumerate(LYRICS_DEFAULT_ORDER)}
    return (configured if type(configured) is int and configured >= 0
            else len(defaults) + defaults.get(row["id"], len(defaults)), row["id"])


def lyrics_rows(index, *, db=None, now=None):
    now = time() if now is None else now
    if db is None:
        with index.db(wait=.2) as connection:
            return lyrics_rows(index, db=connection, now=now)
    rows = db.execute("""SELECT s.*,coalesce(u.used,0) AS used_today,l.until AS limited_until,
                                l.status AS last_http, r.value AS lyrics_stats, q.value AS remote_quota,
                                q.checked AS quota_checked
                         FROM sources s LEFT JOIN daily_usage u
                           ON u.bucket='provider:'||s.id AND u.day=?
                         LEFT JOIN import_provider_limits l ON l.source=s.id AND l.until>?
                         LEFT JOIN reviews r ON r.kind=? AND r.subject=s.id
                         LEFT JOIN reviews q ON q.kind=? AND q.subject=s.id
                         WHERE s.adapter='lrclib' OR s.category LIKE '%lyrics%' OR s.category='lrc'""",
                      (date.fromtimestamp(now).isoformat(), now, LYRICS_STATS, LYRICS_RATE)).fetchall()
    result = []
    for raw in rows:
        row = dict(raw)
        row["decoded"] = json.loads(row["config"])
        daily, row["interval"] = provider_limits(row["decoded"])
        row["remaining"] = max(0, daily - row["used_today"])
        row["lyrics_stats"] = json.loads(row["lyrics_stats"]) if row["lyrics_stats"] else {}
        row["remote_quota"] = (json.loads(row["remote_quota"])
                               if row["quota_checked"] and now - row["quota_checked"] < 300 else None)
        if row["state"] != "active":
            row.update(availability=row["state"], next_available=None)
        elif row["id"] == RED and not red_contact():
            row.update(availability="configuration", next_available=None)
        elif not daily:
            row.update(availability="paused", next_available=None)
        else:
            reopen = row["limited_until"]
            if not row["remaining"]:
                tomorrow = date.fromtimestamp(now) + timedelta(days=1)
                midnight = datetime.combine(tomorrow, datetime.min.time()).timestamp()
                reopen = max(reopen or 0, midnight)
            row.update(availability="limited" if reopen else "ready", next_available=reopen)
        result.append(row)
    by_id = {row["id"]: row for row in result}
    broker = by_id.get("lyrics.ovh")
    if broker and broker["availability"] == "ready":
        upstreams = [by_id.get(name, {}) for name in LYRICS_BROKER_UPSTREAMS
                     if by_id.get(name, {}).get("availability") != "ready"]
        if upstreams:
            dates = [row.get("next_available") for row in upstreams]
            broker.update(availability="upstream", next_available=max(dates) if all(dates) else None)
    return sorted(result, key=lyrics_priority)


def move_lyrics_source(index, identity, delta):
    if delta not in (-1, 1):
        raise ValueError("Déplacement de fournisseur invalide")
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        # The broker route retains its execution semantics.
        rows = [row for row in lyrics_rows(index, db=db) if row["id"] != "lyrics.ovh"]
        position = next((i for i, row in enumerate(rows) if row["id"] == identity), -1)
        neighbor = position + delta
        if position < 0 or not 0 <= neighbor < len(rows):
            return
        rows[position], rows[neighbor] = rows[neighbor], rows[position]
        for order, row in enumerate(rows):
            config = dict(row["decoded"], lyrics_priority=order)
            db.execute("UPDATE sources SET config=? WHERE id=?", (document(config), row["id"]))


def record_lyrics(index, identity, synced):
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?", (LYRICS_STATS, identity)).fetchone()
        value = json.loads(row[0]) if row else {}
        form = "lrc" if synced else "plain"
        value[form] = value.get(form, 0) + 1
        db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                   (LYRICS_STATS, identity, "observed", time(), document(value)))


def record_quota(index, identity, headers):
    # Only an explicit numeric remaining value is presented as remote.
    # Stale observations don't affect admission or masquerade as local limits.
    raw = headers.get("RateLimit-Remaining") or headers.get("X-RateLimit-Remaining")
    if raw and re.fullmatch(r"[0-9]{1,12}", raw):
        index.review(LYRICS_RATE, identity, "observed", int(raw))


def provider_policy(index, identity, *, validation=False):
    with index.db() as db:
        row = db.execute("SELECT state,config FROM sources WHERE id=?", (identity,)).fetchone()
    if not row or (row["state"] != "active" and not (validation and row["state"] != "excluded")):
        raise ProviderUnavailable("Fournisseur désactivé")
    daily, configured = provider_limits(json.loads(row["config"]))
    if not daily:
        raise ProviderUnavailable("Limite quotidienne du fournisseur atteinte")
    return daily, configured


def ready_sources(index, now):
    """Advisory snapshot; the transport still reserves each request atomically."""
    with index.db(wait=.15) as db:
        rows = db.execute("""SELECT s.id,s.category,s.config,coalesce(u.used,0) AS used
                             FROM sources s LEFT JOIN daily_usage u
                               ON u.bucket='provider:'||s.id AND u.day=?
                             WHERE s.state='active' AND NOT EXISTS
                               (SELECT 1 FROM import_provider_limits l WHERE l.source=s.id AND l.until>?)""",
                          (date.fromtimestamp(now).isoformat(), now)).fetchall()
    return {row["id"]: row for row in rows
            if row["used"] < provider_limits(json.loads(row["config"]))[0]
            and (row["id"] != RED or red_contact())}


def night_lrc_providers(index, *, now=None, db=None):
    """Providers with an approved adapter that can deliver native LRC."""
    from _parole_lrclib_local import configured_snapshot

    direct = {"LRCLIB", RED, UNISON, "NetEase", "QQ Music"}
    providers = [row for row in lyrics_rows(index, db=db, now=now)
                 if row["state"] == "active" and "lrc" in row["category"].split(",")
                 and (row["id"] in direct or row["adapter"] == "lrclib" and row["decoded"].get("validated"))]
    if configured_snapshot():
        providers.insert(0, dict(id="LRCLIB local", availability="ready", next_available=None))
    return providers

def ready_categories(index, now):
    ready = set()
    available = ready_sources(index, now)
    for identity, row in available.items():
        if identity == "lyrics.ovh" and any(upstream not in available for upstream in LYRICS_BROKER_UPSTREAMS):
            continue
        if identity == "LyricDown":
            # This adapter needs an existing text. It cannot keep the
            # unrestricted LRC queue or unknown songs falsely eligible.
            ready.add("lrc_text")
        else:
            ready.update(row["category"].split(","))
        if identity == "MusicBrainz":
            ready.add("musicbrainz")
    return ready


def audio_policies(index, *, now=None):
    """Read the configured request budgets and observed provider signals."""
    now = time() if now is None else now
    with index.db(wait=.1) as db:
        rows = db.execute("""SELECT s.id, s.state, s.config, coalesce(u.used,0) AS used,
                                    p.until, p.status
                             FROM sources s
                             LEFT JOIN daily_usage u ON u.bucket='provider:' || s.id AND u.day=?
                             LEFT JOIN import_provider_limits p ON p.source=s.id AND p.until>?
                             WHERE s.id IN (?,?,?)""",
                          (date.fromtimestamp(now).isoformat(), now, *AUDIO_HOSTS)).fetchall()
    result = {}
    for row in rows:
        source = row["id"]
        daily, interval = provider_limits(json.loads(row["config"])) if source != YOUTUBE else (None, 0.)
        result[source] = dict(enabled=row["state"] == "active", daily=daily, used=row["used"],
                              interval=max(AUDIO_INTERVALS[AUDIO_HOSTS[source]], interval),
                              hold=max(0., (row["until"] or 0.) - now), status=row["status"])
    return result


def retry_delay(value):
    if value.isascii() and value.isdecimal() and len(value) < 12:
        return int(value)
    try:
        stamp = parsedate_to_datetime(value)
        if stamp.tzinfo is not None:
            return max(0., stamp.timestamp() - time())
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    return 120


def download_interval(index, identity, interval):
    try:
        _, configured = provider_policy(index, identity)
    except ProviderUnavailable as error:
        raise ImportIssue("network", str(error), stage=f"Téléchargement {identity}") from error
    return max(interval, configured)


def wait_provider(index, identity, host, interval, *, max_wait=5, validation=False, paced=False,
                  lyrics=False):
    daily, configured = provider_policy(index, identity, validation=validation)
    now = time()
    with index.db(wait=.15) as db:
        if not index.can_reserve(db, "provider:" + identity, date.fromtimestamp(now).isoformat(), now, daily, 0):
            raise ProviderUnavailable("Limite quotidienne du fournisseur atteinte")
        if db.execute("SELECT 1 FROM import_provider_limits WHERE source=? AND until>?", (identity, now)).fetchone():
            raise ProviderUnavailable("Fournisseur temporairement suspendu")
    if not paced:
        try:
            # Catalogue and audio share the host gate. Scheduled audio
            # already reserved its interval at global track admission.
            if lyrics:
                # Some catalogues use separate hosts for search and lyrics.
                # Their configured interval is shared across both hosts.
                wait_service(host if host == "lrclib.net" else "lyrics:" + identity,
                             max(interval, configured), max_wait=max_wait)
            else:
                wait_service(host, max(interval, configured), max_wait=max_wait)
        except ImportIssue as error:
            raise ProviderUnavailable("Temporisation fournisseur") from error
    if not validation and not index.source_enabled(identity):
        raise ProviderUnavailable("Fournisseur désactivé pendant la temporisation")
    if not index.reserve_daily("provider:" + identity, daily):
        raise ProviderUnavailable("Limite quotidienne du fournisseur atteinte")


def public_url(url):
    if not isinstance(url, str) or len(url) > 900 or any(c.isspace() or ord(c) < 32 for c in url):
        raise ValueError("URL de fournisseur invalide")
    parts = urlsplit(url)
    if (parts.scheme != "https" or not HOST.fullmatch(parts.netloc)
            or parts.hostname.endswith((".local", ".localhost", ".internal", ".onion", ".test", ".arpa"))
            or "\\" in url or parts.fragment):
        raise ValueError("Adresse HTTPS publique requise")
    if any(re.search(r"token|password|cookie|credential|authorization|api[-_]?key", name, re.I)
           for name in parse_qs(parts.query, keep_blank_values=True, max_num_fields=25)):
        raise ValueError("Paramètre d’authentification exclu de l’adresse")
    return parts


def external_ips(host):
    addresses = [entry[4] for entry in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)]
    if not addresses:
        raise ProviderUnavailable("Aucune adresse publique")
    for address in addresses:
        ip = ipaddress.ip_address(address[0])
        if not ip.is_global or ip.is_multicast or ip.is_reserved or getattr(ip, "ipv4_mapped", None):
            raise ValueError("Adresse réseau privée ou réservée")
    return addresses


class PinnedHTTPS(http.client.HTTPSConnection):
    def watch(self, stream):
        if (pause := current_pause()) is not None:
            pause.watch_socket(stream)
            self.import_socket = stream

    def forget(self):
        if (pause := current_pause()) is not None and hasattr(self, "import_socket"):
            pause.forget_socket(self.import_socket)
            del self.import_socket

    def connect(self):
        # Resolve once, validate every answer and connect to the literal
        # address. TLS still authenticates the original DNS name.
        address = external_ips(self.host)[0]
        with_tls = socket.socket(socket.AF_INET6 if len(address) == 4 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            self.watch(with_tls)
            with_tls.settimeout(self.timeout)
            with_tls.connect(address)
            self.forget()
            with_tls = self._context.wrap_socket(with_tls, server_hostname=self.host, do_handshake_on_connect=False)
            self.watch(with_tls)
            with_tls.do_handshake()
            self.sock = with_tls
        except BaseException:
            with_tls.close()
            raise


@contextmanager
def https_connection(host, timeout):
    connection = PinnedHTTPS(host, timeout=timeout, context=ssl.create_default_context())
    try:
        yield connection
    finally:
        connection.close()
        if isinstance(connection, PinnedHTTPS):
            connection.forget()


def fetch(url, *, interval=1.2, accept="application/json", registry=None, provider=None, validation=False, audit=None,
          user_agent="Parole/3 (https://github.com/labfytools/kara-Samsung)"):
    parts = public_url(url)
    event = {"type": "request", "url": url, "sent": False} if audit else None
    try:
        with request_slot(parts.hostname, 5):
            return fetch_response(parts, interval, accept, registry, provider, validation, event, user_agent)
    except ImportIssue as error:
        raise ProviderUnavailable("Créneau fournisseur indisponible") from error
    except (OSError, ValueError) as error:
        if event is not None:
            event["reason"] = str(error)
        raise
    finally:
        if event is not None:
            audit(event)


def fetch_response(parts, interval, accept, registry, provider, validation, event, user_agent):
    service = parts.hostname if registry is not None or parts.hostname == "lrclib.net" else "parole-" + parts.hostname
    if registry is not None:
        wait_provider(registry, provider, service, interval, validation=validation,
                      lyrics=provider == "Unison" or event is not None)
    else:
        try:
            wait_service(service, interval, max_wait=5)
        except ImportIssue as error:
            raise ProviderUnavailable("Temporisation fournisseur") from error
    deadline = monotonic() + 12
    with https_connection(parts.hostname, 5) as connection:
        try:
            checkpoint()
            path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            if event is not None:
                event["sent"] = True
            connection.request("GET", path, headers={
                "Accept": accept, "User-Agent": user_agent})
            response = connection.getresponse()
            if event is not None:
                event.update(http=response.status, retry_after=response.getheader("Retry-After"))
            if registry and provider:
                record_quota(registry, provider, getattr(response, "headers", {}))
            if response.status == 404:
                return None
            if response.status in (401, 403, 429, 451) or response.status >= 500:
                retry = response.getheader("Retry-After", "")
                denied = response.status in (401, 403, 451)
                delay = provider_backoff(registry, provider, service, max(300 if denied else 0, retry_delay(retry)),
                                         response.status, lookup_access=denied)
                defer_service(service, delay)
                raise ProviderUnavailable(f"Fournisseur HTTP {response.status}")
            if response.status != 200:
                raise ValueError(f"Réponse HTTP {response.status} refusée")
            if accept == "application/json" and "application/json" not in response.getheader("Content-Type", ""):
                raise ValueError("Type de réponse inattendu")
            data = bytearray()
            while True:
                checkpoint()
                if not (block := response.read1(32 * 1024)):
                    break
                data.extend(block)
                if len(data) > MAX_RESPONSE or monotonic() > deadline:
                    raise ProviderUnavailable("Réponse fournisseur hors limites")
            checkpoint()
            return json.loads(data) if accept == "application/json" else bytes(data)
        except ProviderUnavailable:
            raise
        except (OSError, http.client.HTTPException) as error:
            checkpoint()
            defer_service(service, provider_backoff(registry, provider, service, 5, 503))
            raise ProviderUnavailable("Source indisponible") from error


def provider_backoff(index, identity, host, minimum, status, *, lookup_access=False):
    if host == "lrclib.net" and status == 429:
        from random import uniform

        minimum += uniform(0, min(minimum * .3, 3))
    if index is None or not identity:
        return minimum
    delay = backoff(index, identity, minimum)
    limited(index, identity, delay, status, lookup_access=lookup_access)
    return delay


class AudioDownload:
    def __init__(self, payload, started, deadline, length, offset=0):
        self.payload = payload
        self.deadline = deadline
        self.length = length
        self.offset = offset
        self.transfer = Transfer(started, download_event)
        self.finished = False

    def read(self, size):
        checkpoint()
        if monotonic() >= self.deadline:
            raise ImportIssue("network", "Délai de téléchargement dépassé.")
        try:
            block = self.payload.read1(size)
        except (OSError, http.client.HTTPException) as error:
            checkpoint()
            raise ImportIssue("network", "Transfert audio interrompu.") from error
        checkpoint()
        if block:
            self.transfer.received(self.transfer.bytes + len(block), monotonic())
        else:
            self.finished = True
            self.transfer.end()
        return block


@contextmanager
def open_audio(url, *, registry, provider, service, hosts, expected_size=None, interval=1.2, offset=0, paced=False):
    # Rate and authorization are checked before contacting any storage node.
    # Redirects must stay in the adapter's allowed domains and keep TLS.
    try:
        wait_provider(registry, provider, service, interval, paced=paced)
    except ProviderUnavailable as error:
        raise ImportIssue("network", str(error), stage=f"Téléchargement {provider}") from error
    started = monotonic()
    deadline = started + 360
    for _ in range(4):
        checkpoint()
        parts = public_url(url)
        if not any(parts.hostname == host or parts.hostname.endswith("." + host) for host in hosts):
            raise ImportIssue("path", f"Redirection hors de {provider} refusée.")
        with https_connection(parts.hostname, 12) as connection:
            request = (parts.path or "/") + ("?" + parts.query if parts.query else "")
            try:
                headers = {"User-Agent": "Parole/3 (Kara music library)"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                connection.request("GET", request, headers=headers)
                response = connection.getresponse()
            except (OSError, http.client.HTTPException) as error:
                checkpoint()
                raise ImportIssue("network", f"Transfert {provider} indisponible.") from error
            checkpoint()
            if response.status in {301, 302, 303, 307, 308} and response.getheader("Location"):
                url = urljoin(url, response.getheader("Location"))
                continue
            if response.status == 429 or response.status >= 500:
                retry = response.getheader("Retry-After", "")
                delay = retry_delay(retry)
                defer_service(service, delay)
                limited(registry, provider, delay, response.status)
                raise ImportIssue("network", f"{provider} HTTP {response.status}")
            if response.status == 206 and offset and expected_size is not None:
                if response.getheader("Content-Range") != f"bytes {offset}-{expected_size - 1}/{expected_size}":
                    raise ImportIssue("processing", f"Reprise partielle {provider} non vérifiable.")
            elif response.status == 200:
                offset = 0
            else:
                raise ImportIssue("path", f"Fichier {provider} indisponible (HTTP {response.status}).")
            raw_length = response.getheader("Content-Length")
            length = int(raw_length) if raw_length is not None and re.fullmatch(r"[0-9]{1,10}", raw_length) else None
            if raw_length is not None and (length is None or expected_size is not None and length != expected_size - offset):
                raise ImportIssue("processing", f"La longueur du fichier {provider} a changé.")
            media = response.getheader("Content-Type", "").split(";", 1)[0].casefold()
            if media and not (media.startswith("audio/") or media in {"application/octet-stream", "application/ogg"}):
                raise ImportIssue("processing", f"La réponse {provider} n’est pas un fichier audio.")
            stream = AudioDownload(response, started, deadline, length, offset)
            try:
                yield stream
            finally:
                stream.transfer.end()
            if stream.finished:
                observe(registry, provider, stream.transfer.measurements())
            return
    raise ImportIssue("network", f"Trop de redirections {provider}.")


def seed(index):
    with index.db() as db:
        db.executemany("""INSERT OR IGNORE INTO sources(id,name,category,state,adapter,config,discovered)
                       VALUES(?,?,?,'active','builtin',?,?)""",
                       ((name, name, kinds, document({"host": host, "docs": docs}), time())
                        for name, (host, kinds, docs) in BUILTINS.items()))
        # Upgrade capability without changing an operator's state or quota.
        db.execute("""UPDATE sources SET category=? WHERE id=? AND adapter='builtin'
                      AND category='lyrics_candidate'""", ("lyrics_candidate,lrc", UNISON))
        db.executemany("""INSERT OR IGNORE INTO sources(id,name,category,state,adapter,config,discovered)
                       VALUES(?,?,'import','active','builtin',?,?)""",
                       ((name, name, document({"host": route.host, "docs": route.help, "transport": "local"}), time())
                        for name, route in MANUAL.items()))


def discover(index, source, research_id):
    parts = public_url(source["url"])
    identity = "host:" + parts.hostname
    with index.db() as db:
        for row in db.execute("SELECT id,config FROM sources"):
            if json.loads(row["config"]).get("host") == parts.hostname:
                return row["id"], False
        config = dict(host=parts.hostname, url=source["url"], docs=source["documentation"],
                      terms=source["terms"], discovery=research_id)
        cursor = db.execute("""INSERT OR IGNORE INTO sources(id,name,category,state,adapter,config,discovered)
                              VALUES(?,?,?,'pending',?,?,?)""",
                            (identity, parts.hostname, source["category"], source["adapter"], document(config), time()))
        if cursor.rowcount:
            db.execute("INSERT INTO source_history VALUES(?,?,?)", (time(), identity, "discovered"))
        return identity, bool(cursor.rowcount)


def record(index, identity, result, seconds, *, error=""):
    with index.db() as db:
        now = time()
        changed = db.execute("""UPDATE sources SET attempts=attempts+1, successes=successes+?, failures=failures+?,
                      used=?, latency=(latency*attempts+?)/(attempts+1),
                      error=CASE WHEN ? THEN ? ELSE error END WHERE id=?""",
                   (result == "found", result == "error", now, seconds, result == "error",
                    error[:200] or "Indisponible", identity)).rowcount
        if changed and result in SOURCE_OUTCOMES and isfinite(seconds) and 0 <= seconds <= 600:
            row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?",
                             (RECENT_SOURCE_KIND, identity)).fetchone()
            try:
                samples = json.loads(row["value"]) if row else []
            except ValueError:
                samples = []
            if not isinstance(samples, list):
                samples = []
            # Keep only durations and decisions, in the same transaction as
            # the provider totals. No query, title or response is persisted.
            samples = [*samples[-(RECENT_SOURCE_LIMIT - 1):], [round(seconds * 1000), result]]
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                       (RECENT_SOURCE_KIND, identity, "observed", now, document(samples)))


def recent_source(index, identity):
    from collections import Counter

    with index.db(wait=.15) as db:
        row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?",
                         (RECENT_SOURCE_KIND, identity)).fetchone()
    if not row:
        return None
    samples = json.loads(row["value"])
    if not isinstance(samples, list):
        return None
    samples = [(ms, result) for entry in samples if isinstance(entry, list) and len(entry) == 2
               for ms, result in (entry,) if type(ms) is int and 0 <= ms <= 600_000
               and isinstance(result, str) and result in SOURCE_OUTCOMES]
    if not samples:
        return None
    times = sorted(ms for ms, _ in samples)
    return {"mesures": len(times), "moyenne_ms": round(sum(times) / len(times)),
            "p95_ms": times[ceil(.95 * len(times)) - 1] if len(times) >= 20 else None,
            "résultats": dict(Counter(result for _, result in samples))}


def controls(index, identity, action):
    with index.db() as db:
        source = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
        if not source:
            return
        config = json.loads(source["config"])
        if action == "reset":
            db.execute("UPDATE sources SET attempts=0,successes=0,failures=0,latency=0,error='' WHERE id=?", (identity,))
            db.execute("DELETE FROM import_provider_metrics WHERE source=?", (identity,))
            db.execute("DELETE FROM reviews WHERE kind IN (?,?) AND subject=?",
                       (RECENT_SOURCE_KIND, LYRICS_STATS, identity))
        elif action in ("excluded", "disabled") or (
                action == "active" and source["state"] != "excluded"
                and (source["adapter"] == "builtin" or config.get("validated"))):
            db.execute("UPDATE sources SET state=? WHERE id=?", (action, identity))
            if action == "active" and source["state"] != "active":
                # Retry gradually through the same bounded scheduler.
                db.execute("""UPDATE tasks SET state='queued', due=?
                    WHERE state='review' AND (
                         kind IN ('lyrics','lrc') AND detail='not_found'
                         OR kind='prepare' AND detail='référence fiable absente')""", (time(),))
        else:
            return
        db.execute("INSERT INTO source_history VALUES(?,?,?)", (time(), identity, action))


@dataclass(frozen=True)
class LyricsEndpoint:
    host: str
    path: str

    def __post_init__(self):
        public_url(f"https://{self.host}")
        if not PATH.fullmatch(self.path) or "//" in self.path:
            raise ValueError("Point d’accès LRCLIB invalide")

    def search(self, title, artist, album, interval=1.2, **transport):
        query = urlencode({"track_name": title, "artist_name": artist, "album_name": album or ""})
        result = fetch(f"https://{self.host}{self.path}?{query}", interval=interval, **transport)
        if result is None:
            return []
        if not isinstance(result, list) or len(result) > 100:
            raise ValueError("Contrat LRCLIB non respecté")
        return result


def validate_endpoint(index, identity, endpoint, terms, fixtures, match, *, activate=True):
    # The configuration must be backed by approved automation terms and by
    # several expected results. Network content and Codex output cannot grant
    # approval by themselves. Hashes are over verified lyrics from fixtures.
    if not isinstance(terms, str) or public_url(terms).hostname != endpoint.host:
        raise ValueError("Conditions d’utilisation du domaine requises")
    if len(fixtures) < 3 or len(fixtures) > 8 or sum(f["sha256"] is None for f in fixtures) < 1 or sum(
            f["sha256"] is not None for f in fixtures) < 2:
        raise ValueError("Deux résultats positifs et un résultat négatif sont requis")
    if fetch(terms, accept="text/html", registry=index, provider=identity, validation=True) is None:
        raise ValueError("Conditions d’utilisation introuvables")
    for fixture in fixtures:
        title, artist, album, duration = fixture["title"], fixture["artist"], fixture["album"], fixture["duration"]
        value = match(endpoint.search(title, artist, album, registry=index, provider=identity, validation=True),
                      title, artist, album,
                      duration, fixture.get("album_artist"), synced=fixture["synced"])
        hashed = sha256(value.encode()).hexdigest() if value else None
        if hashed != fixture["sha256"]:
            raise ValueError("Résultat de vérification indépendant divergent")
    with index.db() as db:
        row = db.execute("SELECT config,adapter,state FROM sources WHERE id=?", (identity,)).fetchone()
        if not row or row["adapter"] != "lrclib" or row["state"] == "excluded":
            raise ValueError("Source absente, exclue ou adaptateur non approuvé")
        config = json.loads(row["config"])
        if config["host"] != endpoint.host:
            raise ValueError("Domaine différent de la source enregistrée")
        config.update(path=endpoint.path, terms=terms, validated=True, fixtures=fixtures)
        db.execute("UPDATE sources SET config=?,checked=?,error='' WHERE id=?", (document(config), time(), identity))
        db.execute("INSERT INTO source_history VALUES(?,?,?)", (time(), identity, "validated"))
    if activate:
        controls(index, identity, "active")
