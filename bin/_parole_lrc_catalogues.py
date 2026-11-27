"""Récupère les LRC natifs depuis deux catalogues publics avec leurs crédits."""

from urllib.parse import urlencode

from _parole_lrc_credit import RED, UNISON, LINKS, red_contact
from _parole_sources import ProviderUnavailable, fetch
from _parole_unison import BASE as UNISON_BASE, inspect, isrc

RED_BASE = "https://lrc.red"
USER_AGENT = "Parole/3 (https://github.com/labfytools/kara-Samsung; contact: {})"


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
    data = request(UNISON_BASE + "/lyrics?" + urlencode({"isrc": code}))

    def record(item):
        # Unison may omit duration on an exact ISRC response. The ISRC and
        # edition must still match and all native timestamps fit the track.
        return dict(trackName=item.get("song"), artistName=item.get("artist"), albumName=item.get("album"),
                    duration=item.get("duration", signature[3]), isrc=item.get("isrc"), id=item.get("id"))

    found = inspect(data, code, lambda item: score(record(item)) is not None)
    if not found or found["format"] != "lrc":
        return None
    item = data["data"]
    value = validate(item["lyrics"])
    credits = [{"url": found["url"], "label": LINKS[1][0]}, {"url": found["profile"], "label": found["contributor"]}]
    return (record(item), value, credits) if value else None
