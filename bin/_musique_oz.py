"""Formats et métadonnées communs aux deux commandes de l'audiothèque."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
from functools import lru_cache
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
from unicodedata import combining, normalize
from urllib.parse import parse_qs, urlsplit

from _kara_paths import STATE, music_library
from _kara_import_resources import lock_file, run_checked

AUDIO = frozenset((".aac", ".aif", ".aiff", ".alac", ".flac", ".m4a", ".mp3",
                   ".ogg", ".oga", ".opus", ".wav", ".wma"))
INBOX = "A_trier"
UNKNOWN_ARTIST = "Artiste inconnu"
UNKNOWN_ALBUM = "Sans album"


def audio_file(path):
    return path.suffix.lower() in AUDIO and not path.is_symlink() and path.is_file()


def audio_files(path):
    if audio_file(path):
        yield path
    elif path.is_dir():
        for base, folders, files in os.walk(path):
            folders[:] = sorted(folder for folder in folders if not folder.startswith("."))
            for name in sorted(files):
                track = Path(base) / name
                if not name.startswith(".") and audio_file(track):
                    yield track


def probe(path, *, include_duration=False, include_codec=False):
    command = ("ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-of", "json",
               "-show_entries", "format=duration:format_tags:stream=index,codec_type,codec_name:stream_disposition=attached_pic:stream_tags",
               str(path))
    output = run_checked(command, capture_output=True, check=True, timeout=20)
    info = json.loads(output.stdout)
    streams = info.get("streams", [])
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("aucune piste audio")
    tags = {}
    for source in (info.get("format", {}), audio):
        for key, value in source.get("tags", {}).items():
            if isinstance(value, str) and value.strip():
                tag = re.sub(r"[^a-z0-9]", "", key.casefold())
                tags[tag] = value.strip()
    if include_duration:
        try:
            from math import isfinite
            duration = float(info.get("format", {}).get("duration", 0))
            if isfinite(duration) and duration > 0:
                tags["duration_seconds"] = duration
        except (TypeError, ValueError):
            pass
    if include_codec:
        tags["codec"] = audio.get("codec_name", "")
    pictures = [stream for stream in streams if stream.get("disposition", {}).get("attached_pic")]
    pictures.sort(key=lambda stream: "front" not in stream.get("tags", {}).get("comment", "").casefold())
    return tags, pictures


def title_pair(tags):
    return tags.get("albumartist") or tags.get("artist"), tags.get("album")


def component(value, fallback):
    value = normalize("NFC", value or "")
    value = re.sub(r"[\x00-\x1f\x7f/\\]+", " - ", value)
    value = " ".join(value.split()).strip(" .")
    return value.encode("utf-8")[:170].decode("utf-8", "ignore").rstrip(" .") or fallback


@lru_cache(maxsize=4)
def album_associations(path, _revision):
    from contextlib import closing

    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=.4)) as db:
            rules = {}
            for old, album, new, identities in db.execute("""
                SELECT a.old_artist,a.album,a.new_artist,group_concat(DISTINCT s.artist_id)
                FROM associations AS a LEFT JOIN songs AS s ON s.owner=a.new_artist AND s.album=a.album
                GROUP BY a.old_artist,a.album,a.new_artist
            """):
                known = {value for raw in (identities or "").split(",") if (value := musicbrainz_artist(raw))}
                for pair in ((old, album), (artist_spelling(old), artist_spelling(album))):
                    rules.setdefault(pair, {}).setdefault(new, set()).update(known)
            return {pair: next(iter(names.items())) for pair, names in rules.items() if len(names) == 1}
    except sqlite3.Error:
        return {}


def artist_spelling(value):
    folded = "".join(c for c in normalize("NFKD", value or "").casefold() if not combining(c))
    # Compare spellings inside a confirmed album association. Keep each
    # collaborator in the key so a shared lead credit never creates an alias.
    credits = re.split(r"[,/&]|\s+(?:and|et|y|avec|feat\.?|ft\.?)\s+", folded)
    return tuple("".join(c for c in credit if c.isalnum()) for credit in credits)


def musicbrainz_artist(value):
    value = (value or "").casefold()
    return value if re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", value) else ""


def index_file(root):
    key = sha256(json.dumps([str(root)], ensure_ascii=True, separators=(",", ":")).encode()).hexdigest()
    return STATE / "kara/parole" / key / "maintenance.sqlite3"


def index_revision(path):
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    wal = path.with_name(path.name + "-wal")
    try:
        return stamp, wal.stat().st_mtime_ns
    except OSError:
        return stamp, 0


def album_groups(database):
    # WAL changes also invalidate the cache used by the existing sorter.
    revision = index_revision(database)
    return album_associations(database, revision) if revision is not None else {}


def grouped_artist(rules, credit, album, artist_id=None):
    rule = rules.get((credit, album)) or rules.get((artist_spelling(credit), artist_spelling(album)))
    if rule:
        owner, known = rule
        identity = musicbrainz_artist(artist_id)
        if not identity or not known or identity in known:
            return owner


def album_group(credit, album, *, database=None, artist_id=None):
    return grouped_artist(album_groups(database or index_file(SHARED_LIBRARY)), credit, album, artist_id)


@lru_cache(maxsize=4)
def group_locations(database, _revision):
    from contextlib import closing

    locations = {}
    try:
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=.4)) as db:
            for state, value in db.execute("SELECT state,value FROM reviews WHERE kind='artist_group' ORDER BY checked"):
                for row in json.loads(value)["moves"]:
                    source, target = (Path(row[field]) for field in ("source", "destination"))
                    if any(p.is_absolute() or len(p.parts) != 2 or ".." in p.parts for p in (source, target)):
                        continue
                    if state in ("undoing", "undone"):
                        source, target = target, source
                        locations.pop(target, None)
                    locations[source] = target
    except (sqlite3.Error, ValueError, KeyError, TypeError):
        return {}
    return locations


def grouped_path(root, path, *, database=None):
    # Historical receipts remain immutable. Resolve only missing paths and
    # only through a recorded album move for this collection.
    if not path.is_relative_to(root) or path.exists() or path.is_symlink():
        return path
    parts = path.relative_to(root).parts
    if len(parts) < 2:
        return path
    database = database or index_file(root)
    revision = index_revision(database)
    if revision is None:
        return path
    locations = group_locations(database, revision)
    parent = Path(*parts[:2])
    visited = set()
    while parent not in visited and parent in locations:
        visited.add(parent)
        parent = locations[parent]
        destination = (root / parent).joinpath(*parts[2:])
        if destination.exists() and destination.resolve() == destination:
            return destination
    return path


def canonical_album_artist(credit, album=None, *, artist_id=None):
    credit = credit or ""
    if album and (group := album_group(credit, album, artist_id=artist_id)):
        return group
    lead = credit.split(",", 1)[0].strip()
    if re.fullmatch(r"Juan Luis Guerra(?:\s*(?:&|y)?\s*4[.:]\s*40)?", lead, flags=re.IGNORECASE):
        return "Juan Luis Guerra"

    title = "".join(c for c in normalize("NFKD", album or "") if not combining(c))
    title = " ".join(title.casefold().replace("’", "'").split())
    if re.fullmatch(r"Myriam Hern[aá]ndez", lead, flags=re.IGNORECASE):
        if lead == credit or title in {"en concierto", "enamorandome", "seduccion", "tauro",
                                       "serie verde- myrian hernandez"} or title.startswith("nuestra navidad"):
            return "Myriam Hernández"
    if lead.casefold() == "la sonora de tommy rey":
        if lead == credit or title == "cantemos con la sonora de tommy rey":
            return "La Sonora de Tommy Rey"

    # L'artiste d'album sert au classement de la collection personnelle.
    # Le tag artiste de chaque piste conserve ses crédits d'origine.
    normalized_credit = normalize("NFKC", credit).casefold().strip()
    if title == "12 golden hits":
        credits = {name.strip() for name in normalized_credit.split(",")}
        if {"bonny cepeda", "johnny ventura", "juan luis guerra"} <= credits:
            return "Juan Luis Guerra"
    if re.fullmatch(r"le clash(?: - round [2-4])? \(b\.o\.s\.s\. vs\. iv my people\)", title):
        return "Suprême NTM"
    if re.fullmatch(r"don't be stupid(?: \(deluxe edition\))?", title):
        if re.search(r"\b(?:kurupt|c-mob|gotti mob)\b", normalized_credit):
            return "Kurupt"
    if title == "dpg wu-tang, wu-tang dpg -the godbody lp-":
        return "Kurupt"
    separator = r"(?:$|\s*(?:,|&|and\b|feat\b|x\b))"
    pac = r"(?:2pac|tupac(?: shakur)?)"
    big = r"(?:(?:the )?notorious b\.?i\.?g\.?|biggie(?: smalls)?)"
    if re.match(pac + separator, normalized_credit):
        return "2Pac"
    if title == "soldier 2 soldier" and "dead prez" in normalized_credit and "outlawz" in normalized_credit:
        return "2Pac"
    if re.match(big + separator, normalized_credit):
        return "The Notorious B.I.G."
    if title == "the king & i" and re.match(r"faith evans" + separator, normalized_credit):
        if re.search(r"\b" + big + separator, normalized_credit):
            return "The Notorious B.I.G."
    if title == "snoop cube 40 $hort" and re.match(r"mount westmore" + separator, normalized_credit):
        return "Snoop Dogg"
    if title == "high" and re.fullmatch(r"wiz khalifa\s*(?:,|&|and)\s*snoop dogg", normalized_credit):
        return "Snoop Dogg"
    if re.match(r"(?:snoop doggy dogg|snoop dogg)" + separator, normalized_credit):
        return "Snoop Dogg"
    for artist in ("Kurupt", "Cypress Hill", "Xzibit"):
        if re.match(artist.casefold() + separator, normalized_credit):
            return artist
    if title.replace("œ", "oe") == "joe dassin chante avec les choeurs de l'armee rouge":
        return "Joe Dassin"
    if title == "black sunday live at the royal albert hall" and normalized_credit.startswith("london symphony orchestra"):
        return "Cypress Hill"
    if normalized_credit == "serial killers" and title in {
        "serial killers presents: summer of sam",
        "serial killers, vol. 1",
        "serial killers: the murder show",
        "serial killers: this thing of ours",
    }:
        return "Xzibit"

    wu_member = (
        r"bobby digital|cappadonna|dirt mcgirt|ghostface(?: killah)?|"
        r"gza(?: the genius)?|(?:the )?genius\s*(?:[/&-]\s*)gza|"
        r"inspectah deck|masta\s*killa|method man|"
        r"o\.?d\.?b\.?|ol['’]?\s+dirty bastard|prince rakeem|"
        r"raekwon(?: the chef)?|rza(?: as bobby digital)?|u[- ]?god"
    )
    wu_tang = r"wu[-‐‑– ]?tang(?:\s+clan)?"
    if re.match(rf"(?:{wu_tang}|{wu_member}){separator}", normalized_credit):
        return "Wu-Tang Clan"
    if title in {
        "black samson, the bastard swordsman",
        "dreddy kruger presents: think differently music - wu-tang meets the indie culture the lost files",
        "masta killa presents: the next chamber",
        "wu music group presents pollen: the swarm, pt. 3",
        "wu tang vs. shaolin masters",
        "wu-block",
        "wu-chronicles",
        "wu-chronicles: chapter 2",
        "wu-tang collective",
        "wu: the story of the wu-tang clan",
    }:
        return "Wu-Tang Clan"
    return credit or None


def source_video(url):
    try:
        parsed = urlsplit(url or "")
        if parsed.hostname == "youtu.be":
            video = parsed.path.strip("/")
        elif parsed.hostname in {"youtube.com", "www.youtube.com", "music.youtube.com"}:
            video = parse_qs(parsed.query).get("v", [""])[0]
        else:
            return ""
    except ValueError:
        return ""
    return video if re.fullmatch(r"[A-Za-z0-9_-]{11}", video) else ""


# Keep the personal music directory (a link to the shared collection on ArchASP)
# and explicit CLI destinations on the same canonical path for locks and ACLs.
SHARED_LIBRARY = music_library()


def share_file(draft, destination):
    if destination.is_relative_to(SHARED_LIBRARY):
        # Les ACL héritées sont présentes, mais mkstemp masque leurs droits.
        os.chmod(draft, 0o660)


def mutation_report(message, *, file=None):
    try:
        print(message, file=file, flush=True)
    except BrokenPipeError:
        if os.environ.get("KARA_SHARED_WAIT") != "suivi":
            raise
        # A managed sorter still owns publication receipts after losing its
        # reporting parent. Drain subsequent messages locally.
        if file is sys.stderr:
            sys.stderr = open(os.devnull, "w")
        else:
            sys.stdout = open(os.devnull, "w")


@contextmanager
def shared_mutation(root, enabled=True, *, wait=False):
    if not enabled or root != SHARED_LIBRARY:
        yield
        return
    with (root / ".labfy-music.lock").open("a") as lock:
        if wait:
            # JSON import workers report through their own protocol.
            lock_file(lock)
            yield
            return
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError as error:
            mode = os.environ.get("KARA_SHARED_WAIT")
            if mode not in {"suivi", "texte"}:
                raise SystemExit("Un autre import, tri ou enrichissement est en cours.") from error
            message = ("KARA_ATTENTE_BIBLIOTHEQUE" if mode == "suivi"
                       else "Attente de l’accès à la bibliothèque partagée…")
            mutation_report(message)
            lock_file(lock)
            if mode == "suivi":
                mutation_report("KARA_BIBLIOTHEQUE_DISPONIBLE")
        yield
