"""Local, bounded Kara diagnostics. Only explicit event fields enter the journal."""
from collections import Counter
from datetime import datetime, timezone
from fcntl import LOCK_EX, LOCK_NB, flock
from hashlib import file_digest, sha256
import importlib.metadata
import json
import os
from pathlib import Path
from queue import Empty, Full, Queue
import re
import sys
import shutil
import struct
from subprocess import DEVNULL, PIPE, Popen, SubprocessError, check_output
from threading import Event, Lock, Thread
import time
from urllib.parse import unquote
import wave
from uuid import uuid4

from _kara_paths import BIN, STATE

ROOT = STATE / "kara/blackbox"
FORMAT = 1
SESSION = re.compile(r"session-[0-9]{8}T[0-9]{6}-[a-f0-9]{12}")
MAX_EVENT = 65536
DEFAULT_BYTES = 128 * 1024 * 1024
DEFAULT_VOICE_BYTES = 16 * 1024 ** 3
MIN_FREE = 32 * 1024 * 1024
AUDIO_SEGMENT_BYTES = 16 * 1024 * 1024
GRAPH_TRACKS = ("monitor", "witness")
GRAPH_CLOCK_FIELDS = ("driver", "frame", "monotonic_ns", "xrun", "quantum", "present",
                      "spa_headers", "route", "epoch")
TRACK_NAME = re.compile(r"[a-f0-9]{16}-(?:wifi|whisper|return|monitor|witness)-[0-9]{4}\.wav")
FLOAT_WAV = struct.Struct("<4sI4s4sIHHIIHH4sII4sI")
INCIDENTS = {"error", "disconnect", "interrupted", "capture-gap", "overflow",
             "partial", "xrun", "write-error", "marker", "tv-timeout", "clipping"}
SECRET_FIELDS = {"authorization", "proxyauthorization", "cookie", "cookies", "setcookie",
                 "password", "passwd", "secret", "token", "key", "admincode", "mobileid",
                 "apikey", "privatekey", "credential", "credentials", "sessioncookie",
                 "clientsecret", "consumerkey", "applicationsecret", "applicationkey",
                 "accesstoken", "refreshtoken", "credentialtoken", "dnsauth", "sshkey",
                 "email", "ipaddress", "macaddress", "deviceid", "serialnumber"}
SECRET_TEXT = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|\b(?:bearer|basic)\s+[A-Za-z0-9_+./=-]{8,}"
    r"|(?:^|[\s?&;{,])[\"']?(?:[\w-]*(?:token|password|secret|credential)|(?:set-)?cookie"
    r"|api[-_]?key|authorization)[\"']?\s*[:=]"
    r"|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+",
    re.IGNORECASE)
HARDWARE_MAC = re.compile(r"(?i)(?<![0-9a-f])(?:[0-9a-f]{2}[:_]){5}[0-9a-f]{2}(?![0-9a-f])")
SECRET_SUFFIXES = ("apikey", "applicationsecret", "applicationkey", "consumerkey",
                   "privatekey", "accesstoken", "refreshtoken", "credentialtoken")


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def sync_directory(path):
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_text(path, text):
    draft = path.with_name("." + path.name + ".tmp")
    with os.fdopen(os.open(draft, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                   "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    draft.replace(path)
    sync_directory(path.parent)


def atomic_json(path, value):
    atomic_text(path, json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


def events(path):
    with path.open("rb") as stream:
        for row in iter(lambda: stream.readline(MAX_EVENT + 1), b""):
            if len(row) > MAX_EVENT or not row.endswith(b"\n"):
                raise ValueError("Événement incomplet ou trop long.")
            item = json.loads(row)
            if not isinstance(item, dict):
                raise ValueError("Événement invalide.")
            yield item


def session_folders(root):
    if not root.is_dir():
        return []
    folders = [path for path in root.iterdir()
               if SESSION.fullmatch(path.name) and not path.is_symlink() and path.is_dir()]
    return sorted(folders, key=lambda path: (path.stat().st_mtime_ns, path.name))


def recover_events(path):
    # A killed writer may leave one partial line. Keep all preceding
    # complete records and record the interrupted status in the manifest.
    if not path.exists():
        path.touch(mode=0o600)
    with path.open("rb+") as stream:
        valid_end = 0
        while row := stream.readline(MAX_EVENT + 1):
            try:
                if len(row) > MAX_EVENT or not row.endswith(b"\n"):
                    break
                if not isinstance(json.loads(row), dict):
                    break
            except (UnicodeError, ValueError):
                break
            valid_end = stream.tell()
        stream.truncate(valid_end)
        os.fsync(stream.fileno())


def finish_manifest(folder, manifest, *, reason, dropped, error=None):
    log = folder / "events.jsonl"
    recover_events(log)
    by_component, by_event = Counter(), Counter()
    acoustic = {}
    with os.fdopen(os.open(folder / "incidents.jsonl", os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600),
                   "w", encoding="utf-8") as incidents:
        for item in events(log):
            by_component[item["component"]] += 1
            by_event[item["event"]] += 1
            data, turn = item["data"] if "data" in item else {}, item.get("turn")
            if item["component"] == "acoustic" and item["event"] == "tap":
                record = acoustic.setdefault(turn, {}).setdefault(data["track"], {
                    "frames": 0, "drivers": [], "states": [], "epochs": 0})
                state = data["state"]
                if state == "requested":
                    record["epochs"] += 1
                if state not in record["states"]:
                    record["states"].append(state)
                if data.get("reason") and "reason" not in record:
                    record["reason"] = data["reason"]
                if data.get("missing_frames"):
                    record["missing_frames"] = record.get("missing_frames", 0) + data["missing_frames"]
            elif item["event"] == "pcm" and data.get("track") in GRAPH_TRACKS:
                record = acoustic.setdefault(turn, {}).setdefault(data["track"], {
                    "frames": 0, "drivers": [], "states": [], "epochs": 0})
                record["frames"] += data["frames"]
                driver = data["clock"]["driver"]
                if driver not in record["drivers"]:
                    record["drivers"].append(driver)
            if item["event"] in INCIDENTS:
                incidents.write(json.dumps({"event_id": item["id"], "kind": item["event"],
                                            "component": item["component"], "time": item["monotonic_ns"],
                                            "evidence": "events.jsonl"}, ensure_ascii=False) + "\n")
        incidents.flush()
        os.fsync(incidents.fileno())
    complete = not (dropped or error or reason == "interrupted")
    summary = {"session": manifest["session"], "complete": complete, "reason": reason, "dropped": dropped,
               "error": error, "events": sum(by_event.values()), "by_component": dict(by_component),
               "by_event": dict(by_event), "incidents": sum(by_event[kind] for kind in INCIDENTS)}
    atomic_json(folder / "summary.json", summary)
    atomic_text(folder / "REPORT.md",
                f"# Diagnostic Kara {manifest['session']}\n\n"
                f"- Fin de collecte : {reason}\n- Journal complet : {'oui' if complete else 'non'}\n"
                f"- Événements : {summary['events']}\n- Incidents et marqueurs : {summary['incidents']}\n"
                f"- Événements perdus : {dropped}\n\n"
                "Les identifiants de preuve figurent dans `incidents.jsonl`.\n")
    names = ["events.jsonl", "incidents.jsonl", "summary.json", "REPORT.md"]
    for path in sorted((folder / "audio").glob("*.wav")):
        if not TRACK_NAME.fullmatch(path.name) or path.is_symlink():
            continue
        if reason == "interrupted" and not recover_wave(path):
            continue
        names.append(f"audio/{path.name}")
    manifest.update(status="complete" if complete else "incomplete", ended=now(),
                    files={name: {"sha256": digest(folder / name), "bytes": (folder / name).stat().st_size}
                           for name in names})
    if manifest.get("capture", {}).get("voice"):
        for tracks in acoustic.values():
            for name in GRAPH_TRACKS:
                track = tracks.setdefault(name, {"frames": 0, "drivers": [], "states": [], "epochs": 0})
                track["coverage"] = ("partial" if track["frames"] and (
                    "unavailable" in track["states"] or track.get("missing_frames"))
                                     else "recorded" if track["frames"] else "unavailable")
        manifest["capture"]["acoustic"] = {"turns": acoustic, "clock": "pipewire_host_monotonic",
                                           "cross_device_calibrated": False, "hardware_bound_ns": None}
    # Publish the terminal manifest last. Its hashes cover the final report
    # and indexes so readers never mistake an interrupted write for success.
    atomic_json(folder / "manifest.json", manifest)
    return summary


def provenance():
    base = BIN.parent
    info = {"versions": {"python": sys.version.split()[0]}, "platform": os.uname().release}
    for package in ("PyGObject", "aiohttp", "numpy", "scipy"):
        try:
            info["versions"][package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            pass
    for component, arguments in (("pipewire", ("pipewire", "--version")), ("ffmpeg", ("ffmpeg", "-version")),
                                 ("openssl", ("openssl", "version"))):
        try:
            lines = check_output(arguments, timeout=2, stderr=DEVNULL).decode(errors="replace").splitlines()
            if lines:
                info["versions"][component] = " · ".join(lines[:3] if component == "pipewire" else lines[:1])[:256]
        except (OSError, SubprocessError):
            continue
    try:
        for field, args in (("commit", ("rev-parse", "HEAD")),
                            ("branch", ("rev-parse", "--abbrev-ref", "HEAD"))):
            info[field] = check_output(("git", "-C", base, *args), timeout=3, stderr=DEVNULL).decode().strip()
        # Hash the names and contents of untracked files as well as tracked
        # modifications. The journal contains the digest, never source text.
        with Popen(("git", "-C", base, "diff", "--binary", "HEAD", "--"),
                   stdout=PIPE, stderr=DEVNULL) as process:
            content = file_digest(process.stdout, "sha256")
            if process.wait(timeout=3):
                return info
        names = check_output(("git", "-C", base, "ls-files", "--others", "--exclude-standard", "-z"),
                             timeout=3, stderr=DEVNULL)
        for name in sorted(filter(None, names.split(b"\0"))):
            path = base / os.fsdecode(name)
            if path.is_symlink():
                payload = os.fsencode(os.readlink(path))
            elif path.is_file():
                payload = digest(path).encode()
            else:
                continue
            content.update(b"\0" + name + b"\0" + sha256(payload).digest())
        info["dirty_sha256"] = content.hexdigest()
    except (OSError, SubprocessError):
        pass
    return info


def sensitive_field(key):
    name = re.sub(r"[^a-z0-9]", "", key.casefold())
    return name in SECRET_FIELDS or name.endswith(SECRET_SUFFIXES)


def clean(data):
    if isinstance(data, dict):
        return {key: clean(value) for key, value in data.items() if not sensitive_field(key)}
    if isinstance(data, (tuple, list)):
        return [clean(item) for item in data]
    if isinstance(data, str):
        if SECRET_TEXT.search(data) or "%" in data and SECRET_TEXT.search(unquote(data)):
            return "[secret masqué]"
        return HARDWARE_MAC.sub("adresse-masquée", data)
    return data


def system_snapshot():
    result = {"load": os.getloadavg()}
    try:
        lines = Path("/proc/meminfo").read_text().splitlines()
        result["memory_kib"] = {key: int(value.split()[0]) for line in lines
                                for key, _, value in [line.partition(":")]
                                if key in ("MemTotal", "MemAvailable", "SwapTotal", "SwapFree")}
        result["writer_cpu_s"] = time.thread_time()
    except (OSError, ValueError):
        pass
    for label, paths, units in (
        ("cpu_temp_c", Path("/sys/class/thermal").glob("thermal_zone*/temp"), 1000),
        ("gpu_percent", Path("/sys/class/drm").glob("card[0-9]/device/gpu_busy_percent"), 1),
        ("cpu_mhz", Path("/sys/devices/system/cpu/cpufreq").glob("policy[0-9]/scaling_cur_freq"), 1000),
    ):
        values = []
        for path in sorted(paths)[:12]:
            try:
                values.append(int(path.read_text().strip()) / units)
            except (OSError, ValueError, KeyError):
                continue
        if values:
            result[label] = values
    try:
        result["io_bytes"] = {key: int(value.strip()) for row in Path("/proc/self/io").read_text().splitlines()
                              for key, _, value in [row.partition(":")] if key in ("read_bytes", "write_bytes")}
    except (OSError, ValueError):
        pass
    return result


class BlackBox:
    def __init__(self, root=ROOT, *, max_bytes=None, reserve=MIN_FREE, queue_size=512):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.reserve = reserve
        self.queue_size = queue_size
        self.lock = Lock()
        self.session = None
        self.last = None
        self.failure = None
        self.free_bytes = None
        self.verified = None

    def active(self):
        with self.lock:
            return self.session is not None and not self.session.stop.is_set()

    def status(self, *, storage=False):
        with self.lock:
            record = self.session
            status = {"active": bool(record and not record.stop.is_set()),
                    "finalizing": bool(record and record.stop.is_set()),
                    "session": record.identity if record else None,
                    "dropped": record.dropped if record else 0,
                    "latest": self.last, "error": self.failure}
            if storage:
                status["free_bytes"] = self.free_bytes
                status["verified"] = self.verified
                status["voice"] = bool(record and record.manifest["capture"]["voice"])
                status["unavailable"] = sorted(record.unavailable) if record else []
            return status

    def voice_session(self, track=None):
        with self.lock:
            record = self.session
            if (record and not record.stop.is_set() and record.manifest["capture"]["voice"]
                    and (track is None or track in record.tracks)):
                return record.identity

    def disk_available(self):
        path = self.root
        while not path.exists() and path.parent != path:
            path = path.parent
        try:
            self.free_bytes = shutil.disk_usage(path).free
        except OSError:
            self.free_bytes = None
        return self.free_bytes

    def recover(self):
        if not self.root.is_dir():
            return
        with open(self.root / ".writer.lock", "a", opener=private_file) as lock:
            flock(lock, LOCK_EX | LOCK_NB)
            self._recover()

    def _recover(self):
        for folder in session_folders(self.root):
            manifest = folder / "manifest.json"
            try:
                data = json.loads(manifest.read_text())
                if data.get("status") == "recording":
                    finish_manifest(folder, data, reason="interrupted", dropped=None)
                self.last = json.loads((folder / "summary.json").read_text())
            except (OSError, ValueError):
                continue

    def start(self, *, consent=False, voice=False, configuration=None, tracks=None):
        if consent is not True:
            raise ValueError("Confirme l’accord des participants et des responsables des mineurs.")
        allowed = {"wifi", "whisper", "return", *GRAPH_TRACKS}
        if tracks is not None and (not isinstance(tracks, list) or len(tracks) > len(allowed)
                                   or any(not isinstance(track, str) or track not in allowed for track in tracks)):
            raise ValueError("Pistes BlackBox invalides.")
        selected = sorted(allowed if tracks is None else set(tracks)) if voice is True else []
        with self.lock:
            if self.session is not None:
                return
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.root.is_symlink() or self.root.stat().st_uid != os.getuid() or self.root.stat().st_mode & 0o077:
                raise OSError("Le dossier BlackBox doit être privé (0700).")
            owner = open(self.root / ".writer.lock", "a", opener=private_file)
            try:
                flock(owner, LOCK_EX | LOCK_NB)
                if (self.disk_available() or 0) < self.reserve + 65536:
                    raise OSError("Espace disque insuffisant pour BlackBox.")
                identity = datetime.now(timezone.utc).strftime("session-%Y%m%dT%H%M%S-") + uuid4().hex[:12]
                folder = self.root / identity
                folder.mkdir(mode=0o700)
                manifest = {"format": FORMAT, "session": identity, "status": "recording",
                            "started": now(), "monotonic_ns": time.monotonic_ns(),
                            "capture": {"voice": bool(selected), "tracks": selected},
                            "configuration": clean(configuration or {}),
                            "quota_bytes": self.max_bytes if self.max_bytes is not None else
                                (DEFAULT_VOICE_BYTES if voice is True else DEFAULT_BYTES)}
                atomic_json(folder / "manifest.json", manifest)
                record = Recording(self, folder, manifest, owner)
                self.session = record
                self.failure = None
                self.verified = None
                record.thread.start()
            except BaseException:
                owner.close()
                self.session = None
                raise

    def stop(self, reason="manual", *, wait=False):
        with self.lock:
            record = self.session
            if record is not None and not record.stop.is_set():
                record.reason = reason
                record.stop.set()
        if wait and record is not None:
            record.thread.join(timeout=8)

    def emit(self, component, event, *, turn=None, player=None, song=None, position=None,
             recording_session=None, **data):
        # The writer alone serializes and touches disk. Producers can drop a
        # bounded record and expose loss without waiting on the writer.
        with self.lock:
            record = self.session
            if (record is None or record.stop.is_set()
                    or recording_session is not None and recording_session != record.identity):
                return
            if component == "acoustic" and event == "tap" and data.get("state") == "unavailable":
                record.unavailable.add(data["track"])
            record.sequence += 1
            item = record.item(component, event, player, song, turn, position, data)
            try:
                record.pending.put_nowait(item)
            except Full:
                record.dropped += 1

    def pcm(self, track, rate, channels, block, *, turn, player, song, sequence=None, capture_end=None,
            clock=None, recording_session=None, packet_timing=None):
        # Explicit voice consent is independent of metadata collection. This
        # admission check lives before the copy and never performs file I/O.
        if track not in ("wifi", "whisper", "return", *GRAPH_TRACKS) or not re.fullmatch(r"[a-f0-9]{16}", turn):
            return
        graph = track in GRAPH_TRACKS
        width = 4 if graph else 2
        if (rate not in (16000, 44100, 48000) or channels not in (1, 2)
                or not 0 < len(block) <= (65536 if graph else 4096) or len(block) % (channels * width)):
            return
        if graph and (not clock or not recording_session or rate != 48000
                      or channels != (2 if track == "monitor" else 1)
                      or any(type(clock.get(key)) is not int for key in GRAPH_CLOCK_FIELDS)
                      or len(block) != clock["quantum"] * channels * width):
            return
        if (sequence is not None and type(sequence) is not int
                or capture_end is not None and type(capture_end) not in (int, float)):
            return
        with self.lock:
            record = self.session
            if (record is None or record.stop.is_set() or track not in record.tracks
                    or recording_session is not None and recording_session != record.identity):
                return
            # The per-packet schema only contains typed numeric clock fields
            # and generated identifiers. Cache the already-scrubbed labels
            # once per turn so routine PCM never scans its metadata for secrets.
            if turn not in record.pcm_labels:
                record.pcm_labels[turn] = clean(player), clean(song)
            player, song = record.pcm_labels[turn]
            record.sequence += 1
            item = record.item("audio", "pcm", player, song, turn, None,
                               {"track": track, "rate": rate, "channels": channels,
                                "sequence": sequence, "capture_end_s": capture_end})
            if graph:
                item["data"].update(clock={key: clock[key] for key in GRAPH_CLOCK_FIELDS}, sample_format="f32le")
            if packet_timing is not None and track in ("wifi", "whisper"):
                item["data"]["packet_timing"] = {
                    key: packet_timing[key] for key in ("frame_end", "send_ms", "received_s")
                    if type(packet_timing.get(key)) in (int, float)
                }
            try:
                record.pending.put_nowait((item, bytes(block)))
            except Full:
                record.dropped += 1


def private_file(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW, 0o600)


def float_header(rate, channels, size):
    # IEEE float WAV includes the fact sample count. Byte sizes and frame
    # counts are explicit little endian, independent of the host ABI.
    return FLOAT_WAV.pack(b"RIFF", FLOAT_WAV.size - 8 + size, b"WAVE", b"fmt ", 16, 3,
                          channels, rate, rate * channels * 4, channels * 4, 32, b"fact", 4,
                          size // (4 * channels), b"data", size)


def wave_layout(path):
    with open(path, "rb", opener=private_file) as stream:
        raw = stream.read(FLOAT_WAV.size)
        if len(raw) == FLOAT_WAV.size and raw[20:22] == b"\x03\x00":
            parts = FLOAT_WAV.unpack(raw)
            channels, rate, frames, size = parts[6], parts[7], parts[13], parts[15]
            if (channels not in (1, 2) or rate != 48000 or size != 4 * channels * frames
                    or raw != float_header(rate, channels, size)
                    or path.stat().st_size != FLOAT_WAV.size + size):
                raise ValueError("En-tête ou taille WAV flottant invalide.")
            return rate, channels, frames, 4, FLOAT_WAV.size
        stream.seek(0)
        with wave.open(stream, "rb") as source:
            return source.getframerate(), source.getnchannels(), source.getnframes(), source.getsampwidth(), 44


def recover_wave(path):
    # Our PCM writer uses the fixed 44-byte RIFF header. A crash can leave
    # provisional chunk sizes and one partial frame.
    with open(path, "rb+", opener=private_file) as stream:
        header = stream.read(FLOAT_WAV.size)
        if len(header) == FLOAT_WAV.size and header[20:22] == b"\x03\x00":
            parts = FLOAT_WAV.unpack(header)
            channels, rate = parts[6:8]
            if channels not in (1, 2) or rate != 48000 or parts[11:13] != (b"fact", 4):
                return False
            size = path.stat().st_size - FLOAT_WAV.size
            size -= size % (4 * channels)
            stream.truncate(FLOAT_WAV.size + size)
            stream.seek(0)
            stream.write(float_header(rate, channels, size))
            stream.flush()
            os.fsync(stream.fileno())
            return True
        header = header[:44]
        if len(header) != 44 or header[:4] != b"RIFF" or header[36:40] != b"data":
            return False
        alignment, = struct.unpack_from("<H", header, 32)
        if alignment not in (2, 4):
            return False
        payload = path.stat().st_size - 44
        payload -= payload % alignment
        stream.truncate(44 + payload)
        stream.seek(4)
        stream.write(struct.pack("<I", 36 + payload))
        stream.seek(40)
        stream.write(struct.pack("<I", payload))
        stream.flush()
        os.fsync(stream.fileno())
    return True


class Segment:
    def __init__(self, path, rate, channels, *, floating=False):
        self.path, self.rate, self.channels = path, rate, channels
        self.size = 0
        self.file = open(path, "wb", opener=private_file)
        self.width = 4 if floating else 2
        self.writer = None if floating else wave.open(self.file, "wb")
        if self.writer is None:
            self.file.write(float_header(rate, channels, 0))
        else:
            self.writer.setparams((channels, 2, rate, 0, "NONE", "not compressed"))

    def write(self, block):
        offset = self.size // (self.width * self.channels)
        if self.writer is None:
            self.file.write(block)
        else:
            self.writer.writeframesraw(block)
        self.size += len(block)
        return offset

    def close(self):
        try:
            if self.writer is None:
                self.file.seek(0)
                self.file.write(float_header(self.rate, self.channels, self.size))
            else:
                self.writer.close()
            self.file.flush()
            os.fsync(self.file.fileno())
        finally:
            self.file.close()


def validate_recording(folder, manifest):
    if manifest["format"] != FORMAT or manifest["session"] != folder.name:
        raise ValueError("Manifeste incompatible.")
    if manifest["status"] == "recording":
        raise ValueError("La session est en cours ou doit être récupérée par Parole.")
    if not {"events.jsonl", "incidents.jsonl", "summary.json", "REPORT.md"} <= set(manifest["files"]):
        raise ValueError("Inventaire de session invalide.")
    audio = {}
    for name, entry in manifest["files"].items():
        path = folder / name
        if path.is_symlink():
            raise ValueError("Chemin de capture invalide.")
        if name not in ("events.jsonl", "incidents.jsonl", "summary.json", "REPORT.md"):
            if not name.startswith("audio/") or not TRACK_NAME.fullmatch(name.removeprefix("audio/")) or path.parent.is_symlink():
                raise ValueError("Chemin de capture invalide.")
            audio[name] = wave_layout(path)
        if path.stat().st_size != entry["bytes"] or digest(path) != entry["sha256"]:
            raise ValueError(f"Intégrité invalide pour {name}.")
    previous = 0
    graph_tail, gaps = {}, 0
    for item in events(folder / "events.jsonl"):
        if item["session"] != folder.name or item["id"] <= previous:
            raise ValueError("Identité ou ordre d’événement invalide.")
        if item["event"] == "pcm":
            data = item["data"]
            rate, channels, frames, width, _ = audio[data["file"]]
            graph = data["track"] in GRAPH_TRACKS
            if (data["rate"], data["channels"], width) != (rate, channels, 4 if graph else 2) or not (
                    0 <= data["sample"] < data["sample"] + data["frames"] <= frames):
                raise ValueError("Index PCM invalide.")
            if graph:
                clock = data["clock"]
                expected_planes = 6 if data["track"] == "monitor" else 1
                if (any(type(clock[k]) is not int or clock[k] < 0 for k in
                        ("driver", "epoch", "frame", "xrun", "monotonic_ns", "quantum", "present"))
                        or clock["quantum"] != data["frames"]
                        or clock["present"] & expected_planes != expected_planes):
                    raise ValueError("Horloge PipeWire invalide.")
                key = (item["turn"], data["track"], clock["epoch"])
                last = graph_tail.get(key)
                if last:
                    if (clock["driver"] != last["driver"] or clock["xrun"] != last["xrun"]
                            or clock["monotonic_ns"] <= last["monotonic_ns"]
                            or clock["frame"] < last["frame"] + last["quantum"]):
                        raise ValueError("Discontinuité d’horloge PipeWire dans une même fenêtre.")
                    gaps += clock["frame"] > last["frame"] + last["quantum"]
                graph_tail[key] = clock
        previous = item["id"]
    return {"valid": True, "status": manifest["status"], "session": folder.name, "graph_index_gaps": gaps}


class Recording:
    def __init__(self, manager, folder, manifest, owner):
        self.manager, self.folder, self.manifest, self.owner = manager, folder, manifest, owner
        self.identity = folder.name
        self.stop = Event()
        self.pending = Queue(maxsize=manager.queue_size)
        self.thread = Thread(target=self.write, name="kara-blackbox", daemon=True)
        self.reason = "interrupted"
        self.dropped = self.sequence = 0
        self.segments = {}
        self.pcm_labels = {}
        self.tracks = frozenset(manifest["capture"]["tracks"])
        self.unavailable = set()

    def item(self, component, event, player, song, turn, position, data):
        return {"id": self.sequence, "session": self.identity, "component": component, "event": event,
                "monotonic_ns": time.monotonic_ns(), "turn": turn, "player": player,
                "song": song, "position": position, "data": data}

    def append_pcm(self, item, block):
        data = item["data"]
        key = item["turn"], data["track"]
        number, segment = self.segments.get(key, (0, None))
        rate, channels = data["rate"], data["channels"]
        if not segment or segment.size + len(block) > AUDIO_SEGMENT_BYTES or (rate, channels) != (segment.rate, segment.channels):
            if segment:
                segment.close()
                number += 1
            audio = self.folder / "audio"
            audio.mkdir(mode=0o700, exist_ok=True)
            segment = Segment(audio / f"{key[0]}-{key[1]}-{number:04}.wav", rate, channels,
                              floating=key[1] in GRAPH_TRACKS)
            self.segments[key] = number, segment
        data.update(file=f"audio/{segment.path.name}", sample=segment.write(block),
                    frames=len(block) // (segment.width * channels))

    def close_turn(self, turn):
        for key in tuple(self.segments):
            if key[0] == turn:
                _, segment = self.segments.pop(key)
                segment.close()

    def write(self):
        summary, error, verified = None, None, None
        written = 0
        next_sample = 0.
        next_disk = 0.
        try:
            self.manifest["provenance"] = provenance()
            atomic_json(self.folder / "manifest.json", self.manifest)
            with open(self.folder / "events.jsonl", "ab", opener=private_file) as stream:
                while not self.stop.is_set() or not self.pending.empty():
                    clock = time.monotonic()
                    if clock >= next_disk:
                        if (self.manager.disk_available() or 0) < self.manager.reserve:
                            raise OSError("Réserve disque BlackBox atteinte.")
                        next_disk = clock + 2
                    if clock >= next_sample:
                        self.manager.emit("system", "sample", **system_snapshot())
                        next_sample = clock + 5
                    try:
                        item = self.pending.get(timeout=.20)
                    except Empty:
                        continue
                    pcm = isinstance(item, tuple)
                    if pcm:
                        item, block = item
                        if written + len(block) + 1024 > self.manifest["quota_bytes"]:
                            raise OSError("Limite de taille BlackBox atteinte.")
                        self.append_pcm(item, block)
                        written += len(block) + 44
                    if item["event"] == "turn-end":
                        self.close_turn(item["turn"])
                    try:
                        raw = (json.dumps(item if pcm else clean(item), ensure_ascii=False, allow_nan=False,
                                          separators=(",", ":")) + "\n").encode()
                    except (ValueError, TypeError, RecursionError):
                        raw = b""
                    if not raw or len(raw) > MAX_EVENT:
                        with self.manager.lock:
                            self.dropped += 1
                        continue
                    if written + len(raw) > self.manifest["quota_bytes"]:
                        raise OSError("Limite de taille BlackBox atteinte.")
                    stream.write(raw)
                    written += len(raw)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            error = exc.strerror or str(exc)
            with self.manager.lock:
                self.dropped += self.pending.qsize() + 1
                self.stop.set()
                self.reason = "write-error"
        try:
            for turn in {key[0] for key in self.segments}:
                self.close_turn(turn)
        except (OSError, ValueError) as exc:
            error = error or str(exc)
        try:
            summary = finish_manifest(self.folder, self.manifest, reason=self.reason,
                                      dropped=self.dropped, error=error)
            # Use the same streaming integrity checks as the CLI with the
            # closed segments and published inventory.
            validate_recording(self.folder, self.manifest)
            verified = True
        except (OSError, ValueError, KeyError) as exc:
            verified = False
            error = error or f"Finalisation impossible ({type(exc).__name__})."
        finally:
            self.owner.close()
            with self.manager.lock:
                self.manager.last = summary or self.manager.last
                self.manager.failure = error
                self.manager.verified = verified
                self.manager.session = None
