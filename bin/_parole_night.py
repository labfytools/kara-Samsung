"""Admission nocturne Sway/logind. Le propriétaire de maintenance détient les FD."""
from dataclasses import dataclass, replace
from pathlib import Path
import json
import os
import re
import sqlite3
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


def acpi_lid_closed(root=Path("/proc/acpi/button/lid")):
    # _LID may itself misreport around resume. Use it only to veto an
    # opening when firmware and the input event disagree.
    try:
        states = {(entry / "state").read_text().split()[-1] for entry in root.iterdir()}
    except (OSError, IndexError):
        return None
    return {"closed": True, "open": False}.get(next(iter(states))) if len(states) == 1 else None


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

    def request_lock(self):
        if (CONFIG / "quickshell/labfy-lock/shell.qml").is_file():
            # The wrapper owns the real compositor lock, which must survive
            # Parole and remain in effect after the capot is reopened.
            args = [str(Path.home() / ".local/bin/labfy-lock")]
        else:
            args = ["loginctl", "lock-session", sway_session()]
        return subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, close_fds=True)

    def display(self, on):
        replies = json.loads(command("swaymsg", "-r", "output", "*", "power", "on" if on else "off"))
        if not replies or not all(reply.get("success") is True for reply in replies):
            raise ValueError("Commande DPMS refusée par Sway")

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
        self.lid_fd = self.fd = self.delay = None
        self.profile = self.locker = None
        self.desktop = None
        self.power = "unread"
        self.next_desktop = self.next_sleep = self.next_lock = self.next_display = 0.
        self.open_since = None
        self.reopened = self.restore_display = False
        self.release_after_work = False
        self.budget = None
        self.window = None
        self.reason = ""

    def report(self, reason):
        if reason != self.reason:
            self.reason = reason
            self.journal.emit("IDLE", reason)
        return False

    def stop_work(self):
        if self.delay is not None:
            os.close(self.delay)
            self.delay = None
        if self.fd is not None and (self.lid_fd is None or not self.desktop or not self.desktop.lid):
            os.close(self.fd)
            self.fd = None
        if self.profile is not None:
            hold, self.profile = self.profile, None
            try:
                self.login.release_economy(hold)
            except self.login.glib.Error:
                pass

    def close(self, *, reason="arrêt de la surveillance"):
        # FD_CLOEXEC and one maintenance owner restore logind policy on crash.
        # Close the lid FD after the work FD so releasing it can let logind
        # suspend a still-closed machine once the engine has exited.
        if self.lid_fd is not None:
            fd, self.lid_fd = self.lid_fd, None
            self.stop_work()
            os.close(fd)
            self.journal.emit("IDLE", f"Inhibiteur de capot absent · {reason}")
        else:
            self.stop_work()
        self.release_after_work = False
        if self.locker is not None:
            process, self.locker = self.locker, None
            if process.poll() is None:
                process.kill()
            process.wait()
        self.end_window(reason)

    def end_window(self, reason):
        if self.window is not None:
            try:
                count = self.index.end_closed_report(self.window, reason)
            except (OSError, ValueError, sqlite3.Error):
                self.journal.emit("IDLE", "Bilan capot fermé indisponible", "WARNING")
                return
            self.window = None
            if count is not None:
                self.journal.emit("IDLE", f"Bilan capot fermé · {count} tâches réalisées · {reason}")

    def activity(self):
        # A Wayland activity event while closed can come from an external
        # device or from wakeup. It must not reset the closed-lid allowance.
        if self.desktop and self.desktop.lid:
            return
        if self.budget is not None or self.index.night_budget() is not None:
            self.index.night_budget(None)
            self.budget = None

    def suspend(self, detail, *, running):
        if running:
            self.release_after_work = True
        else:
            self.close(reason=detail)
        return self.report("Préparation nocturne suspendue · " + detail)

    def observe(self, now):
        previous = self.desktop
        state = self.login.observe()
        if previous and previous.lid and not state.lid:
            # logind reported SW_LID=open during the s2idle resume in the
            # incident. Ignore it during sleep and require a fresh stable
            # signal after resume before lighting displays or resuming work.
            if state.sleeping:
                self.open_since = None
            if not state.sleeping and self.open_since is None:
                self.open_since = now
                self.journal.emit("IDLE", "Signal d’ouverture du capot · confirmation en cours")
            if (state.sleeping or now - self.open_since < 2. or acpi_lid_closed() is True):
                state = replace(state, lid=True)
        else:
            self.open_since = None
        self.desktop = state
        if previous is None or previous.screen_off != state.screen_off:
            self.journal.emit("IDLE", "Écrans éteints" if state.screen_off else "Écrans allumés")
        if previous is None or previous.locked != state.locked:
            self.journal.emit("IDLE", "Session Sway verrouillée" if state.locked else "Session Sway déverrouillée")
        if previous is None or previous.lid != state.lid:
            self.journal.emit("IDLE", "Capot fermé" if state.lid else "Capot ouvert · signal confirmé")
            self.reopened = bool(previous and previous.lid and not state.lid)
            if self.reopened:
                self.end_window("réouverture du capot")
                if self.restore_display:
                    self.activity()
        if not state.locked:
            self.reopened = False
        return state

    def secure_closed(self, now):
        if self.locker is not None:
            result = self.locker.poll()
            if result is not None:
                self.locker = None
                if result:
                    self.journal.emit("IDLE", "Commande de verrouillage Sway en échec", "WARNING")
            elif now >= self.next_lock:
                self.locker.kill()
                self.locker.wait()
                self.locker = None
                self.journal.emit("IDLE", "Délai du verrouillage Sway dépassé", "WARNING")
        if not self.desktop.locked:
            if self.locker is None and now >= self.next_lock:
                self.locker = self.login.request_lock()
                self.next_lock = now + 10.
            return self.report("Capot fermé · attente du verrouillage Sway")
        if not self.desktop.screen_off:
            self.set_display(False, now)
            return self.report("Capot fermé · attente de l’extinction des écrans")
        return True

    def set_display(self, on, now):
        if now >= self.next_display:
            self.next_display = now + .85
            self.login.display(on)

    def refresh(self, settings, *, running=False, fast=False):
        # Arm while the lid is still open, independently of idle, queue
        # contents and the admission of compute-intensive work.
        if not settings["night_enabled"] or settings["paused"]:
            return self.suspend("mode désactivé" if not settings["night_enabled"] else "maintenance en pause",
                                running=running)
        power = mains_online()
        if power != self.power:
            event = ("Alimentation secteur détectée" if power is True else
                     "Débranchement du secteur · fonctionnement sur batterie" if power is False and self.power is True else
                     "Fonctionnement sur batterie" if power is False else "Alimentation inconnue")
            self.journal.emit("IDLE", event)
            self.power = power
        if power is not True and running:
            return self.suspend("secteur requis", running=True)
        now = monotonic()
        try:
            if self.login is None:
                self.login = Login()
            if fast and now >= self.next_sleep:
                self.next_sleep = now + .35
                if self.login.sleeping():
                    return self.suspend("mise en veille demandée par logind", running=running)
            if now >= self.next_desktop:
                self.next_desktop = now + .85
                self.observe(now)
            state = self.desktop
            waking_display = False
            # The protected closure owns the display transition even after
            # logind gets its lid policy back. A confirmed wake on battery
            # can restore the display without admitting work or inhibitors.
            if self.restore_display and state and state.active and not state.sleeping and not state.lid:
                waking_display = state.screen_off
                if waking_display:
                    self.set_display(True, now)
                else:
                    self.restore_display = False
                    if self.delay is None:
                        self.stop_work()
        except self.errors():
            return self.suspend("état de la session Sway ou logind indisponible", running=running)
        if power is not True:
            return self.suspend("secteur requis", running=running)
        if state is None or state.sleeping:
            return self.suspend("mise en veille demandée par logind", running=running)
        if not state.active:
            return self.suspend("session Sway inactive", running=running)
        if self.expired(settings):
            return self.suspend("durée maximale de maintien éveillé atteinte", running=running)
        allowed = settings["night_lid"] and settings["night_lid_qualified"]
        if not allowed and self.lid_fd is not None:
            return self.suspend("option capot fermé ou qualification thermique désactivée", running=running)
        try:
            if allowed and self.lid_fd is None and not state.lid:
                self.lid_fd = self.login.inhibit("handle-lid-switch", "block")
                self.journal.emit("IDLE", "Inhibiteur de capot actif · prêt avant fermeture")
            if self.lid_fd is not None and state.lid:
                self.restore_display = True
                self.start_budget()
                # Keep logind's idle action inhibited across tasks, including
                # when the queue is empty. Manual sleep has only a delay FD.
                self.hold_idle()
                if self.window is None:
                    self.window = self.index.start_closed_report()
                if not self.secure_closed(now):
                    return False
            if waking_display:
                return self.report("Réouverture du capot · réactivation des écrans")
        except self.errors():
            return self.suspend("inhibiteur de capot ou commande Sway indisponible", running=running)
        return True

    def check(self, settings, *, busy=False, running=False):
        if not self.refresh(settings, running=running, fast=running):
            return False
        if busy:
            return self.report("Préparation nocturne suspendue · activité prioritaire")
        if self.reopened:
            return self.report("Réouverture du capot · préparation suspendue")
        state = self.desktop
        if state.lid and self.lid_fd is None:
            return self.report("Capot fermé · admission nocturne refusée")
        if not state.locked:
            return self.report("Préparation nocturne en attente du verrouillage Sway")
        if not state.lid and state.screen_off and not settings["night_screen_off"]:
            return self.report("Écrans éteints · préparation désactivée dans cet état")
        try:
            if self.profile is not None and self.login.get(PROFILES_PATH, PROFILES, "ActiveProfile", service=PROFILES) != "power-saver":
                return self.report("Profil énergétique modifié · préparation suspendue")
        except self.errors():
            return self.report("Profil énergétique indisponible · préparation suspendue")
        return True

    def errors(self):
        failures = (OSError, ValueError, ImportError, TypeError, KeyError, sqlite3.Error, subprocess.SubprocessError)
        return failures + (self.login.glib.Error,) if self.login is not None else failures

    def hold_idle(self):
        if self.fd is None:
            self.fd = self.login.inhibit("idle", "block")

    def open(self, settings):
        if self.delay is not None:
            return True
        if not self.check(settings):
            return False
        try:
            self.hold_idle()
            self.delay = self.login.inhibit("sleep", "delay")
        except self.errors():
            self.stop_work()
            return self.report("Inhibiteur de travail logind indisponible")
        try:
            self.profile = self.login.hold_economy()
        except self.errors():
            # Systems without the profile service still enforce live limits.
            self.journal.emit("IDLE", "Profil Économie temporaire indisponible")
        if self.profile is not None:
            self.journal.emit("IDLE", "Profil Économie temporaire demandé")
        self.start_budget()
        self.journal.emit("IDLE", "Mode nocturne actif · maintien temporaire sur secteur")
        return True

    def start_budget(self):
        if self.budget is None:
            self.budget = clock_gettime(CLOCK_BOOTTIME)
            self.index.night_budget(self.budget)

    def expired(self, settings):
        if self.budget is None:
            self.budget = self.index.night_budget()
        return self.budget is not None and clock_gettime(CLOCK_BOOTTIME) >= self.budget + settings["night_minutes"] * 60
