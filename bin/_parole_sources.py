"""Registre des fournisseurs et transport HTTPS destiné aux configurations."""
from dataclasses import dataclass
from hashlib import sha256
import http.client
import ipaddress
import json
import re
import socket
import ssl
from time import monotonic, time
from urllib.parse import parse_qs, urlencode, urlsplit

from _kara_import_resources import defer_service, wait_service
from _kara_import_result import ImportIssue
from _parole_index import document

BUILTINS = {
    "LRCLIB": ("lrclib.net", "lyrics,lrc", "https://lrclib.net/docs"),
    "lyrics.ovh": ("api.lyrics.ovh", "lyrics", "https://lyricsovh.docs.apiary.io/"),
    "NetEase": ("music.163.com", "lyrics,lrc", ""),
    "QQ Music": ("y.qq.com", "lyrics,lrc", ""),
    "Genius": ("genius.com", "lyrics", ""),
    "LyricDown": ("lyricdown.com", "lrc", ""),
    "YouTube Music": ("music.youtube.com", "catalogue,cover,import", ""),
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
COVER_PROVIDERS = {host: identity for identity, (host, category, _) in BUILTINS.items()
                   if "cover" in category or identity == "MusicBrainz"}


class ProviderUnavailable(OSError):
    pass


def provider_limits(config):
    limits = config.get("rate", {})
    daily, seconds = limits.get("daily", DEFAULT_REQUESTS), limits.get("seconds", 0)
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


def wait_provider(index, identity, host, interval, *, max_wait=5, validation=False):
    with index.db() as db:
        row = db.execute("SELECT state,config FROM sources WHERE id=?", (identity,)).fetchone()
    if not row or (row["state"] != "active" and not (validation and row["state"] != "excluded")):
        raise ProviderUnavailable("Fournisseur désactivé")
    daily, configured = provider_limits(json.loads(row["config"]))
    if not daily:
        raise ProviderUnavailable("Limite quotidienne du fournisseur atteinte")
    try:
        # The host gate already coordinates separate clients and import
        # accesses. Settings can raise each protocol's minimum interval.
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
    def connect(self):
        # Resolve once, validate every answer and connect to the literal
        # address. TLS still authenticates the original DNS name.
        address = external_ips(self.host)[0]
        with_tls = socket.socket(socket.AF_INET6 if len(address) == 4 else socket.AF_INET, socket.SOCK_STREAM)
        try:
            with_tls.settimeout(self.timeout)
            with_tls.connect(address)
            self.sock = self._context.wrap_socket(with_tls, server_hostname=self.host)
        except BaseException:
            with_tls.close()
            raise


def fetch(url, *, interval=1.2, accept="application/json", registry=None, provider=None, validation=False):
    parts = public_url(url)
    service = parts.hostname if registry is not None else "parole-" + parts.hostname
    if registry is not None:
        wait_provider(registry, provider, service, interval, validation=validation)
    else:
        try:
            wait_service(service, interval, max_wait=5)
        except ImportIssue as error:
            raise ProviderUnavailable("Temporisation fournisseur") from error
    connection = PinnedHTTPS(parts.hostname, timeout=5, context=ssl.create_default_context())
    deadline = monotonic() + 12
    try:
        path = (parts.path or "/") + ("?" + parts.query if parts.query else "")
        connection.request("GET", path, headers={"Accept": accept, "User-Agent": "Parole/3 (Kara music library)"})
        response = connection.getresponse()
        if response.status == 404:
            return None
        if response.status == 429 or response.status >= 500:
            retry = response.getheader("Retry-After", "")
            defer_service(service, min(int(retry), 86400) if retry.isdecimal() and len(retry) < 8 else 120)
            raise ProviderUnavailable(f"Fournisseur HTTP {response.status}")
        if response.status != 200:
            raise ValueError(f"Réponse HTTP {response.status} refusée")
        if accept == "application/json" and "application/json" not in response.getheader("Content-Type", ""):
            raise ValueError("Type de réponse inattendu")
        data = bytearray()
        while block := response.read1(32 * 1024):
            data.extend(block)
            if len(data) > MAX_RESPONSE or monotonic() > deadline:
                raise ProviderUnavailable("Réponse fournisseur hors limites")
        return json.loads(data) if accept == "application/json" else bytes(data)
    except (OSError, http.client.HTTPException) as error:
        raise ProviderUnavailable("Source indisponible") from error
    finally:
        connection.close()


def seed(index):
    with index.db() as db:
        db.executemany("""INSERT OR IGNORE INTO sources(id,name,category,state,adapter,config,discovered)
                       VALUES(?,?,?,'active','builtin',?,?)""",
                       ((name, name, kinds, document({"host": host, "docs": docs}), time())
                        for name, (host, kinds, docs) in BUILTINS.items()))


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


def record(index, identity, result, seconds):
    with index.db() as db:
        db.execute("""UPDATE sources SET attempts=attempts+1, successes=successes+?, failures=failures+?,
                      used=?, latency=(latency*attempts+?)/(attempts+1), error=? WHERE id=?""",
                   (result == "found", result == "error", time(), seconds, "Indisponible" if result == "error" else "", identity))


def controls(index, identity, action):
    with index.db() as db:
        source = db.execute("SELECT * FROM sources WHERE id=?", (identity,)).fetchone()
        if not source:
            return
        config = json.loads(source["config"])
        if action == "reset":
            db.execute("UPDATE sources SET attempts=0,successes=0,failures=0,latency=0,error='' WHERE id=?", (identity,))
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
