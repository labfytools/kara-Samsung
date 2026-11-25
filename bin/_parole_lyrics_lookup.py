"""Transport et cache bornés des réponses de paroles déjà vérifiées."""
from collections import OrderedDict
from contextlib import closing
from http.client import HTTPException, HTTPSConnection
from io import BytesIO
import json
import re
import sqlite3
from time import time
from unicodedata import normalize
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, getproxies

from _parole_index import document, encoded_key

MAX_AGE = 7 * 86400
MAX_ABSENCE_AGE = 86400
MAX_ROWS = 2048
CACHE_KIND = "lyrics_query_v1"
PROFILE_KIND = "lyrics_source_profile"
OUTAGE_KIND = "lyrics_source_outage"


def contexts(tags):
    language = (tags.get("language") or "").strip().casefold()
    genre = normalize("NFKC", tags.get("genre") or "").strip().casefold()
    return tuple(filter(None, (
        ("language", language) if re.fullmatch(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})?", language) else None,
        ("genre", genre) if genre and len(genre) <= 80 and genre.isprintable() else None,
    )))


def profile_key(provider, synced, context):
    return encoded_key(provider, bool(synced), context)


def provider_profiles(index, providers, synced, context):
    keys = {profile_key(name, synced, item): name for name in providers for item in context}
    if not keys:
        return {}
    try:
        with index.db(wait=.2) as db:
            marks = ",".join("?" for _ in keys)
            rows = db.execute(f"""SELECT subject,value FROM reviews WHERE kind=? AND checked>?
                               AND subject IN ({marks})""", (PROFILE_KIND, time() - 30 * 86400, *keys)).fetchall()
        result = {}
        for row in rows:
            sample = json.loads(row["value"])
            if sample["observations"] >= 5:
                result.setdefault(keys[row["subject"]], []).append(sample)
        return result
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        return {}


def record_profile(index, provider, synced, context, seconds, verified):
    if not context:
        return
    try:
        with index.db(wait=.2) as db:
            db.execute("BEGIN IMMEDIATE")
            for item in context:
                identity = profile_key(provider, synced, item)
                row = db.execute("SELECT checked,value FROM reviews WHERE kind=? AND subject=?",
                                 (PROFILE_KIND, identity)).fetchone()
                value = json.loads(row["value"]) if row and time() - row["checked"] < 30 * 86400 else None
                if value is None:
                    value = {"seconds": seconds, "rate": int(verified), "observations": 1}
                else:
                    value = {"seconds": .8 * value["seconds"] + .2 * seconds,
                             "rate": .8 * value["rate"] + .2 * int(verified),
                             "observations": value["observations"] + 1}
                db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                           (PROFILE_KIND, identity, "observed", time(), document(value)))
    except (sqlite3.Error, ValueError, TypeError, KeyError):
        pass


def backoff(index, provider, minimum):
    # Episodes counted here are actual HTTP errors. Expire the previous
    # episode so an isolated outage does not penalize a provider indefinitely.
    now = time()
    try:
        with index.db(wait=.2) as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT checked,value FROM reviews WHERE kind=? AND subject=?",
                               (OUTAGE_KIND, provider)).fetchone()
            level = min(6, int(prior["value"]) + 1) if prior and now - prior["checked"] < 900 else 1
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                       (OUTAGE_KIND, provider, "limited", now, document(level)))
        return max(minimum, 5 * 2**(level - 1))
    except (sqlite3.Error, ValueError, TypeError):
        return minimum


class KeepAlive:
    def __init__(self, opener, maximum):
        # Preserve environment proxy handling and custom urllib transports.
        self.fallback = opener if not isinstance(opener, OpenerDirector) or getproxies().get("https") else None
        self.maximum = maximum
        self.connections = OrderedDict()

    def open(self, request, *, timeout):
        if self.fallback:
            return self.fallback.open(request, timeout=timeout)
        url = urlsplit(request.full_url)
        if url.scheme != "https" or url.port not in (None, 443) or url.username or not url.hostname:
            raise URLError("URL de paroles invalide")
        host = url.hostname
        connection = self.connections.pop(host, None)
        if connection is None:
            connection = HTTPSConnection(host, timeout=timeout)
        if len(self.connections) >= 8:
            self.connections.popitem(last=False)[1].close()
        connection.timeout = timeout
        if connection.sock:
            connection.sock.settimeout(timeout)
        try:
            path = url.path + ("?" + url.query if url.query else "")
            connection.request("GET", path, headers=dict(request.header_items()))
            with closing(connection.getresponse()) as response:
                if response.status != 200:
                    raise HTTPError(request.full_url, response.status, response.reason, response.headers, None)
                raw = response.read(self.maximum + 1)
                result = BytesIO(raw)
                result.headers = response.headers
                # Only a complete response is safe to reuse on this connection.
                if len(raw) <= self.maximum and not response.will_close:
                    self.connections[host] = connection
                else:
                    connection.close()
                return result
        except HTTPError:
            connection.close()
            raise
        except (HTTPException, OSError) as error:
            connection.close()
            raise URLError(error) from error
        except BaseException:
            connection.close()
            raise

    def close(self):
        for connection in self.connections.values():
            connection.close()
        self.connections.clear()


def cache_identity(provider, signature, synced, local_text, strict, configuration):
    # Keep the exact version/credits in the key. LyricDown aligns an existing
    # text, and its timing is only reusable with the same reference text.
    return encoded_key(provider, signature, synced, encoded_key(local_text) if synced else "", strict, configuration)


def cached(index, identity):
    try:
        with index.db(wait=.2) as db:
            row = db.execute("SELECT checked,state,value FROM reviews WHERE kind=? AND subject=?",
                             (CACHE_KIND, identity)).fetchone()
        if row and time() - row["checked"] < (MAX_ABSENCE_AGE if row["state"] == "absent" else MAX_AGE):
            value = json.loads(row["value"])
            if value is None or isinstance(value, str):
                return True, value
    except (sqlite3.Error, ValueError):
        pass
    return False, None


def remember(index, identity, value):
    try:
        with index.db(wait=.2) as db:
            db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                       (CACHE_KIND, identity, "absent" if value is None else "verified", time(), document(value)))
    except sqlite3.Error:
        pass


def prune(index):
    try:
        with index.db(wait=.2) as db:
            db.execute("DELETE FROM reviews WHERE kind=? AND checked<?", (CACHE_KIND, time() - MAX_AGE))
            db.execute("""DELETE FROM reviews WHERE rowid IN (SELECT rowid FROM reviews WHERE kind=?
                          ORDER BY checked DESC LIMIT -1 OFFSET ?)""", (CACHE_KIND, MAX_ROWS))
            db.execute("DELETE FROM reviews WHERE kind=? AND checked<?", (PROFILE_KIND, time() - 30 * 86400))
            db.execute("""DELETE FROM reviews WHERE rowid IN (SELECT rowid FROM reviews WHERE kind=?
                          ORDER BY checked DESC LIMIT -1 OFFSET ?)""", (PROFILE_KIND, MAX_ROWS))
    except sqlite3.Error:
        pass
