"""Formats et métadonnées communs aux deux commandes de l'audiothèque."""
from contextlib import contextmanager
from fcntl import LOCK_EX, LOCK_NB, flock
import json
import os
from pathlib import Path
import re
import subprocess
from unicodedata import combining, normalize

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


def probe(path):
    command = ("ffprobe", "-v", "error", "-protocol_whitelist", "file,pipe", "-of", "json",
               "-show_entries", "format_tags:stream=index,codec_type:stream_disposition=attached_pic:stream_tags",
               str(path))
    output = subprocess.run(command, capture_output=True, check=True, timeout=20)
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


def canonical_album_artist(credit, album=None):
    credit = credit or ""
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


SHARED_LIBRARY = Path("/home/Partage/Music")


def share_file(draft, destination):
    if destination.is_relative_to(SHARED_LIBRARY):
        # Les ACL héritées sont présentes, mais mkstemp masque leurs droits.
        os.chmod(draft, 0o660)


@contextmanager
def shared_mutation(root, enabled=True):
    if not enabled or root != SHARED_LIBRARY:
        yield
        return
    with (root / ".labfy-music.lock").open("a") as lock:
        try:
            flock(lock, LOCK_EX | LOCK_NB)
        except BlockingIOError as error:
            mode = os.environ.get("KARA_SHARED_WAIT")
            if mode not in {"suivi", "texte"}:
                raise SystemExit("Un autre import, tri ou enrichissement est en cours.") from error
            message = ("KARA_ATTENTE_BIBLIOTHEQUE" if mode == "suivi"
                       else "Attente de l’accès à la bibliothèque partagée…")
            print(message, flush=True)
            flock(lock, LOCK_EX)
            if mode == "suivi":
                print("KARA_BIBLIOTHEQUE_DISPONIBLE", flush=True)
        yield
