#!/usr/bin/env python3
"""Mesure le vrai parcours capot fermé, avec une bibliothèque d'essai privée."""

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
from shutil import copy2, disk_usage
import signal
import subprocess
import sys
from threading import Event
from time import CLOCK_BOOTTIME, clock_gettime, monotonic


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))


def read_command(*args, timeout=3):
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def temperatures():
    cpu, gpu = [], []
    for device in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            name = (device / "name").read_text().strip()
            if name in {"k10temp", "coretemp", "zenpower"}:
                group = cpu
            elif name in {"amdgpu", "nouveau", "nvidia", "i915", "xe"}:
                group = gpu
            else:
                continue
            group.extend(int(sensor.read_text()) / 1000 for sensor in device.glob("temp*_input"))
        except (OSError, ValueError):
            continue
    return max(cpu, default=None), max(gpu, default=None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path, help="nouveau dossier privé sur un volume avec 20 Gio libres")
    parser.add_argument("--audio", required=True, type=Path, help="morceau local accompagné d’un LRC de référence")
    parser.add_argument("--vpn-interface", default="proton0")
    parser.add_argument("--minutes", type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.minutes <= 720:
        parser.error("durée attendue entre 5 et 720 minutes")
    audio = args.audio.resolve(strict=True)
    lrc = audio.with_suffix(".lrc")
    if not audio.is_file() or not lrc.is_file():
        parser.error("audio et LRC de référence requis")
    workspace = args.workspace.resolve()
    if disk_usage(workspace.parent).free <= 20 * 1024**3:
        parser.error("réserve de 20 Gio requise sur le volume d’essai")

    # All writes use a fresh workspace. The existing model is shared in
    # place while both the stems and Parole index are private to the test.
    os.umask(0o077)
    workspace.mkdir(mode=0o700)
    library = workspace / "library"
    album = library / "Essai" / "Capot"
    album.mkdir(parents=True)
    copy2(audio, album / audio.name)
    copy2(lrc, album / lrc.name)
    original_cache = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    os.environ.setdefault("TORCH_HOME", str(original_cache / "torch"))
    os.environ.update(KARA_MUSIC_LIBRARY=str(library), XDG_STATE_HOME=str(workspace / "state"),
                      XDG_CACHE_HOME=str(workspace / "cache"))

    from _parole_maintenance import Maintenance
    from _parole_night import Login, acpi_lid_closed, mains_online

    login = Login()
    initial = login.observe()
    if initial.lid or not initial.active or mains_online() is not True:
        parser.error("démarrer sur secteur, capot ouvert, dans la session Sway active")
    owner = Maintenance(library, None, background=True)
    owner.index.configure(night_enabled=True, night_lid=True, night_lid_qualified=True, night_minutes=args.minutes,
                          lyrics=False, lrc=False, cover=False, metadata=False, albums=False, discography=False)
    stop = Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())
    started = datetime.now().astimezone()
    limit = clock_gettime(CLOCK_BOOTTIME) + args.minutes * 60
    next_network = 0.
    announced = False
    owner.start()
    try:
        with (workspace / "samples.jsonl").open("x", encoding="utf-8", buffering=1) as stream:
            while not stop.is_set() and clock_gettime(CLOCK_BOOTTIME) < limit and owner.thread.is_alive():
                clock = monotonic()
                cpu, gpu = temperatures()
                try:
                    desktop = login.observe()
                except (OSError, ValueError, subprocess.SubprocessError, login.glib.Error):
                    desktop = None
                closed = owner.index.closed_progress()
                armed = owner.night.lid_fd is not None
                active = owner.active
                sample = dict(at=datetime.now().astimezone().isoformat(), monotonic=clock,
                              boottime=clock_gettime(CLOCK_BOOTTIME), cpu_c=cpu, gpu_c=gpu,
                              mains=mains_online(), acpi_closed=acpi_lid_closed(), armed=armed,
                              logind_closed=desktop.lid if desktop else None, sleep=desktop.sleeping if desktop else None,
                              locked=desktop.locked if desktop else None, screens_off=desktop.screen_off if desktop else None,
                              task=active[0] if active else None, reason=owner.night.reason,
                              completed=closed["completed"] if closed else {})
                if clock >= next_network:
                    next_network = clock + 15.
                    # An explicit device binding rules out a successful
                    # non-VPN fallback. Store only connectivity and status.
                    sample["network"] = read_command("nmcli", "networking", "connectivity")
                    sample["vpn_http"] = read_command("curl", "-q", "--noproxy", "*", "--interface",
                                                      "if!" + args.vpn_interface, "--connect-timeout", "3",
                                                      "--max-time", "5", "-fsSI", "-o", "/dev/null",
                                                      "-w", "%{http_code}", "https://example.com/", timeout=6)
                stream.write(json.dumps(sample, ensure_ascii=False, allow_nan=False) + "\n")
                if armed and not announced:
                    announced = True
                    print("Inhibiteur de capot actif · essai prêt", flush=True)
                stop.wait(max(0., 2. - (monotonic() - clock)))
    finally:
        owner.close()
        owner.thread.join(timeout=15)
        with (workspace / "bilan.json").open("x", encoding="utf-8") as stream:
            json.dump({"capot": owner.index.closed_progress(), "préparation": owner.index.night_progress()[3]},
                      stream, ensure_ascii=False, indent=2)
        journal = read_command("journalctl", "-b", "-u", "systemd-logind", "-u", "systemd-suspend",
                               "--since", started.strftime("%Y-%m-%d %H:%M:%S"), "--no-pager",
                               "-o", "short-iso-precise", timeout=5)
        if journal is not None:
            (workspace / "suspend.log").write_text(journal + "\n")
    return 0 if announced and not owner.thread.is_alive() else 1


if __name__ == "__main__":
    sys.exit(main())
