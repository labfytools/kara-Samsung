"""Verified equivalent offers, persisted before a managed job can claim them."""
from dataclasses import dataclass
from itertools import combinations, zip_longest
from math import isfinite
from time import time
from urllib.parse import urlsplit

from _parole_import_metrics import LOOKUP, STARTUP, SUCCEEDED, THROUGHPUT, TOTAL, snapshot
from _parole_import_providers import ARCHIVE, JAMENDO, archive_identifier, audio_source, jamendo_identifier
from _parole_index import document, encoded_key
from _parole_music_identity import CONFIRMED, compare, from_catalogue
from _parole_music_registry import approved_aliases

STRATEGIES = {
    "speed": "Vitesse maximale",
    "quality": "Qualité audio maximale",
    "preferred": "Fournisseur préféré",
}
AUTOMATIC = (ARCHIVE, JAMENDO)
GROUP_PREFIX = "parole://edition/"
LOSSLESS = {"FLAC", "WAV", "AIFF", "ALAC", "APE"}
LOSSY = {"AAC", "M4A", "MP3", "OGG", "OPUS", "WMA"}


def plan_id(url):
    if not url.startswith(GROUP_PREFIX):
        return None
    value = url.removeprefix(GROUP_PREFIX)
    return value if len(value) == 64 and all(ch in "0123456789abcdef" for ch in value) else None


def source_order(urls, previous=None):
    # Bring each requested source into the album window early. On resume,
    # prior failed extractions must not monopolize that source's resolution
    # slots while fresh targets are still waiting. Positions in the durable
    # lot and order among equally advanced albums are preserved.
    previous = previous or {}
    priority = {"suspended": 1, "partial": 2, "failure": 2}
    lanes = {}
    for ordinal, url in enumerate(urls, 1):
        lane = "edition" if plan_id(url) else audio_source(url)
        lanes.setdefault(lane, []).append((ordinal, url))
    for items in lanes.values():
        items.sort(key=lambda item: priority.get(previous.get(item[1], {}).get("status"), 0))
    return (item for group in zip_longest(*lanes.values()) for item in group if item is not None)


def offers_are_equivalent(left, right, *, index=None):
    first, second = from_catalogue(left.get("facts")), from_catalogue(right.get("facts"))
    if first is None or second is None:
        return False
    return (left["provider"] != right["provider"]
            and compare(first, second, approved_aliases(index, first.title)).confidence == CONFIRMED)


def valid_offer(offer):
    if not isinstance(offer, dict):
        return False
    provider, url = offer.get("provider"), offer.get("url")
    facts = from_catalogue(offer.get("facts"))
    if (provider not in AUTOMATIC or not isinstance(url, str) or len(url) > 400
            or type(offer.get("price")) is not int or offer["price"] != 0 or facts is None
            or len(facts.tracks) != facts.count or not facts.count
            or not isinstance(offer.get("formats"), list) or not offer["formats"]):
        return False
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    positions = {(t.disc, t.number) for t in facts.tracks}
    if len(positions) != facts.count:
        return False
    if provider == ARCHIVE:
        return archive_identifier(parts) is not None
    item = jamendo_identifier(parts)
    return bool(item and item[0] == "album")


def create_plan(index, offers):
    # This is an immutable acquisition plan. A provider reply must still
    # reconfirm its own edition and permissions at execution.
    if not 2 <= len(offers) <= len(AUTOMATIC) or not all(valid_offer(offer) for offer in offers):
        raise ValueError("Deux offres de téléchargement d’édition vérifiables sont nécessaires.")
    offers = sorted(offers, key=lambda offer: (offer["provider"], offer["url"]))
    if any(not offers_are_equivalent(a, b, index=index) for a, b in combinations(offers, 2)):
        raise ValueError("Les offres ne désignent pas toutes la même édition confirmée.")
    if len(document(offers).encode()) > 1_000_000:
        raise ValueError("Descriptif d’édition trop volumineux.")
    identity = stable_plan_id(offers)
    with index.db() as db:
        db.execute("INSERT OR IGNORE INTO import_offer_plans VALUES(?,?)", (identity, document(offers)))
    return GROUP_PREFIX + identity


def stable_plan_id(offers):
    # Discovery may report a fresh byte count or a slightly different duration.
    # The same release and provider catalogue IDs must find the same receipts.
    releases = [from_catalogue(offer["facts"]) for offer in offers]
    proof = (("musicbrainz", releases[0].release_id) if releases[0].release_id and
             all(r.release_id == releases[0].release_id for r in releases) else ("gtin", releases[0].barcode))
    members = []
    for offer in offers:
        provider = offer["provider"]
        parts = urlsplit(offer["url"])
        key = archive_identifier(parts) if provider == ARCHIVE else jamendo_identifier(parts)[1]
        members.append((provider, key))
    return encoded_key("import-offers-v1", proof, sorted(members))


def load_plan(index, identity):
    import json

    with index.db() as db:
        row = db.execute("SELECT offers FROM import_offer_plans WHERE id=?", (identity,)).fetchone()
    if row is None:
        return []
    offers = json.loads(row[0])
    if not 2 <= len(offers) <= len(AUTOMATIC) or not all(valid_offer(offer) for offer in offers):
        return []
    if (stable_plan_id(offers) != identity or
            any(not offers_are_equivalent(a, b, index=index) for a, b in combinations(offers, 2))):
        return []
    return offers


def measured(metrics, metric):
    value = metrics.get(metric, {}).get("mean")
    return value if isinstance(value, (float, int)) and isfinite(value) and value > 0 else None


def quality_key(offer):
    formats = {value.upper() for value in offer.get("formats", []) if isinstance(value, str)}
    lossless = bool(formats & LOSSLESS)
    lossy = bool(formats & LOSSY)
    # Jamendo's FLAC is the requested delivery format. Archive can also
    # distinguish a publisher-provided original from a platform derivative.
    original = offer["provider"] == ARCHIVE
    return (2 if lossless else int(lossy), original)


@dataclass(frozen=True)
class Ranked:
    offer: dict
    estimate: float | None
    throughput: float | None
    reason: str


def rank(index, offers, *, remaining=None, now=None, admission_waits=None):
    now = time() if now is None else now
    config = index.settings()
    strategy = config["import_strategy"]
    preferred = config["import_preferred"]
    history = snapshot(index, now=now)
    observed = []
    for offer in offers:
        source = offer["provider"]
        stats = history.get(source, {})
        if not valid_offer(offer) or not index.source_enabled(source) or stats.get("limited_until", 0) > now:
            continue
        count = len(offer["facts"]["tracks"])
        pending = count if remaining is None else min(count, remaining)
        throughput, startup, search_time, total = (measured(stats, key) for key in (THROUGHPUT, STARTUP, LOOKUP, TOTAL))
        size = offer.get("bytes")
        if throughput is not None and type(size) is int and size > 0:
            estimate = (startup or 0) * pending + (size * pending / count) / throughput
            if total is not None:
                estimate = max(estimate, total * pending / count)
            estimate += search_time or 0
        elif total is not None:
            estimate = total * pending / count + (search_time or 0)
        else:
            estimate = None
        success = stats.get(SUCCEEDED)
        if estimate is not None and success:
            estimate *= 1 + (1 - success["mean"]) * min(1., success["samples"] / 4)
        observed.append((offer, estimate, throughput, stats.get(SUCCEEDED, {}).get("samples", 0)))
    attempted = sum(record[3] for record in observed)
    explore = (attempted % 5 == 0 and any(record[3] == 0 for record in observed))
    known = sorted(record[1] for record in observed if record[1] is not None)
    estimated_unknown = known[len(known) // 2] if known else 0.
    order = []
    for offer, estimate, throughput, samples in observed:
        quality = quality_key(offer)
        eta = estimate if estimate is not None else estimated_unknown
        if strategy == "preferred" and offer["provider"] == preferred:
            priority, reason = (-1,), "fournisseur préféré disponible"
        elif strategy == "quality":
            priority = (0, -quality[0], -int(quality[1]), eta)
            reason = "qualité audio disponible puis délai estimé"
        else:
            trial = explore and samples == 0
            wait = (admission_waits or {}).get(offer["provider"], 0.) if strategy == "speed" else 0.
            priority = (0, not trial, eta + wait, -quality[0], -int(quality[1]))
            estimate = eta + wait if estimate is not None or wait else None
            reason = ("délai estimé avec attente de créneau" if wait else
                      "premier transfert sur une source équivalente" if trial else
                      "délai de récupération estimé" if estimate is not None else "débit encore inconnu")
        order.append((priority, offer["provider"], offer["url"], Ranked(offer, estimate, throughput, reason)))
    return [value[-1] for value in sorted(order, key=lambda value: value[:3])]
