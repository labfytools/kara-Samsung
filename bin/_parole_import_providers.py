"""Download routes documented by music providers."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from _kara_import_result import ImportIssue

ALL = "Toutes les sources"
YOUTUBE = "YouTube Music"
OPEN_LICENSES = {
    "CC0 1.0": "https://creativecommons.org/publicdomain/zero/1.0/",
    "CC BY 3.0": "https://creativecommons.org/licenses/by/3.0/",
    "CC BY 4.0": "https://creativecommons.org/licenses/by/4.0/",
    "CC BY-SA 3.0": "https://creativecommons.org/licenses/by-sa/3.0/",
    "CC BY-SA 4.0": "https://creativecommons.org/licenses/by-sa/4.0/",
}


@dataclass(frozen=True)
class ManualProvider:
    host: str
    downloads: str
    help: str
    album_hosts: re.Pattern
    album_path: re.Pattern
    licenses: tuple = ()

    def release_reference(self, value):
        # Only the public edition URL is provenance. Account, payment and
        # signed download URLs are private and never travel through the queue.
        try:
            parts = urlsplit(value)
        except ValueError as error:
            raise ImportIssue("path", "Adresse publique d’édition invalide.") from error
        if (len(value) > 400 or parts.scheme != "https" or not self.album_hosts.fullmatch(parts.netloc)
                or not self.album_path.fullmatch(parts.path) or parts.query or parts.fragment):
            raise ImportIssue("path", "Indique la page publique de l’édition, sans paramètres ni lien temporaire.")
        return f"https://{parts.netloc}{parts.path.rstrip('/')}"

    def provenance(self, reference, license_url):
        if self.licenses and (license_url not in self.licenses or not reference):
            raise ImportIssue("path", "Indique la page publique et la licence de téléchargement affichée.")
        if license_url and license_url not in self.licenses:
            raise ImportIssue("path", "Licence non prise en charge par ce fournisseur.")
        return self.release_reference(reference) if reference else "", license_url


MANUAL = {
    "Bandcamp": ManualProvider(
        "bandcamp.com", "https://bandcamp.com/",
        "https://get.bandcamp.help/en/articles/15263192-can-i-re-download-my-purchases",
        re.compile(r"[a-z0-9](?:[a-z0-9-]{0,62})\.bandcamp\.com"),
        re.compile(r"/(?:album|track)/[a-z0-9][a-z0-9-]{0,159}/?")),
    "Qobuz": ManualProvider(
        "qobuz.com", "https://www.qobuz.com/",
        "https://help.qobuz.com/en/articles/369151-tutorial-how-to-download-without-qobuz-downloader",
        re.compile(r"(?:www\.|open\.)?qobuz\.com"),
        re.compile(r"/(?:[a-z]{2}-[a-z]{2}/)?album/(?:[a-z0-9][a-z0-9-]{0,159}/)?[a-z0-9]{5,30}/?")),
    "SoundCloud": ManualProvider(
        "soundcloud.com", "https://soundcloud.com/",
        "https://help.soundcloud.com/hc/en-us/articles/115003448787-Downloading-tracks",
        re.compile(r"(?:www\.)?soundcloud\.com"),
        re.compile(r"/[a-z0-9][a-z0-9_-]{0,89}/(?:sets/)?[a-z0-9][a-z0-9_-]{0,159}/?")),
    "Free Music Archive": ManualProvider(
        "freemusicarchive.org", "https://freemusicarchive.org/",
        "https://freemusicarchive.org/License_Guide",
        re.compile(r"(?:www\.)?freemusicarchive\.org"),
        re.compile(r"/music/(?:[A-Za-z0-9_%-]{1,90}/){1,2}[A-Za-z0-9_%-]{1,140}/?"),
        tuple(OPEN_LICENSES.values())),
}
