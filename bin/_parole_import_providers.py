"""Download routes documented by music providers."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from _kara_import_result import ImportIssue

ALL = "Toutes les sources"
YOUTUBE = "YouTube Music"


@dataclass(frozen=True)
class ManualProvider:
    host: str
    downloads: str
    help: str
    album_hosts: re.Pattern
    album_path: re.Pattern

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
}
