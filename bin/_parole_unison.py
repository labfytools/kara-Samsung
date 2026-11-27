"""Découverte Unison par identifiant d’enregistrement, avec attribution."""

import json
import re
from time import time
from urllib.parse import urlencode

from _kara_import_result import ImportIssue
from _parole_lyrics_lookup import lookup_slot
from _parole_index import encoded_key
from _parole_sources import ProviderUnavailable, fetch, record

SOURCE = "Unison"
BASE = "https://unison.betterlyrics.org"
ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")


def isrc(value):
    value = value.replace("-", "").strip().upper() if isinstance(value, str) else ""
    return value if ISRC.fullmatch(value) else ""


def inspect(payload, expected, matches):
    if not isinstance(payload, dict) or payload.get("success") is not True:
        return None
    data = payload.get("data")
    if (not isinstance(data, dict) or isrc(data.get("isrc")) != expected
            or data.get("matchedBy", "isrc") != "isrc" or not matches(data)
            or data.get("format") not in {"lrc", "plain", "ttml"}
            or not isinstance(data.get("lyrics"), str) or not data["lyrics"].strip()):
        return None
    video = data.get("videoId")
    submitter = data.get("submitter")
    if not isinstance(submitter, dict) or not isinstance(video, str) or not re.fullmatch(r"[A-Za-z0-9_-]{11}", video):
        return None
    name, profile = submitter.get("displayName"), submitter.get("keyId")
    if (not isinstance(name, str) or not 1 <= len(name) <= 100 or not name.isprintable()
            or not isinstance(profile, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", profile)):
        return None
    # Keep the attribution and destination only. Neither the lyrics nor
    # their timestamps become an offline copy via this discovery route.
    return {"url": f"{BASE}/song/{video}", "contributor": name, "profile": f"{BASE}/curator/{profile}",
            "format": data["format"]}


def discover(index, song, value, matches):
    code = isrc(value)
    if not code or not index.source_enabled(SOURCE):
        return None
    try:
        with lookup_slot(index, encoded_key(SOURCE, code)):
            return discover_locked(index, song, code, matches)
    except ImportIssue:
        return None


def discover_locked(index, song, code, matches):
    with index.db(wait=.15) as db:
        row = db.execute("""SELECT checked,value FROM reviews
                            WHERE kind='unison_candidate' AND subject=?""", (song["id"],)).fetchone()
    if row and time() - row["checked"] < 86400:
        saved = json.loads(row["value"])
        if saved["token"] == song["token"] and saved["isrc"] == code:
            return saved["candidate"]
    result, error = probe(index, code, matches)
    if not error:
        index.review("unison_candidate", song["id"], "found" if result else "absent",
                     {"token": song["token"], "isrc": code, "candidate": result})
    return result


def probe(index, code, matches):
    began = time()
    try:
        payload = fetch(f"{BASE}/lyrics?{urlencode({'isrc': code})}", registry=index, provider=SOURCE)
    except (ProviderUnavailable, ValueError) as error:
        record(index, SOURCE, "error", time() - began, error=str(error))
        return None, str(error)
    result = inspect(payload, code, matches)
    record(index, SOURCE, "found" if result else "absent", time() - began)
    return result, None
