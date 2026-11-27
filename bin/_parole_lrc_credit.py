"""Crédits associés au contenu exact d’un LRC, indépendamment de son chemin."""

import json
import os
import re
import stat
from time import time

from _kara_paths import CONFIG
from _parole_index import document

RED = "lrc.red"
UNISON = "Unison"
CREDITED = frozenset({RED, UNISON})
KIND = "lrc_attribution"
LINKS = (
    ("Paroles de lrc.red", r"https://lrc\.red/s/[A-Z]{2}[A-Z0-9]{3}[0-9]{7}"),
    ("Paroles d’Unison", r"https://unison\.betterlyrics\.org/song/[A-Za-z0-9_-]{11}"),
    ("", r"https://unison\.betterlyrics\.org/curator/[A-Za-z0-9_-]{1,128}"),
)


def red_contact():
    contact = os.environ.get("KARA_LRC_RED_CONTACT")
    if contact is None:
        # The service and terminal tools share this local setting. Read at
        # most one address and do not accept symlinks, pipes or public files.
        try:
            path = CONFIG / "kara/lrc-red-contact"
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), "rb") as handle:
                info = os.fstat(handle.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
                    return ""
                contact = handle.read(257).decode("ascii") if info.st_size <= 256 else ""
        except (OSError, UnicodeError):
            return ""
    contact = contact.strip()
    return contact if re.fullmatch(r"[A-Za-z0-9._+%-]{1,64}@[A-Za-z0-9.-]{1,190}\.[A-Za-z]{2,24}", contact) else ""


def checked_links(links):
    if not isinstance(links, list):
        return []
    valid = {}
    for link in links:
        if not isinstance(link, dict):
            continue
        url, text = link.get("url"), link.get("label")
        if not isinstance(url, str) or not isinstance(text, str) or not 1 <= len(text) <= 100 or not text.isprintable():
            continue
        for i, (label, pattern) in enumerate(LINKS):
            if (not label or text == label) and re.fullmatch(pattern, url):
                valid[i] = {"url": url, "label": text}
                break
    return ([valid[0]] if 0 in valid else []) + ([valid[1], valid[2]] if {1, 2} <= valid.keys() else [])


def load_links(index, digest):
    with index.db(wait=.06) as db:
        row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?", (KIND, digest)).fetchone()
    return checked_links(json.loads(row[0])) if row else []


def save_links(index, digest, source, links):
    links = checked_links(links)
    if (source == RED and not any(link["label"] == LINKS[0][0] for link in links)
            or source == UNISON and not any(link["label"] == LINKS[1][0] for link in links)):
        raise ValueError("Crédit du fournisseur de LRC incomplet")
    # Commit provenance before publishing the unchanged sidecar. Leases and
    # retries may reuse the same content, so preserve the first credit from
    # each catalogue even when both served the same bytes.
    with index.db() as db:
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM reviews WHERE kind=? AND subject=?", (KIND, digest)).fetchone()
        existing = checked_links(json.loads(row[0])) if row else []
        db.execute("INSERT OR REPLACE INTO reviews VALUES(?,?,?,?,?)",
                   (KIND, digest, "observed", time(), document(checked_links(links + existing))))
