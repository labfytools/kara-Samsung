"""Politique bornée de diagnostic au-dessus de la file d'import commune."""
from dataclasses import dataclass
import json
import re
import shutil
import subprocess
from time import time
from urllib.parse import parse_qs, urlsplit

from _kara_import_local import LOCAL as LOCAL_PROVIDER, local_url
from _kara_import_queue import import_key
from _kara_paths import catalogue_command
from _parole_index import encoded_key
from _parole_import_providers import ARCHIVE, archive_identifier
from _parole_process import Interrupted

HOUR = 3600
DAY = 24 * HOUR
TEMPORARY = {"network", "provider", "rate"}
LOCAL = {"space", "permissions", "interrupted", "local_file", "authorization"}
BEARING = {"playlist", "track", "edition", "metadata", "format", "engine"}
LABELS = {
    "network": "Erreur réseau temporaire", "provider": "Fournisseur indisponible", "rate": "Limitation du fournisseur",
    "playlist": "Playlist indisponible", "track": "Piste indisponible", "edition": "Édition à identifier",
    "metadata": "Métadonnées incohérentes", "format": "Format audio incompatible", "ffmpeg": "Échec FFmpeg",
    "lyrics": "Échec du traitement des paroles", "sort": "Échec du tri", "integration": "Indexation Strawberry",
    "permissions": "Permissions de la bibliothèque", "space": "Espace insuffisant", "interrupted": "Import interrompu",
    "dependency": "Dépendance ou version incompatible", "api": "Réponse fournisseur incompatible",
    "engine": "Échec du moteur sans cause confirmée", "local_file": "Sélection locale à vérifier",
    "authorization": "Autorisation de téléchargement à vérifier",
}
SECRET = re.compile(r"https?://\S+|(?:~|/)(?:[a-z0-9_.-]+/)+\S+|"
                    r"(?i:\b(?:token|cookie|bearer|authorization|password|secret|apikey|api_key|credential)\b).*")
VERSION = re.compile(r"\b(?:[0-9]{1,4}\.){1,3}[0-9]{1,4}(?:[-+._a-zA-Z0-9]{0,24})\b")
YTHOSTS = {"youtu.be", "youtube.com", "www.youtube.com", "music.youtube.com"}


@dataclass(frozen=True)
class Failure:
    cause: str
    stage: str
    provider: str
    url: str
    http: int | None
    returncode: int | None
    retry_after: int
    tracks: tuple
    artist: str
    title: str


def label(value):
    # Fields are catalogue labels, never log lines. Strip URLs, paths and
    # obvious credential syntax as a second layer before constructing a prompt.
    if not isinstance(value, str):
        return ""
    return SECRET.sub("[expurgé]", " ".join(value.split()))[:160]


def classify(entry, outcome):
    detail = str(entry.get("detail", ""))[:16000].casefold()
    stage_text = str(entry.get("stage", "")).casefold()[:180]
    category = entry.get("category")
    source = (outcome.get("url") or "")[:4000]
    local_input = local_url(source)
    try:
        parsed = urlsplit(source)
        host = parsed.hostname
        from_archive = archive_identifier(parsed) is not None
    except ValueError:
        host = None
        from_archive = False
    provider = "YouTube Music" if host in YTHOSTS else ARCHIVE if from_archive else "Local"
    http_match = re.search(r"\bhttp(?: error)?[ :]+([1-5][0-9]{2})\b", detail)
    http = int(http_match[1]) if http_match else None
    stage = next((identifier for fragment, identifier in (
        ("integrat", "strawberry"), ("indexation", "strawberry"), ("tri", "tri"),
        ("paroles", "paroles"), ("pochettes", "pochettes"), ("playlist", "playlist"),
        ("catalog", "catalogue"), ("convert", "ffmpeg"), ("téléchargement", "transfert"))
        if fragment in stage_text), "import")
    cause = "engine"
    if category == "space" or re.search(r"no space left|espace disque insuffisant|disk quota", detail):
        cause = "space"
    elif re.search(r"interrompu volontairement|interruption volontaire|annulé par|arrêt avant téléchargement", detail):
        cause = "interrupted"
    elif re.search(r"permission denied|permission refusée|read-only file system|accès refusé", detail):
        cause = "permissions"
    elif local_input and category == "path":
        cause = "local_file"
    elif from_archive and category == "path" and not re.search(r"\bhttp 5\d\d\b", detail):
        cause = "authorization"
    elif http == 429 or re.search(r"too many requests|retry-after|rate.limit|limitation de requêtes", detail):
        cause = "rate"
    elif http and http >= 500:
        cause = "provider"
    elif category == "network":
        cause = "network"
    elif category == "dependency" or re.search(r"modulenotfound|unknown option|unrecognized option|no such option", detail):
        cause = "dependency"
    elif re.search(r"jsondecode|réponse .*invalide|malformed json|signature extraction failed|unexpected response", detail):
        cause = "api"
    elif stage == "strawberry":
        cause = "integration"
    elif category == "audio_unavailable" or "requested format is not available" in detail:
        cause = "format"
    elif category == "playlist":
        cause = "playlist"
    elif re.search(r"video unavailable|private video|piste introuvable|aucun fichier pour cette piste", detail):
        cause = "track"
    elif "édition" in detail or stage == "catalogue" and category == "path":
        cause = "edition"
    elif "metadata" in detail or "métadonnée" in detail:
        cause = "metadata"
    elif stage == "ffmpeg" or "ffmpeg" in detail or entry.get("tool") == "ffmpeg":
        cause = "ffmpeg"
    elif stage == "tri":
        cause = "sort"
    elif stage == "paroles":
        cause = "lyrics"
    after = re.search(r"retry-after\s*[:=]\s*([0-9]{1,6})\b", detail)
    positions = re.search(r"\bpiste\s+(\d{1,4})\b", stage_text)
    tracks = (f"position {positions[1]}",) if positions else ()
    code = entry.get("returncode")
    if local_input:
        # Local paths and archive member names are confined to the private journal.
        return Failure(cause, stage, LOCAL_PROVIDER, "", http, None, 0, (), "", "")
    return Failure(cause, stage, provider, source, http, code if type(code) is int and -255 <= code <= 255 else None,
                   min(86400, int(after[1])) if after else 0, tracks, label(outcome.get("artist")),
                   label(outcome.get("title")))


def signatures(failure, versions):
    # An unavailable release is release-specific. Tool/API regressions are
    # shared across albums running the same tool versions and pipeline stage.
    album = import_key(failure.url) if failure.cause in BEARING else ()
    return encoded_key(failure.cause, failure.stage, failure.provider, failure.http, versions, album)


def tool_versions():
    result = {}
    for tool, args in (("yt-dlp", ("--ignore-config", "--version")), ("ffmpeg", ("-version",)),
                       ("Strawberry", ("-Q", "strawberry"))):
        executable = shutil.which("pacman" if tool == "Strawberry" else tool)
        if not executable:
            result[tool] = "absent"
            continue
        try:
            completed = subprocess.run((executable, *args), capture_output=True, timeout=2, text=True)
        except (OSError, subprocess.SubprocessError):
            result[tool] = "indisponible"
            continue
        match = VERSION.search(completed.stdout[:400])
        result[tool] = match[0] if match else "inconnue"
    return result


def rendition(url):
    """Only accepted public YouTube identifiers, with no arbitrary options."""
    if not isinstance(url, str) or len(url) > 300 or any(c.isspace() for c in url):
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.netloc not in YTHOSTS or parsed.fragment:
        return None
    kind, identity = import_key(url)
    allowed = {"album": r"MPRE[A-Za-z0-9_-]{5,120}", "playlist": r"[A-Za-z0-9_-]{10,120}",
               "video": r"[A-Za-z0-9_-]{11}"}
    if kind not in allowed or not re.fullmatch(allowed[kind], identity):
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    paths = {"album": {f"/browse/{identity}"}, "playlist": {"/playlist"},
             "video": {"/watch", f"/{identity}"}}
    expected = {"album": {}, "playlist": {"list": [identity]}, "video": ({"v": [identity]} if parsed.path == "/watch" else {})}
    if params != expected[kind] or parsed.path not in paths[kind]:
        return None
    return kind, identity


class ImportDiagnostics:
    def __init__(self, maintenance, queue, *, versions=tool_versions):
        self.control = maintenance
        self.queue = queue
        self.index = maintenance.index
        self.versions = versions
        self.known_versions = {}
        self.checked = 0.
        self.next_poll = 0.

    def collect(self, row):
        terminal = json.loads(row["terminal"]) if row.get("terminal") else {}
        if terminal.get("interrupted"):
            return [Failure("interrupted", "import", "Local", "", None, None, 0, (), "", "")]
        caught = {}
        for value in self.queue.diagnosis_outcomes(row["id"]):
            for entry in value.get("diagnostics", [])[:64]:
                failure = classify(entry, value)
                # Keep one representative per cause and stage. Preserve the
                # longest provider delay even if it came from a later track.
                key = failure.cause, failure.stage
                previous = caught.get(key)
                if previous is None or failure.retry_after > previous.retry_after:
                    caught[key] = failure
        if caught:
            return list(caught.values())
        target = row["targets"][0] if row["targets"] else ("", "")
        return [classify({**terminal, "stage": "import"}, {"title": target[0], "url": target[1]})]

    def log(self, message, severity="INFO"):
        self.control.journal.emit("DOWNLOAD", message, severity)

    def decide(self, row, signature, phase, message, due=0., **kwargs):
        if self.queue.recover_import(row, signature, phase, message, due, **kwargs):
            self.log(message, "WARNING" if phase == "review" else "INFO")
            self.control.revision += 1
            return True
        return False

    def start_research(self, signature, failure, row, findings):
        result = self.index.enqueue("codex", signature, signature, 39, {
            "research_id": signature, "kind": "import_failure", "artist": failure.artist,
            "album": failure.title, "title": "", "tracks": sorted({position for f in findings for position in f.tracks}),
            "stage": failure.stage, "failure": failure.cause, "provider": failure.provider,
            "logs": [LABELS[failure.cause]], "http_status": failure.http, "return_code": failure.returncode,
            "versions": self.known_versions, "classic_retries": row["classic"],
            "existing_audio_preserved": row["state"] == "partial",
        })
        if result:
            self.control.journal.emit("CODEX", f"Diagnostic mutualisé en file · {LABELS[failure.cause]}")

    def status(self, signature):
        with self.index.db() as db:
            task = db.execute("SELECT state FROM tasks WHERE kind='codex' AND subject=?", (signature,)).fetchone()
            item = db.execute("SELECT value FROM reviews WHERE kind='codex' AND subject=?", (signature,)).fetchone()
        return task[0] if task else "", json.loads(item[0]) if item else None

    def cooldown(self, row, sig, now):
        with self.queue.connect(write=True) as db:
            recent = db.execute("""SELECT count(*) FROM import_recovery
                                  WHERE signature=? AND job!=? AND due>?""", (sig, row["id"], now - HOUR)).fetchone()[0]
            if recent < 3:
                return 0
            seconds = 30 * 60
            db.execute("UPDATE import_recovery SET due=max(due,?) WHERE signature=? AND phase='retry_wait'",
                       (now + seconds, sig))
        self.log("Relances automatiques suspendues temporairement pour cette cause commune", "WARNING")
        return seconds

    def resolve(self, row, signature, failure, data, now):
        resolution = data.get("resolution", {})
        if (resolution.get("action") != "catalogue" or row["remedial"] or not failure.url
                or data.get("status") != "found" or data.get("confidence") != "high" or data.get("contradictions")):
            self.decide(row, signature, "review", f"À vérifier · {LABELS[failure.cause]}")
            return
        before = rendition(failure.url)
        target = resolution.get("candidate_url", "")
        after = rendition(target) if target else None
        # Normalizing a URL may remove obsolete routing, but the request
        # must keep the same approved catalogue identifier and edition.
        if not before or after != before or target == failure.url or after[0] not in ("album", "playlist"):
            self.decide(row, signature, "review", "À vérifier · la référence proposée exige une validation")
            return
        key = encoded_key(signature, before, target)
        with self.index.db() as db:
            validated = db.execute("SELECT 1 FROM reviews WHERE kind='import_remedy' AND subject=? AND state='verified'",
                                   (key,)).fetchone()
        if not validated:
            try:
                code, raw = self.control.output(catalogue_command("preparer", target, str(self.index.root)), timeout=50)
                payload = json.loads(raw)
                verified = payload.get("data") or {}
                if code or not verified.get("tracks") or not verified.get("title"):
                    raise ValueError("édition non confirmée")
            except (Interrupted, OSError, ValueError, AttributeError, subprocess.SubprocessError):
                self.decide(row, signature, "review", "À vérifier · la fiche proposée n’a pas été confirmée")
                return
            self.index.review("import_remedy", key, "verified", {"action": "catalogue", "target": target, "signature": signature})
            self.log("Correction de référence confirmée dans le catalogue")
        self.decide(row, signature, "verified", "Nouvelle tentative avec la référence vérifiée",
                    replacement=(failure.url, target))

    def tick(self, settings, now=None):
        now = time() if now is None else now
        if now < self.next_poll:
            return
        self.next_poll = now + 5
        for row in self.queue.diagnosis_candidates(now):
            if row["state"] in ("done", "cancelled"):
                self.decide(row, row["signature"] or "", row["state"],
                            "Import résolu et intégré" if row["state"] == "done" else "Import interrompu")
                continue
            if not self.known_versions or now > self.checked + 4 * HOUR:
                self.known_versions, self.checked = self.versions(), now
            findings = self.collect(row)
            # A local/authorization gate must take precedence over retries.
            order = {"interrupted": 0, "space": 1, "permissions": 2, "authorization": 3, "local_file": 4,
                     "rate": 5, "provider": 6, "network": 7}
            failure = min(findings, key=lambda f: (order.get(f.cause, 8), f.stage))
            signature = signatures(failure, self.known_versions)
            if row["recovery_phase"] not in ("retry_wait", "codex_wait"):
                self.log(f"Échec · {LABELS[failure.cause]} · {failure.stage}", "WARNING")
            if failure.cause in LOCAL or row["remedial"]:
                self.decide(row, signature, "review", f"À vérifier · {LABELS[failure.cause]}")
            elif failure.cause in TEMPORARY and row["classic"] < 2:
                if row["recovery_phase"] == "retry_wait" and row["signature"] == signature:
                    self.decide(row, signature, "classic", f"Relance classique {row['classic']+1}/2 · reprise des pistes",
                                due=now)
                else:
                    delay = max(failure.retry_after, 60 * 4**row["classic"], self.cooldown(row, signature, now))
                    self.decide(row, signature, "retry_wait", f"{LABELS[failure.cause]} · relance différée", now + delay)
            elif (failure.cause == "rate" or not (settings["codex"] and settings["codex_auto"])
                  or "import_failure" not in settings["codex_categories"]):
                self.decide(row, signature, "review", f"À vérifier · {LABELS[failure.cause]}")
            else:
                task, result = self.status(signature)
                if result:
                    self.resolve(row, signature, failure, result, now)
                elif row["recovery_phase"] == "codex_wait" and row["signature"] == signature:
                    self.decide(row, signature, "review", "À vérifier · diagnostic approfondi en attente ou indisponible")
                elif task in ("review", "excluded"):
                    self.decide(row, signature, "review", "À vérifier · diagnostic approfondi sans correction vérifiée")
                else:
                    if not task:
                        self.start_research(signature, failure, row, findings)
                    self.decide(row, signature, "codex_wait", "Échec regroupé · diagnostic Codex en attente", now + 300)
