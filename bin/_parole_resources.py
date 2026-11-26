"""Politique d'admission lourde de Parole dans la session Sway."""
import os
import stat
from pathlib import Path
import selectors
from shutil import disk_usage
import signal
import subprocess
import sys
from time import monotonic

from _kara_prepare import CACHE_ROOT
from _kara_stems import ROOT as STEM_ROOT

GIB = 1024**3
MIB = 1024**2


def contents(root):
    total, shared, sizes, seen = 0, 0, {}, set()
    for parent, _, names in os.walk(root):
        for name in names:
            p = Path(parent) / name
            try:
                info = p.lstat()
                identity = info.st_dev, info.st_ino
                if stat.S_ISREG(info.st_mode):
                    size = info.st_blocks * 512
                    if identity in seen:
                        # Count each additional name for an already allocated
                        # inode without charging its blocks to the disk cap.
                        shared += size
                        continue
                    seen.add(identity)
                    total += size
                    category = name if name.endswith(".flac") else p.suffix
                    sizes[category] = sizes.get(category, 0) + size
            except FileNotFoundError:
                continue
    return total, sizes, shared


def nearest(path):
    while not path.exists():
        path = path.parent
    return path


def block_device(path):
    info = path.stat()
    if os.major(info.st_dev):
        return os.major(info.st_dev), os.minor(info.st_dev)
    # Btrfs subvolumes report a virtual device in stat(). Resolve the backing
    # device so /proc/diskstats measures the mounted filesystem's real I/O.
    output = subprocess.run(("findmnt", "-n", "-o", "SOURCE", "--target", str(path)),
                            capture_output=True, text=True, timeout=2, check=True).stdout.strip()
    device = output.split("[", 1)[0]
    if not device.startswith("/dev/"):
        return None
    info = Path(device).stat()
    return os.major(info.st_rdev), os.minor(info.st_rdev)


def multimedia_active():
    # Reading sink state never creates a playback or capture stream.
    query = subprocess.run(("pactl", "list", "sink-inputs"), capture_output=True,
                           timeout=1.5, check=True, env=dict(os.environ, LC_ALL="C"))
    return b"Corked: no" in query.stdout


# PSI is a stall-time indicator, not a fraction of RAM in use. For
# unattended work with the lid closed, record stalls without treating
# PSI alone as memory exhaustion. The absolute reserve remains mandatory.
MEMORY_RETRY_SECONDS = 30.


def memory_psi(text):
    values = {}
    for line in text.splitlines():
        fields = line.split()
        if fields and fields[0] in ("some", "full"):
            sample = next((item[6:] for item in fields[1:] if item.startswith("avg10=")), None)
            if sample is None or fields[0] in values:
                raise ValueError("PSI mémoire invalide")
            value = float(sample)
            if not 0 <= value <= 100:
                raise ValueError("PSI mémoire invalide")
            values[fields[0]] = value
    if set(values) != {"some", "full"}:
        raise ValueError("PSI mémoire incomplet")
    return values["some"], values["full"]


def memory_limits(*, closed=False, running=False, light=False):
    return (None, None) if closed else (1., 1.)


def thermal_admission(settings, cpu_t, gpu_t, *, nocturnal=False, closed=False):
    cpu_limit = settings["night_cpu_temperature" if nocturnal else "temperature"]
    gpu_limit = settings["night_gpu_temperature" if nocturnal else "temperature"]
    if closed:
        # Explicitly configurable, with a hard application ceiling of 95°C.
        # Keep the conservative defaults and all sensor validation.
        closed_limit = min(95, settings["night_closed_temperature"])
        cpu_limit, gpu_limit = min(cpu_limit, closed_limit), min(gpu_limit, closed_limit)
    temperatures = ((cpu_t or gpu_t) and all(0 < value < cpu_limit for value in cpu_t)
                    and all(0 < value < gpu_limit for value in gpu_t))
    return bool(temperatures and (not nocturnal or (cpu_t and gpu_t)))


class Resources:
    def __init__(self, root):
        self.root = root
        self.listener = None
        self.idle = False
        self.resumed = False
        self.threshold = None
        self.last_usage = 0
        self.disks = {}
        self.cpu = None
        self.storage = {}
        self.sample = 0
        self.index_pressure = None
        self.sample_policy = None
        self.can_start = self.can_start_light = self.can_continue = False
        self.reason = "Mesure des ressources"
        self.memory_detail = None
        self.memory_retry_at = 0.
        self.memory_observation = None

    def close(self):
        proc, self.listener = self.listener, None
        self.idle = False
        if proc:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=.5)
            except ProcessLookupError:
                pass
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
            proc.stdout.close()

    def session_idle(self, seconds):
        if self.listener is not None and (self.threshold != seconds or self.listener.poll() is not None):
            self.close()
        if self.listener is None:
            if not os.environ.get("SWAYSOCK") or not os.environ.get("WAYLAND_DISPLAY"):
                return False
            try:
                # The commands are constant, write one byte to a private pipe
                # and do not change display or logind settings.
                guard = Path(__file__).with_name("_parole_guard.py")
                self.listener = subprocess.Popen([sys.executable, "-B", str(guard), str(os.getpid()),
                                                  "swayidle", "-C", "/dev/null", "-w", "timeout",
                                                  str(seconds), "printf 1", "resume", "printf 0"],
                                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                                 stderr=subprocess.DEVNULL, start_new_session=True)
            except OSError:
                return False
            self.threshold = seconds
        with selectors.DefaultSelector() as selector:
            selector.register(self.listener.stdout, selectors.EVENT_READ)
            if selector.select(0):
                chunk = os.read(self.listener.stdout.fileno(), 64)
                self.resumed |= b"0" in chunk
                self.idle = bool(chunk and chunk[-1:] == b"1")
        return self.idle

    def collect_storage(self, now, force=False):
        if not force and now < self.last_usage + 30:
            return
        prepared, reference, shared_references = contents(CACHE_ROOT)
        stems, audio, shared_stems = contents(STEM_ROOT)
        paths = {name: nearest(path) for name, path in (
            ("bibliothèque", self.root), ("références", CACHE_ROOT), ("pistes IA", STEM_ROOT))}
        self.storage = {"bytes": prepared + stems, "reference": reference, "stems": audio,
                        "shared_bytes": shared_references + shared_stems,
                        "free": {name: disk_usage(path).free for name, path in paths.items()},
                        "devices": {name: block_device(path) for name, path in paths.items()}}
        self.last_usage = now

    def disk_load(self, now):
        devices = set(self.storage["devices"].values())
        current = {}
        for line in Path("/proc/diskstats").read_text().splitlines():
            fields = line.split()
            device = int(fields[0]), int(fields[1])
            if device in devices:
                current[device] = int(fields[12])
        previous, self.disks = self.disks, current
        if set(current) != devices or not previous or self.sample == now:
            return None
        return max((value - previous.get(key, value)) / (10 * (now - self.sample))
                   for key, value in current.items())

    def allowed_storage(self, settings, duration=0):
        if not self.storage:
            return False
        # Reserve for concurrent FLAC drafts and the reference on the mounted
        # filesystems, including a conservative transient upper bound.
        working = int(max(duration, 1) * 3 * MIB) if duration else 0
        return (self.storage["bytes"] + working < settings["cache_gib"] * GIB
                and min(self.storage["free"].values()) > settings["reserve_gib"] * GIB + working)

    def check(self, settings, *, busy=False, running=False, nocturnal=False, lid=False, light=False):
        now = monotonic()
        if busy:
            self.can_start = self.can_start_light = self.can_continue = False
            self.reason = "Kara, lecture ou imports prioritaires"
            self.memory_detail = None
            return False
        closed = nocturnal and lid
        policy = (nocturnal, closed, running, light)
        if policy == self.sample_policy and now < self.sample + 1.5:
            return self.can_continue if running else (self.can_start_light if light else self.can_start)
        self.can_start = self.can_start_light = self.can_continue = False
        self.memory_detail = None
        try:
            self.collect_storage(now)
            if running:
                # Free space is sampled on the actual mounts during writing.
                self.storage = {**self.storage, "free": {
                    name: disk_usage(nearest(path)).free for name, path in (
                        ("bibliothèque", self.root), ("références", CACHE_ROOT), ("pistes IA", STEM_ROOT))}}
            # The owner has already confirmed the closed lid, compositor
            # lock and DPMS. Re-sample under the stricter thermal limits at
            # that transition without waiting for a Wayland idle timeout.
            idle = closed or self.session_idle(settings["night_idle" if nocturnal else "idle"])
            # Night.check reads the physical source at process-supervision
            # cadence. This legacy policy remains available for daytime work.
            power = True
            if not nocturnal:
                supply = Path("/sys/class/power_supply")
                batteries, external = [], []
                for device in supply.iterdir():
                    kind = (device / "type").read_text().strip()
                    if kind == "Battery":
                        batteries.append(device)
                    elif (device / "online").is_file():
                        external.append((device / "online").read_text().strip() == "1")
                power = settings["battery"] or not batteries or any(external)
            cpu_fields = [int(token) for token in Path("/proc/stat").read_text().splitlines()[0].split()[1:9]]
            cpu_sample = sum(cpu_fields), cpu_fields[3] + cpu_fields[4]
            cpu, self.cpu = self.cpu, cpu_sample
            load = 100 * (1 - (cpu_sample[1] - cpu[1]) / max(1, cpu_sample[0] - cpu[0])) if cpu else None
            available = int(next(line.split()[1] for line in Path("/proc/meminfo").read_text().splitlines()
                                 if line.startswith("MemAvailable:"))) / 1024
            pressure, full_pressure = memory_psi(Path("/proc/pressure/memory").read_text())
            gpus = [int(p.read_text()) for p in Path("/sys/class/drm").glob("card[0-9]*/device/gpu_busy_percent")]
            sensors = {}
            for device in Path("/sys/class/hwmon").glob("hwmon*"):
                name = (device / "name").read_text().strip()
                if name in {"k10temp", "amdgpu", "coretemp", "zenpower", "nouveau", "nvidia", "i915", "xe"}:
                    sensors.setdefault(name, []).extend(int(p.read_text()) / 1000 for p in device.glob("temp*_input"))
            cpu_t = [value for name in ("k10temp", "coretemp", "zenpower") for value in sensors.get(name, ())]
            gpu_t = [value for name in ("amdgpu", "nouveau", "nvidia", "i915", "xe") for value in sensors.get(name, ())]
            temperatures = thermal_admission(settings, cpu_t, gpu_t, nocturnal=nocturnal, closed=closed)
            disk = self.disk_load(now)
            self.index_pressure = (now, load, available, pressure, disk)
            self.sample = now
            self.sample_policy = policy
            # During the task its own CPU/GPU/disk load is expected. User
            # activity, power, pressure, temperature and remaining space
            # still revoke admission and trigger process-group cleanup.
            minimum = min(settings["memory_mib"], settings["running_memory_mib"]) if running else settings["memory_mib"]
            some_limit, full_limit = memory_limits(closed=closed, running=running, light=light)
            pressure_ok = some_limit is None or (pressure < some_limit and full_pressure < full_limit)
            self.memory_observation = {"available_mib": available, "minimum_mib": minimum,
                                       "some_avg10": pressure, "full_avg10": full_pressure,
                                       "psi_advisory": some_limit is None}
            memory_ok = available >= minimum and pressure_ok
            if closed and running and not light and not memory_ok:
                self.memory_retry_at = now + MEMORY_RETRY_SECONDS
            for ok, reason in (
                (idle, "Session active"), (power, "Alimentation secteur requise"),
                (memory_ok, "Mémoire ou pression mémoire"),
                (temperatures, "Température CPU/GPU indisponible ou trop élevée"),
                (self.allowed_storage(settings), "Réserve disque ou plafond du cache"),
            ):
                if not ok:
                    self.reason = reason
                    if reason == "Mémoire ou pression mémoire":
                        # PSI is system-wide and can trigger with ample freeable
                        # memory. Record both observations from this decision.
                        cause = ("réserve et PSI" if available < minimum and not pressure_ok
                                 else "réserve" if available < minimum else "PSI")
                        thresholds = ("informatif, capot fermé" if some_limit is None else
                                      f"seuils some {some_limit:g} %, full {full_limit:g} %")
                        self.memory_detail = (f"{cause} · {available:.0f} Mio disponibles (minimum {minimum})"
                                              f" · PSI mémoire some avg10 {pressure:.2f} %,"
                                              f" full avg10 {full_pressure:.2f} % ({thresholds})")
                    return False
            if multimedia_active():
                self.reason = "Flux multimédia actif"
                return False
            self.can_continue = True
            # Network maintenance needs the same safety measurements and
            # launch memory floor, but does not need a free compute slot.
            self.can_start_light = available >= settings["memory_mib"]
            if closed and not running and now < self.memory_retry_at:
                self.reason = "Pause mémoire après interruption · reprise différée"
                return light and self.can_start_light
            # A secured closed lid on AC is dedicated to batch work. Load
            # percentages are observations, not resource exhaustion. Keep
            # thermal, memory, storage and foreground checks above.
            if closed:
                self.reason = "Mode capot fermé · calcul prioritaire"
                self.can_start = self.can_start_light
                return running or self.can_start
            for ok, reason in (
                (load is not None and load < settings["cpu"], "CPU occupé"),
                (disk is not None and disk < settings["disk_busy"], "Disque occupé"),
                (gpus and max(gpus) <= settings["gpu"], "GPU occupé ou non mesurable"),
            ):
                if not ok:
                    self.reason = reason
                    return running or (light and self.can_start_light)
            self.reason = "Ordinateur disponible"
            self.can_start = self.can_start_light
            return running or self.can_start
        except (OSError, ValueError, StopIteration, IndexError, subprocess.SubprocessError):
            self.reason = "Mesures de ressources indisponibles"
            return False
