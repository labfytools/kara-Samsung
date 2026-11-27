"""Récupère les LRC natifs et leurs preuves d’édition."""

import re
from hashlib import sha1
import json
from pathlib import PurePosixPath
from time import time
from urllib.parse import quote, urlencode

from _kara_import_resources import resource_slot
from _parole_lrc_credit import RED, UNISON, LINKS, red_contact
from _parole_sources import COMMUNITY_LRC, KUGOU, ProviderUnavailable, fetch
from _parole_unison import BASE as UNISON_BASE, inspect, isrc
from _paroles_lrc import MAX_LRC_BYTES

RED_BASE = "https://lrc.red"
USER_AGENT = "Parole/3 (https://github.com/labfytools/kara-Samsung; contact: {})"
KUGOU_URL = "https://api.betterlyrics.org/kugou/getLyrics"
LRC_TAGS = re.compile(r"^[\ufeff \t]*\[(ti|ar|al|length|isrc):([^\[\]\r\n]+)\][ \t]*\r?$",
                      re.IGNORECASE | re.MULTILINE)


def lrc_evidence(value):
    tags = {}
    for name, content in LRC_TAGS.findall(value):
        name, content = name.lower(), content.strip()
        if name in tags and tags[name] != content:
            return None
        tags[name] = content
    length = re.fullmatch(r"([0-9]{1,2}):([0-5][0-9](?:\.[0-9]{1,3})?)", tags.get("length", ""))
    if tags.get("length") and not length:
        return None
    native = {"trackName": tags.get("ti"), "artistName": tags.get("ar"), "albumName": tags.get("al"),
              "duration": int(length[1]) * 60 + float(length[2]) if length else None, "isrc": tags.get("isrc")}
    return {key: value for key, value in native.items() if value}


def kugou_lookup(signature, registry, audit, *, score, validate):
    title, artist, album, duration, _ = signature
    query = urlencode({"s": title, "a": artist, "al": album or "", "d": round(duration)})
    payload = fetch(f"{KUGOU_URL}?{query}", registry=registry, provider=KUGOU, audit=audit)
    if payload is None:
        return None
    if not isinstance(payload, dict) or payload.get("provider") != "kugou" or not isinstance(payload.get("lyrics"), str):
        raise ProviderUnavailable("Réponse KuGou illisible")
    value = validate(payload.get("lyrics"))
    if not value or (evidence := lrc_evidence(value)) is None:
        return None
    # Filled values may rule out a contradiction for human review, but
    # echoed search terms never count as evidence for automatic publication.
    expected = {"trackName": title, "artistName": artist, "albumName": album, "duration": duration}
    if score(expected | evidence) is None:
        return None
    return evidence, value, bool(expected.keys() <= evidence.keys())


def community_tree(source, registry, audit):
    repository, branch = COMMUNITY_LRC[source]
    # Reload once per day across desktop searches and nightly workers. The
    # directory contains names and hashes only, so catalogue misses stay local.
    with resource_slot(registry.folder / "lrc-catalogues" / repository.split("/")[0], 1, timeout=15):
        with registry.db(wait=.2) as db:
            row = db.execute("SELECT checked,value FROM reviews WHERE kind='community_lrc_tree' AND subject=?",
                             (repository,)).fetchone()
        if row and time() - row["checked"] < 86400:
            return json.loads(row["value"])
        url = f"https://api.github.com/repos/{repository}/git/trees/{branch}?recursive=1"
        payload = fetch(url, registry=registry, provider=source, audit=audit)
        tree = payload.get("tree") if isinstance(payload, dict) and payload.get("truncated") is False else None
        if not isinstance(tree, list) or len(tree) > 4000:
            raise ProviderUnavailable("Répertoire de LRC indisponible")
        files = {}
        for item in tree:
            if not isinstance(item, dict) or item.get("mode") != "100644" or item.get("type") != "blob":
                continue
            path, blob, size = item.get("path"), item.get("sha"), item.get("size")
            if (not isinstance(path, str) or not path.lower().endswith(".lrc")
                    or any(part in ("", ".", "..") for part in path.split("/")) or len(quote(path)) > 620
                    or type(size) is not int or not 0 < size <= MAX_LRC_BYTES
                    or not isinstance(blob, str) or not re.fullmatch(r"[a-f0-9]{40}", blob)):
                continue
            files[path] = blob
        registry.review("community_lrc_tree", repository, "observed", files)
        return files


def community_lookup(source, signature, registry, audit, *, score, validate, title_key):
    repository, branch = COMMUNITY_LRC[source]
    files = community_tree(source, registry, audit)
    matching = []
    for path in sorted(files):
        stem = PurePosixPath(path).stem
        if stem.endswith(".rom") or not stem.endswith(".jp") and path[:-4] + ".jp.lrc" in files:
            continue
        stem = stem.removesuffix(".jp")
        stem = re.sub(r"^(?:\[\d{1,3}\]|\d{1,3}[. -])\s*", "", stem)
        if source == "Myaanee":
            stem = stem.partition(" - ")[2]
        if title_key(stem) == title_key(signature[0]):
            matching.append(path)
    if not 0 < len(matching) <= 4:
        return None
    found = {}
    for path in matching:
        url = f"https://raw.githubusercontent.com/{repository}/{branch}/{quote(path)}"
        raw = fetch(url, registry=registry, provider=source, accept="text/plain", audit=audit)
        if raw is None:
            continue
        # Same bytes as the blob indexed above, even if the branch advanced.
        if sha1(f"blob {len(raw)}\0".encode() + raw, usedforsecurity=False).hexdigest() != files[path]:
            raise ProviderUnavailable("Le dépôt a changé depuis son indexation")
        value = validate(raw.decode("utf-8"))
        if value and (evidence := lrc_evidence(value)) and score(evidence) is not None:
            found[value] = dict(evidence, id=files[path])
    if len(found) == 1:
        value, evidence = next(iter(found.items()))
        return evidence, value
    return None


def lookup(source, signature, code, registry, audit, *, score, validate, search_text):
    def request(uri, **options):
        return fetch(uri, registry=registry, provider=source, audit=audit, **options)

    if source == RED:
        contact = red_contact()
        if not contact:
            return None

        def red_request(path, **options):
            return request(RED_BASE + path, user_agent=USER_AGENT.format(contact), **options)

        return red_lookup(red_request, code, score, validate, search_text)
    if source == UNISON and code:
        return unison_lookup(request, signature, code, score, validate)
    return None


def red_record(item):
    return dict(trackName=item.get("title"), artistName=item.get("artist"), albumName=item.get("album"),
                duration=item.get("duration"), isrc=item.get("isrc"), id=item.get("isrc"))


def red_lookup(request, code, score, validate, query):
    if not code:
        page = request("/search.json?" + urlencode({"q": query}))
        if page is None:
            return None
        if not isinstance(page, dict) or not isinstance(page.get("hits"), list):
            raise ProviderUnavailable("Catalogue lrc.red illisible")
        # A title search can return multiple edits. Only a single verified
        # recording can lead to a download, independent of hit order.
        matches = {isrc(item.get("isrc")) for item in page["hits"]
                   if isinstance(item, dict) and score(red_record(item)) is not None}
        matches.discard("")
        if len(matches) != 1:
            return None
        code = matches.pop()
    path = "/s/" + code
    item = request(path + ".json")
    if not isinstance(item, dict) or isrc(item.get("isrc")) != code:
        return None
    record = red_record(item)
    files = item.get("files")
    if score(record) is None or not isinstance(files, dict) or not files.get("lrc"):
        return None
    # Never follow file URLs supplied by the catalogue. The verified ISRC
    # identifies the file on the same origin that received the contact.
    raw = request(path + ".lrc", accept="text/plain")
    if raw is None:
        return None
    value = validate(raw.decode("utf-8"))
    return (record, value, [{"url": RED_BASE + path, "label": LINKS[0][0]}]) if value else None


def unison_lookup(request, signature, code, score, validate):
    def record(item):
        # Unison may omit duration on an exact ISRC response. The ISRC and
        # edition must still match and all native timestamps fit the track.
        return dict(trackName=item.get("song"), artistName=item.get("artist"), albumName=item.get("album"),
                    duration=item.get("duration", signature[3]), isrc=item.get("isrc"), id=item.get("id"))

    def checked(data):
        return inspect(data, code, lambda item: score(record(item)) is not None)

    def native(result, detail):
        item = result["data"]
        value = validate(item["lyrics"])
        credits = [{"url": detail["url"], "label": LINKS[1][0]},
                   {"url": detail["profile"], "label": detail["contributor"]}]
        return (record(item), value, credits) if value else None

    result = request(UNISON_BASE + "/lyrics?" + urlencode({"isrc": code}))
    found = checked(result)
    if not found:
        return None
    if found["format"] == "lrc":
        return native(result, found)

    # The ranked default can be TTML while a contributor also supplied LRC.
    # Only request a native variant from the confirmed video, then verify
    # its own recording metadata and attribution before accepting its bytes.
    video = result["data"]["videoId"]
    listed = request(f"{UNISON_BASE}/lyrics/variants/{video}?limit=5")
    variants = listed.get("data") if isinstance(listed, dict) and listed.get("success") is True else None
    if not isinstance(variants, list):
        return None
    for item in variants[:5]:
        identifier = item.get("id") if isinstance(item, dict) and item.get("format") == "lrc" else None
        if type(identifier) is int and 0 < identifier < 2**63:
            variant = request(f"{UNISON_BASE}/lyrics/{identifier}")
            detail = checked(variant)
            if (detail and detail["format"] == "lrc" and variant["data"].get("videoId") == video
                    and variant["data"].get("id") == identifier):
                return native(variant, detail)
            break  # One variant fetch per recording keeps the fallback bounded.
    return None
