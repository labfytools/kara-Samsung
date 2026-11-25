"""Admission nocturne Sway/logind. Le propriétaire de maintenance détient les FD."""
from dataclasses import dataclass
from pathlib import Path
import json
import os
import re
import subprocess
from time import CLOCK_BOOTTIME, clock_gettime, monotonic

from _kara_paths import CONFIG


LOGIN = "org.freedesktop.login1"
MANAGER = "/org/freedesktop/login1"
PROFILES = "org.freedesktop.UPower.PowerProfiles"
PROFILES_PATH = "/org/freedesktop/UPower/PowerProfiles"


def mains_online(root=Path("/sys/class/power_supply")):
    """True pour un secteur système confirmé, False hors secteur, None si inconnu."""
    measured = unknown = False
    try:
        devices = list(root.iterdir())
    except OSError:
        return None
    for device in devices:
        try:
            kind = (device / "type").read_text().strip()
            scope = device / "scope"
            if kind == "Battery" or (scope.is_file() and scope.read_text().strip() == "Device"):
                continue
            # Les périphériques USB alimentés par le portable peuvent être
            # online. Seule une source Mains ou de scope System prouve l'entrée.
            if kind != "Mains" and not (scope.is_file() and scope.read_text().strip() == "System"):
                continue
            if kind == "Unknown":
                continue
            value = (device / "online").read_text().strip()
            if value == "1":
                # A USB hot-unplug may remove another sysfs directory during
                # this pass. It cannot negate positive system AC evidence.
                return True
            measured |= value == "0"
            unknown |= value != "0"
        except OSError:
            unknown = True
    return False if measured and not unknown else None


def command(*args):
    return subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, check=True, text=True, timeout=.55).stdout


def sway_session():
    # Sway piloté par UWSM vit dans un service utilisateur. logind ne rattache
    # pas son PID à la session, mais l'environnement initial contient son ID.
    match = re.fullmatch(r"sway-ipc\.(\d+)\.(\d+)\.sock", Path(os.environ.get("SWAYSOCK", "")).name)
    if not match or int(match[1]) != os.getuid():
        raise ValueError("Session Sway indisponible")
    process = Path("/proc") / match[2]
    if process.stat().st_uid != os.getuid():
        raise ValueError("Compositeur d’un autre utilisateur")
    env = (process / "environ").read_bytes().split(b"\0")
    session = next((item[15:] for item in env if item.startswith(b"XDG_SESSION_ID=")), b"")
    if not re.fullmatch(rb"[A-Za-z0-9_-]{1,64}", session):
        raise ValueError("Session graphique indisponible")
    return session.decode()


def screen_off():
    outputs = json.loads(command("swaymsg", "-r", "-t", "get_outputs"))
    if not isinstance(outputs, list) or not outputs:
        raise ValueError("Écrans Sway indisponibles")
    for row in outputs:
        if type(row.get("active")) is not bool or row["active"] and type(row.get("power")) is not bool:
            raise ValueError("État DPMS non mesurable")
    return all(not row["active"] or not row["power"] for row in outputs)


def labfy_secure():
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    marker = runtime / "labfy-lock.launch"
    try:
        # Le reçu de lancement seul n'est jamais une preuve de verrouillage.
        with marker.open("rb") as stream:
            record = json.loads(stream.read(4097))
        pid, generation = record["pid"], record["generation"]
        if type(pid) is not int or pid <= 0 or not re.fullmatch(r"[0-9a-f]{32}", generation):
            return False
        proc = Path("/proc") / str(pid)
        if proc.stat().st_uid != os.getuid():
            return False
        if int((proc / "stat").read_text().rsplit(") ", 1)[1].split()[19]) != record["start_ticks"]:
            return False
        return command("quickshell", "ipc", "--pid", str(pid), "call", "lock", "state").strip().strip('"') == generation + ":secure"
    except (OSError, ValueError, KeyError, IndexError, TypeError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class Desktop:
    active: bool
    locked: bool
    screen_off: bool
    lid: bool
    sleeping: bool


class Login:
    def __init__(self):
        from gi.repository import Gio, GLib

        self.gio, self.glib = Gio, GLib
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
        except GLib.Error as error:
            raise OSError("Bus système indisponible") from error

    def get(self, path, interface, name, *, service=LOGIN):
        answer = self.bus.call_sync(
            service, path, "org.freedesktop.DBus.Properties", "Get",
            self.glib.Variant("(ss)", (interface, name)), self.glib.VariantType("(v)"),
            self.gio.DBusCallFlags.NO_AUTO_START, 450, None)
        return answer.unpack()[0]

    def sleeping(self):
        return self.get(MANAGER, LOGIN + ".Manager", "PreparingForSleep")

    def observe(self):
        session = sway_session()
        path = self.bus.call_sync(
            LOGIN, MANAGER, LOGIN + ".Manager", "GetSession", self.glib.Variant("(s)", (session,)),
            self.glib.VariantType("(o)"), self.gio.DBusCallFlags.NO_AUTO_START, 450, None).unpack()[0]
        active = self.get(path, LOGIN + ".Session", "Active")
        labfy = (CONFIG / "quickshell/labfy-lock/shell.qml").is_file()
        locked = active and (labfy_secure() if labfy else self.get(path, LOGIN + ".Session", "LockedHint"))
        return Desktop(active, locked, screen_off(), self.get(MANAGER, LOGIN + ".Manager", "LidClosed"), self.sleeping())

    def inhibit(self, what, mode):
        value, fds = self.bus.call_with_unix_fd_list_sync(
            LOGIN, MANAGER, LOGIN + ".Manager", "Inhibit",
            self.glib.Variant("(ssss)", (what, "Parole", "Préparation nocturne Kara", mode)),
            self.glib.VariantType("(h)"), self.gio.DBusCallFlags.NO_AUTO_START, 500, None, None)
        fd = fds.get(value.unpack()[0])
        os.set_inheritable(fd, False)
        return fd

    def hold_economy(self):
        version = self.get(PROFILES_PATH, PROFILES, "Version", service=PROFILES)
        if version.startswith("tlp-pd"):
            # TLP 1.11.0 leaves its hold after the bus owner disappears.
            # Crash-safe backends are required before Parole changes profiles.
            return None
        # power-profiles-daemon releases all holds owned by a disconnected
        # caller. Use a private bus as a fallback to the explicit release.
        address = self.gio.dbus_address_get_for_bus_sync(self.gio.BusType.SYSTEM, None)
        flags = self.gio.DBusConnectionFlags
        bus = self.gio.DBusConnection.new_for_address_sync(
            address, flags.AUTHENTICATION_CLIENT | flags.MESSAGE_BUS_CONNECTION)
        try:
            cookie = bus.call_sync(PROFILES, PROFILES_PATH, PROFILES, "HoldProfile",
                                   self.glib.Variant("(sss)", ("power-saver", "Préparation nocturne Kara", "fr.labfy.ParolesStrawberry")),
                                   self.glib.VariantType("(u)"), self.gio.DBusCallFlags.NO_AUTO_START, 600, None).unpack()[0]
        except self.glib.Error:
            bus.close_sync(None)
            raise
        return bus, cookie

    def release_economy(self, hold):
        bus, cookie = hold
        try:
            bus.call_sync(PROFILES, PROFILES_PATH, PROFILES, "ReleaseProfile",
                          self.glib.Variant("(u)", (cookie,)), None,
                          self.gio.DBusCallFlags.NO_AUTO_START, 450, None)
        except self.glib.Error:
            pass
        finally:
            bus.close_sync(None)


class Night:
    def __init__(self, index, journal):
        self.index, self.journal = index, journal
        self.login = None
        self.fd = self.delay = None
        self.profile = None
        self.desktop = None
        self.power = "unread"
        self.next_desktop = self.next_sleep = 0.
        self.budget = None
        self.reason = ""

    def report(self, reason):
        if reason != self.reason:
            self.reason = reason
            self.journal.emit("IDLE", reason)
        return False

    def close(self):
        if self.fd is None:
            return
        # FD_CLOEXEC + one owner: a crash also restores logind policy.
        os.close(self.delay)
        os.close(self.fd)
        self.fd = self.delay = None
        if self.profile is not None:
            hold, self.profile = self.profile, None
            try:
                self.login.release_economy(hold)
            except self.login.glib.Error:
                pass
        self.journal.emit("IDLE", "Retour à la politique normale de veille")

    def activity(self):
        # Clear the persisted allowance only after a real compositor resume,
        # never because a newly started idle monitor has yet to report idle.
        if self.budget is not None or self.index.night_budget() is not None:
            self.index.night_budget(None)
            self.budget = None

    def check(self, settings, *, busy=False, fast=False):
        power = mains_online()
        if power != self.power:
            event = ("Alimentation secteur détectée" if power is True else
                     "Débranchement du secteur" if power is False and self.power is True else
                     "Fonctionnement sur batterie" if power is False else "Alimentation inconnue")
            self.journal.emit("IDLE", event)
            self.power = power
        if power is not True:
            return self.report("Préparation nocturne suspendue · secteur requis")
        if busy:
            return self.report("Préparation nocturne suspendue · activité prioritaire")
        now = monotonic()
        try:
            if self.login is None:
                self.login = Login()
            if fast and now >= self.next_sleep:
                self.next_sleep = now + .35
                if self.login.sleeping():
                    return self.report("Mise en veille demandée · préparation suspendue")
            if now >= self.next_desktop:
                self.next_desktop = now + .85
                previous, self.desktop = self.desktop, None
                self.desktop = self.login.observe()
                if (self.profile is not None
                        and self.login.get(PROFILES_PATH, PROFILES, "ActiveProfile", service=PROFILES) != "power-saver"):
                    return self.report("Profil énergétique modifié · préparation suspendue")
                if previous is None or previous.screen_off != self.desktop.screen_off:
                    self.journal.emit("IDLE", "Écran éteint" if self.desktop.screen_off else "Écran allumé")
                if previous is None or previous.lid != self.desktop.lid:
                    self.journal.emit("IDLE", "Capot fermé" if self.desktop.lid else "Capot ouvert")
                    if self.fd is not None and previous and previous.lid:
                        return self.report("Réouverture du capot · préparation suspendue")
        except self.errors():
            self.desktop = None
            return self.report("État de la session Sway ou logind indisponible")
        state = self.desktop
        if state is None or not state.active or not state.locked:
            return self.report("Préparation nocturne en attente du verrouillage Sway")
        if state.sleeping:
            return self.report("Mise en veille demandée · préparation suspendue")
        if state.lid and (self.fd is None or not (settings["night_lid"] and settings["night_lid_qualified"])):
            return self.report("Capot fermé · admission nocturne refusée")
        if not state.lid and state.screen_off and not settings["night_screen_off"]:
            return self.report("Écran éteint · préparation désactivée dans cet état")
        return True

    def errors(self):
        failures = (OSError, ValueError, ImportError, TypeError, KeyError, subprocess.SubprocessError)
        return failures + (self.login.glib.Error,) if self.login is not None else failures

    def open(self, settings):
        # An idle lock blocks logind's idle action; a low-level lid lock
        # reroutes only this automatic trigger. A bounded delay lock lets
        # active engines wind down on explicit sleep and never vetoes it.
        self.budget = self.index.night_budget()
        now = clock_gettime(CLOCK_BOOTTIME)
        if self.budget is not None and now >= self.budget + settings["night_minutes"] * 60:
            return self.report("Durée maximale de maintien éveillé atteinte")
        if self.fd is not None:
            return True
        self.next_desktop = 0.
        if not self.check(settings):
            return False
        what = "idle:handle-lid-switch" if settings["night_lid"] and settings["night_lid_qualified"] else "idle"
        delay = None
        try:
            delay = self.login.inhibit("sleep", "delay")
            fd = self.login.inhibit(what, "block")
        except self.errors():
            if delay is not None:
                os.close(delay)
            return self.report("Inhibiteur logind indisponible")
        self.fd, self.delay = fd, delay
        try:
            self.profile = self.login.hold_economy()
        except self.errors():
            # Systems without the profile service still enforce the same
            # live thermal limits.
            self.journal.emit("IDLE", "Profil Économie temporaire indisponible")
        if self.profile is not None:
            self.journal.emit("IDLE", "Profil Économie temporaire demandé")
        if self.budget is None:
            self.index.night_budget(now)
            self.budget = now
        self.journal.emit("IDLE", "Mode nocturne actif · maintien temporaire sur secteur")
        return True

    def expired(self, settings):
        if self.budget is not None and clock_gettime(CLOCK_BOOTTIME) >= self.budget + settings["night_minutes"] * 60:
            self.report("Durée maximale de maintien éveillé atteinte")
            return True
        return False
