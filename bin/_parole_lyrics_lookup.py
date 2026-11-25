"""Transport et cache bornés des réponses de paroles déjà vérifiées."""
from collections import OrderedDict
from contextlib import closing
from http.client import HTTPException, HTTPSConnection
from io import BytesIO
import json
import sqlite3
from time import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import OpenerDirector, getproxies

from _parole_index import document, encoded_key

MAX_AGE = 7 * 86400
MAX_ABSENCE_AGE = 86400
MAX_ROWS = 2048
CACHE_KIND = "lyrics_query_v1"


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
    except sqlite3.Error:
        pass
