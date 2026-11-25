"""Conservative music identity for comparisons. Original tags remain untouched."""
from dataclasses import dataclass, field
from math import isfinite
import re
from unicodedata import category, normalize

CONFIRMED = "confirmée"
PROBABLE = "probable"
DIFFERENT = "différente"

MBID = re.compile(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", re.I)
ISRC = re.compile(r"[A-Z]{2}[A-Z0-9]{3}[0-9]{7}")
HASH = re.compile(r"[0-9a-f]{64}")
POSITION = re.compile(r"([0-9]{1,3})(?:/[0-9]{1,3})?")
YEAR = re.compile(r"([12][0-9]{3})(?:-[0-9]{2}(?:-[0-9]{2})?)?")
APOSTROPHES = "'’‘‛ʼ＇"
FEATURE = re.compile(r"\s+(?:[\[(]\s*)?(?:featuring|feat\.?|ft\.?|avec)\s+", re.I)
EDITION_MARKERS = re.compile(r"\b(?:deluxe|live|concert|remix|acoustic|acoustique|instrumental|remaster(?:ed|ise)?)\b")


def normalized(value):
    if not isinstance(value, str):
        return ""
    return " ".join("".join(
        char if char.isalnum() else " "
        for char in normalize("NFKD", value.casefold())
        if not category(char).startswith("M") and char not in APOSTROPHES
    ).split())


def display_credit(value):
    if not isinstance(value, str):
        return "", ""
    values = FEATURE.split(value.strip(), maxsplit=1)
    return normalized(values[0]), normalized(values[1]) if len(values) > 1 else ""


def position(value):
    match = POSITION.fullmatch(str(value))
    return int(match[1]) if match else 0


def year(value):
    match = YEAR.fullmatch(str(value))
    return int(match[1]) if match else 0


def mbid(value):
    return value.lower() if isinstance(value, str) and MBID.fullmatch(value) else ""


def isrc(value):
    value = re.sub(r"[- ]", "", value).upper() if isinstance(value, str) else ""
    return value if ISRC.fullmatch(value) else ""


def barcode(value):
    digits = value.replace(" ", "").replace("-", "") if isinstance(value, str) else ""
    if len(digits) not in (8, 12, 13, 14) or not digits.isascii() or not digits.isdecimal():
        return ""
    total = sum(int(digit) * (3 if index % 2 else 1) for index, digit in enumerate(reversed(digits)))
    return digits.zfill(14) if total % 10 == 0 and any(digit != "0" for digit in digits) else ""


def duration_ms(value):
    try:
        numeric = float(value)
    except (ValueError, TypeError, OverflowError):
        return 0
    return round(1000 * numeric) if isfinite(numeric) and 0 < numeric <= 86_400 else 0


@dataclass(frozen=True)
class Track:
    disc: int
    number: int
    title: str
    millis: int
    recording: str = ""
    work: str = ""
    isrc: str = ""
    pcm: str = ""
    artist: str = ""


@dataclass(frozen=True)
class Release:
    title: str
    artist: str
    year: int = 0
    artist_id: str = ""
    group_id: str = ""
    release_id: str = ""
    barcode: str = ""
    discs: int = 0
    count: int = 0
    tracks: tuple[Track, ...] = ()
    edition: str = ""


@dataclass(frozen=True)
class Match:
    confidence: str
    reasons: tuple[str, ...] = field(default_factory=tuple)
    positions: tuple[tuple[int, int], ...] = field(default_factory=tuple)


def track_from(raw):
    if not isinstance(raw, dict):
        return None
    title = raw.get("title")
    disc, number = (position(raw.get(key)) for key in ("disc", "number"))
    if not isinstance(title, str) or not title.strip() or not disc or not number:
        return None
    pcm = raw.get("pcm")
    artist = raw.get("artist")
    return Track(disc, number, title[:240], duration_ms(raw.get("duration")),
                 mbid(raw.get("recording_id")), mbid(raw.get("work_id")), isrc(raw.get("isrc")),
                 pcm if isinstance(pcm, str) and HASH.fullmatch(pcm) else "",
                 artist[:240] if isinstance(artist, str) else "")


def from_catalogue(raw):
    """Build comparison-only data. Unknown values supply no positive evidence."""
    if not isinstance(raw, dict):
        return None
    title, artist = raw.get("title"), raw.get("artist")
    if not isinstance(title, str) or not isinstance(artist, str) or not title.strip() or not artist.strip():
        return None
    original_tracks = raw.get("tracks", [])
    tracks = ()
    if isinstance(original_tracks, list) and len(original_tracks) <= 1024:
        parsed = tuple(track_from(track) for track in original_tracks)
        if all(parsed) and len({(track.disc, track.number) for track in parsed}) == len(parsed):
            tracks = tuple(sorted(parsed, key=lambda track: (track.disc, track.number)))
    edition = raw.get("edition")
    return Release(title[:240], artist[:240], year(raw.get("year")), mbid(raw.get("artist_id")),
                   mbid(raw.get("group_id")), mbid(raw.get("release_id")), barcode(raw.get("barcode")),
                   position(raw.get("discs")), position(raw.get("count")), tracks,
                   edition[:240] if isinstance(edition, str) else "")


def same_track(left, right):
    title_a, credit_a = display_credit(left.title)
    title_b, credit_b = display_credit(right.title)
    bias = min(2500, max(1200, .007 * min(left.millis, right.millis)))
    duration_known = bool(left.millis and right.millis)
    if (title_a != title_b or credit_a and credit_b and credit_a != credit_b
            or duration_known and abs(left.millis - right.millis) > bias
            or left.recording and right.recording and left.recording != right.recording
            or left.artist and right.artist and normalized(left.artist) != normalized(right.artist)):
        return DIFFERENT
    exact_audio = bool(left.pcm and left.pcm == right.pcm)
    recording = bool(left.recording and left.recording == right.recording)
    shared_isrc = bool(left.isrc and left.isrc == right.isrc)
    # An ISRC participates only with agreeing chronology and credits; a work
    # identifier is deliberately irrelevant to the identity of its rendering.
    if title_a and (exact_audio or duration_known and (recording or shared_isrc)):
        return CONFIRMED
    return PROBABLE


def compare(left, right, aliases=()):
    """Compare editions. Only an explicit release proof can confirm a match."""
    if left is None or right is None:
        return Match(PROBABLE, ("données d’édition incomplètes",))
    title_a, title_b = normalized(left.title), normalized(right.title)
    markers_a = set(EDITION_MARKERS.findall(normalized(left.title + " " + left.edition)))
    markers_b = set(EDITION_MARKERS.findall(normalized(right.title + " " + right.edition)))
    if (title_a != title_b or markers_a != markers_b
            or left.edition and right.edition and normalized(left.edition) != normalized(right.edition)):
        return Match(DIFFERENT, ("titre ou mention d’édition distincts",))
    artist_a, _ = display_credit(left.artist)
    artist_b, _ = display_credit(right.artist)
    manually_approved = {frozenset((normalized(first), normalized(second))) for first, second in aliases}
    shared_artist = bool(left.artist_id and left.artist_id == right.artist_id)
    if (left.artist_id and right.artist_id and not shared_artist
            or artist_a != artist_b and not shared_artist
            and frozenset((artist_a, artist_b)) not in manually_approved):
        return Match(DIFFERENT, ("artiste principal distinct",))
    if left.release_id and right.release_id and left.release_id != right.release_id:
        return Match(DIFFERENT, ("identifiants MusicBrainz d’édition distincts",))
    if left.group_id and right.group_id and left.group_id != right.group_id:
        return Match(DIFFERENT, ("groupes de publication distincts",))
    if left.barcode and right.barcode and left.barcode != right.barcode:
        return Match(DIFFERENT, ("codes de publication distincts",))
    if left.year and right.year and abs(left.year - right.year) > 1:
        return Match(DIFFERENT, ("années d’édition divergentes",))
    if (left.count and right.count and left.count != right.count
            or left.discs and right.discs and left.discs != right.discs):
        return Match(DIFFERENT, ("nombre de pistes ou de disques distinct",))
    a = {(track.disc, track.number): track for track in left.tracks}
    b = {(track.disc, track.number): track for track in right.tracks}
    overlap = tuple(sorted(a.keys() & b.keys()))
    complete_a = bool(a) and len(a) == left.count
    complete_b = bool(b) and len(b) == right.count
    if complete_a and b.keys() - a.keys() or complete_b and a.keys() - b.keys():
        return Match(DIFFERENT, ("positions de pistes incompatibles",))
    if complete_a and complete_b and all(track.millis for track in (*a.values(), *b.values())):
        duration_a = sum(track.millis for track in a.values())
        duration_b = sum(track.millis for track in b.values())
        if abs(duration_a - duration_b) > max(3000, min(duration_a, duration_b) * .003):
            return Match(DIFFERENT, ("durées totales divergentes",))
    verdicts = [same_track(a[p], b[p]) for p in overlap]
    if DIFFERENT in verdicts:
        return Match(DIFFERENT, ("piste divergente à une position commune",))
    release_agrees = bool(left.release_id and left.release_id == right.release_id)
    barcode_agrees = bool(left.barcode and left.barcode == right.barcode)
    subset = bool((complete_a and b.keys() <= a.keys()) or (complete_b and a.keys() <= b.keys()))
    # A shared group says nothing about pressing/edition. Even the exact
    # release ID must be supported by ordered tracks rather than by title only.
    if overlap and subset and (release_agrees or barcode_agrees and complete_a and complete_b):
        if all(verdict == CONFIRMED for verdict in verdicts):
            return Match(CONFIRMED, ("édition et enregistrements concordants",), overlap)
        timing = all(a[p].millis and b[p].millis for p in overlap)
        if release_agrees and barcode_agrees and timing and len(overlap) >= 2:
            return Match(CONFIRMED, ("édition, code de publication et minutages concordants",), overlap)
    return Match(PROBABLE, ("preuves d’édition insuffisantes",), overlap)
