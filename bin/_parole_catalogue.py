"""Vérification éditoriale avec identifiants MusicBrainz et positions de pistes."""
import re
from urllib.parse import urlencode

from _parole_index import key
from _parole_sources import fetch

API = "https://musicbrainz.org/ws/2/"
MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)


def request(path, **params):
    result = fetch(f"{API}{path}?{urlencode(dict(fmt='json', **params))}")
    if result is None:
        return {}
    if not isinstance(result, dict):
        raise ValueError("Réponse MusicBrainz invalide")
    return result


def credits(item):
    return [(credit.get("artist", {}).get("id"), credit.get("name") or credit.get("artist", {}).get("name", ""))
            for credit in item.get("artist-credit", [])]


def positions(release):
    position = {}
    for medium in release.get("media", []):
        disc = medium.get("position")
        if type(disc) is not int or disc < 1:
            raise ValueError("Disque sans position")
        tracks = medium.get("tracks", [])
        if not tracks or medium.get("track-count", len(tracks)) != len(tracks):
            raise ValueError("Liste de pistes incomplète")
        for track in tracks:
            ordinal = track.get("position")
            title = track.get("title") or track.get("recording", {}).get("title")
            if type(ordinal) is not int or ordinal < 1 or not title or (disc, ordinal) in position:
                raise ValueError("Position officielle invalide ou doublée")
            position[disc, ordinal] = str(title)
    if not position:
        raise ValueError("Aucune position officielle")
    return position


def compare(local, release):
    official = positions(release)
    found, duplicate, conflicting, unidentified = set(), [], [], []
    for track in local:
        disc = track["disc"] or (1 if {d for d, _ in official} == {1} else 0)
        position = (disc, track["track"])
        if not all(position):
            unidentified.append(track["title"])
        elif position in found:
            duplicate.append([*position, track["title"]])
        elif position not in official or key(track["title"]) != key(official[position]):
            conflicting.append([*position, track["title"]])
        else:
            found.add(position)
    missing = [[*p, title] for p, title in sorted(official.items()) if p not in found]
    state = "ambiguous" if conflicting or unidentified else "incomplete" if missing or duplicate else "complete"
    return state, {"missing": missing, "duplicates": duplicate, "conflicting": conflicting,
                   "unidentified": unidentified, "verified_tracks": len(found)}


def quoted(value):
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def check_album(local, associations, lookup=request):
    first = local[0]
    name, owner = first["album"], first["owner"]
    ids = {r["release_id"] for r in local if MBID.fullmatch(r["release_id"])}
    artist_ids = {r["artist_id"] for r in local if MBID.fullmatch(r["artist_id"])}
    if len(ids) > 1 or len(artist_ids) > 1:
        return "ambiguous", {}
    known = {key(owner), *[key(old) for old in associations]}
    unconfirmed = not bool(ids)
    if ids:
        candidates = list(ids)
    else:
        queries = ([f'release:{quoted(name)} AND arid:{quoted(next(iter(artist_ids)))}'] if artist_ids else
                   [f'release:{quoted(name)} AND artist:{quoted(artist)}' for artist in dict.fromkeys([owner, *associations])][:3])
        candidates = []
        for query in queries:
            records = lookup("release", query=query, limit=5)
            for release in records.get("releases", []):
                if (release.get("status") == "Official" and key(release.get("title", "")) == key(name)
                        and any(key(artist) in known for _, artist in credits(release))
                        and (not first["year"] or str(release.get("date", ""))[:4] == str(first["year"]))
                        and MBID.fullmatch(release.get("id", ""))):
                    candidates.append(release["id"])
            if records.get("count", 0) > 5 and not artist_ids:
                return "ambiguous", {"candidates": sorted(set(candidates))}
        candidates = list(dict.fromkeys(candidates))
    if not candidates:
        return "not_found", {}
    if len(candidates) > 4:
        return "ambiguous", {"candidates": candidates}
    matching = []
    for identity in candidates:
        candidate = lookup("release/" + identity, inc="recordings+artist-credits+release-groups")
        candidate_credits = credits(candidate)
        same_credit = (bool(artist_ids & {artist_id for artist_id, _ in candidate_credits}) if artist_ids else
                       any(key(artist) in known for _, artist in candidate_credits))
        if (candidate.get("status") != "Official" or key(candidate.get("title", "")) != key(name)
                or not same_credit):
            continue
        state, details = compare(local, candidate)
        if state != "ambiguous":
            if unconfirmed and details["verified_tracks"] < 2:
                continue
            matching.append((identity, state, details, candidate))
    if len(matching) != 1:
        return "ambiguous", {"candidates": candidates}
    identity, state, details, release = matching[0]
    release_group = (release.get("release-group") or {}).get("id", "")
    return state, dict(details, id=identity, release_group=release_group, verified=not unconfirmed,
                       artists=credits(release), title=name)


def check_discography(identifier, group_ids, local_titles, categories, lookup=request):
    if not MBID.fullmatch(identifier):
        return "ambiguous", {"reason": "Identifiant d’artiste non confirmé"}
    result = []
    offset = 0
    while offset < 500:
        page = lookup("release-group", artist=identifier, inc="artist-credits", limit=100, offset=offset,
                      **{"release-group-status": "website-default"})
        groups = page.get("release-groups", [])
        for group in groups:
            group_id, title = group.get("id", ""), group.get("title", "")
            secondary = set(group.get("secondary-types") or [])
            kind = group.get("primary-type")
            if not MBID.fullmatch(group_id) or not title or group_id in group_ids or key(title) in local_titles:
                continue
            eligible = bool(secondary & set(categories)) if secondary else kind in categories
            artist_credits = credits(group)
            if len(artist_credits) > 1 and "Collaboration" not in categories:
                continue
            if eligible and group.get("first-release-date"):
                result.append({"id": group_id, "title": title, "kind": ", ".join(sorted(secondary)) or kind,
                               "date": group["first-release-date"]})
        offset += len(groups)
        if offset >= page.get("release-group-count", 0) or not groups:
            return "checked", {"missing": result, "partial": False}
    return "partial", {"missing": result, "partial": True}
