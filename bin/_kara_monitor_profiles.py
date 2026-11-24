"""Read and maintain the private, per-output Kara return settings."""
import argparse
from dataclasses import asdict, dataclass
import fcntl
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile

PROFILE_PATH = Path.home() / ".local/state/kara/micro-test/monitor-profiles.json"
# Block durations are also the wire contract. Queue capacities are in blocks.
PROFILES = {"stable": (20, 4), "balanced": (10, 4), "low-latency": (10, 2)}
DEFAULTS = {"profile": "balanced", "volume_percent": 50, "mic_gain_percent": 55,
            "echo_guard": True, "chrome_echo": True, "chrome_noise": True,
            "pulse_buffer": "standard", "experimental": True}
MAX_BYTES = 16 * 1024
ALSA_NODE = r"alsa_output\.[\w.:-]{1,180}"
ALSA_PORT = r"[\w.:-]{1,128}"


@dataclass(frozen=True)
class MixTrial:
    output: str
    profile: str
    experimental: bool
    drive: float
    ceiling: int


# These are opt-in calibration comparisons. They never replace the profile
# stored for a device. The ceilings bound the return while comparing gain
# and buffering separately, with the original acoustic guard still active.
MIX_TRIALS = {
    "vivobook-reference": MixTrial("analog", "low-latency", False, 1., 60),
    "vivobook-retour-haut": MixTrial("analog", "low-latency", False, 1., 85),
    "vivobook-voix-2": MixTrial("analog", "low-latency", False, 2., 75),
    "vivobook-voix-3": MixTrial("analog", "low-latency", False, 3., 70),
    "vivobook-file-souple": MixTrial("analog", "balanced", False, 2., 75),
    "edenwood-sbc-prudent": MixTrial("default", "balanced", True, 1., 70),
    "edenwood-sbc-voix": MixTrial("default", "balanced", True, 3., 58),
    "edenwood-sbc-natif": MixTrial("default", "balanced", False, 3., 58),
}


def device_key(sink, port=None):
    # A2DP suffixes can change on reconnect. One ALSA node can drive both its
    # speakers and its jack, so a valid ALSA identity also needs an active port.
    bluetooth = re.fullmatch(r"bluez_output\.([0-9a-f]{2}(?:_[0-9a-f]{2}){5})(?:\..+)?", sink, re.I)
    if bluetooth:
        return "bluetooth:" + bluetooth[1].upper().replace("_", ":")
    if (re.fullmatch(ALSA_NODE, sink, flags=re.ASCII) and isinstance(port, str)
            and re.fullmatch(ALSA_PORT, port, flags=re.ASCII)):
        return f"alsa:{sink}/{port}"
    return None


def pulse_sinks():
    raw = subprocess.check_output(
        ("pactl", "--format=json", "list", "sinks"), timeout=2, stderr=subprocess.DEVNULL)
    return json.loads(raw)


def output_details(sink):
    """Read the current PipeWire Pulse view of a concrete sink."""
    for entry in pulse_sinks():
        if entry["name"] == sink:
            return entry
    raise ValueError("Sortie audio indisponible")


def output_port(sink):
    port = output_details(sink).get("active_port")
    return port if isinstance(port, str) and re.fullmatch(ALSA_PORT, port, flags=re.ASCII) else None


def validate_settings(settings):
    if (not isinstance(settings, dict) or not set(DEFAULTS) <= settings.keys()
            or settings.keys() - DEFAULTS.keys() - {"turn_return_drive"}):
        raise ValueError("Champs du profil de retour invalides")
    profile = settings["profile"]
    voice = settings["mic_gain_percent"]
    volume = settings["volume_percent"]
    guard = settings["echo_guard"]
    buffer = settings["pulse_buffer"]
    drive = settings.get("turn_return_drive", 1.)
    if (not isinstance(profile, str) or profile not in PROFILES
            or buffer not in ("standard", "short") or (profile == "stable" and buffer == "short")
            or type(voice) is not int or not 0 <= voice <= 100 or voice % 5
            or type(drive) not in (float, int) or not 1 <= drive <= 3
            or any(type(settings[key]) is not bool for key in
                   ("echo_guard", "chrome_echo", "chrome_noise", "experimental"))
            or type(volume) is not int or not 50 <= volume <= (100 if guard else 180) or volume % 5):
        raise ValueError("Valeur du profil de retour hors limites")
    return settings


def read_profiles(path):
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
    except FileNotFoundError:
        return {}
    if len(raw) > MAX_BYTES:
        raise ValueError("Fichier de profils trop volumineux")
    data = json.loads(raw)
    if (not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] != 1
            or not isinstance(data.get("devices"), dict)):
        raise ValueError("Format des profils de retour invalide")
    profiles = data["devices"]
    for key, settings in profiles.items():
        if not (re.fullmatch(fr"alsa:{ALSA_NODE}/{ALSA_PORT}", key, flags=re.ASCII)
                or re.fullmatch(r"bluetooth:[0-9A-F]{2}(?::[0-9A-F]{2}){5}", key)):
            raise ValueError("Identité de profil invalide")
        validate_settings(settings)
    return profiles


def update_profile(path, sink, port, settings):
    """Atomically save or reset one output. The caller owns the chosen route."""
    key = device_key(sink, port)
    if key is None:
        raise ValueError("Identité stable de sortie indisponible")
    if settings is not None:
        validate_settings(settings)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Writers serialize the read/modify/replace transaction. Readers always see
    # either the old or complete new file. Never replace a corrupt input file.
    fd = os.open(path.with_suffix(".lock"), os.O_CREAT | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        profiles = read_profiles(path)
        if settings is None:
            if key not in profiles:
                return
            del profiles[key]
        else:
            if "turn_return_drive" not in settings and "turn_return_drive" in profiles.get(key, {}):
                settings = settings | {"turn_return_drive": profiles[key]["turn_return_drive"]}
            profiles[key] = settings
        content = json.dumps({"version": 1, "devices": profiles}, indent=2, sort_keys=True) + "\n"
        if len(content.encode()) > MAX_BYTES:
            raise ValueError("Fichier de profils trop volumineux")
        with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as staged:
            temporary = Path(staged.name)
            try:
                staged.write(content)
                staged.flush()
                os.fsync(staged.fileno())
                os.replace(temporary, path)
                dir_fd = os.open(path.parent, os.O_DIRECTORY | os.O_RDONLY)
                try:
                    os.fsync(dir_fd)
                finally:
                    os.close(dir_fd)
            finally:
                temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    operations = parser.add_subparsers(required=True, dest="operation")
    save = operations.add_parser("save")
    save.add_argument("sink")
    save.add_argument("--port", help="Port ALSA à vérifier avant l’enregistrement")
    save.add_argument("--profile", required=True, choices=PROFILES)
    save.add_argument("--volume", required=True, type=int)
    save.add_argument("--voice", required=True, type=int)
    save.add_argument("--echo-guard", action=argparse.BooleanOptionalAction, default=True)
    save.add_argument("--chrome-echo", action=argparse.BooleanOptionalAction, default=True)
    save.add_argument("--chrome-noise", action=argparse.BooleanOptionalAction, default=True)
    save.add_argument("--pulse-buffer", choices=("standard", "short"), default="standard")
    save.add_argument("--experimental", action="store_true")
    save.add_argument("--turn-return-drive", type=float, choices=(1., 1.5, 2., 2.5, 3.))
    reset = operations.add_parser("reset")
    reset.add_argument("sink")
    reset.add_argument("--port", help="Port ALSA à réinitialiser")
    operations.add_parser("trials", help="Affiche les profils temporaires de calibration")
    args = parser.parse_args()
    if args.operation == "trials":
        print(json.dumps({name: asdict(trial) for name, trial in MIX_TRIALS.items()}, ensure_ascii=False, indent=2))
        return
    settings = None
    try:
        if args.operation == "save":
            if args.port and output_port(args.sink) != args.port:
                raise ValueError("Le port ALSA à mémoriser n’est pas actif")
            settings = {"profile": args.profile, "volume_percent": args.volume,
                        "mic_gain_percent": args.voice, "echo_guard": args.echo_guard,
                        "chrome_echo": args.chrome_echo, "chrome_noise": args.chrome_noise,
                        "pulse_buffer": args.pulse_buffer,
                        "experimental": args.experimental}
            if args.turn_return_drive is not None:
                settings["turn_return_drive"] = args.turn_return_drive
        update_profile(PROFILE_PATH, args.sink, args.port, settings)
    except (OSError, ValueError, RecursionError, subprocess.SubprocessError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
