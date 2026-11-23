"""Effet de réduction de voix pour la sortie Strawberry, temporaire et réversible.

Route exclusivement les flux PipeWire/PulseAudio appartenant à Strawberry dans
un puits virtuel dont le moniteur est traité par audiokaraoke/GStreamer.
Ne touche ni aux fichiers locaux ni au flux d'entrée du micro.
"""
import json
import os
from pathlib import Path
import re
import subprocess
import time

import gi
gi.require_version("Gst", "1.0")
from gi.repository import Gst, GLib


Gst.init(None)
SINK_PREFIX = "labfy_paroles_vocals_"
# Préserver les instruments hors des fréquences d'intelligibilité vocales.
# Le filtre centre stéréo agit uniquement dans cette bande et non sur tout le mix.
VOICE_LOWER_HZ = 250
VOICE_UPPER_HZ = 2800
FIR_LENGTH = 401  # filtres complémentaires à phase linéaire, retard identique
STRENGTHS = (("Léger", .25, 1.0), ("Moyen", .45, 1.0), ("Fort", .65, 1.0))
PULSE_TIMEOUT = 2.0


class VocalEffect:
    def __init__(self):
        self.module_id = None
        self.sink = None
        self.pipeline = None
        self.karaoke = None
        self.low_filter = None
        self.high_filter = None
        self.band = (VOICE_LOWER_HZ, VOICE_UPPER_HZ)
        self.original = {}
        self.output_sink = None
        self.strength = 1
        self.active = False
        self.error = ""

    @staticmethod
    def pactl(*arguments):
        command = subprocess.run(("pactl", *map(str, arguments)), capture_output=True,
                                 text=True, timeout=PULSE_TIMEOUT, check=False)
        if command.returncode:
            raise RuntimeError(command.stderr.strip()[:180] or "PipeWire n'a pas répondu.")
        return command.stdout.strip()

    @classmethod
    def inputs(cls):
        data = json.loads(cls.pactl("-f", "json", "list", "sink-inputs"))
        return data if isinstance(data, list) else []

    @staticmethod
    def belongs_to_strawberry(item):
        metadata = item.get("properties", {})
        pid = str(metadata.get("application.process.id", ""))
        if not pid.isdecimal() or int(pid) == os.getpid():
            return False
        try:
            process = Path(f"/proc/{pid}/comm").read_text().strip()
        except OSError:
            return False
        if process != "strawberry":
            return False
        return metadata.get("application.process.binary", "strawberry").split("/")[-1] == "strawberry"

    def _bus_message(self, _bus, message):
        if message.type == Gst.MessageType.ERROR:
            error, _debug = message.parse_error()
            self.error = f"Traitement audio interrompu : {error.message}"
            GLib.idle_add(self._stop_after_error)

    def _stop_after_error(self):
        self.disable(keep_error=True)
        return GLib.SOURCE_REMOVE

    def set_strength(self, index):
        if not 0 <= index < len(STRENGTHS):
            raise ValueError("Intensité inconnue")
        self.strength = index
        if self.karaoke is not None:
            _, level, mono = STRENGTHS[index]
            self.karaoke.set_property("level", level)
            self.karaoke.set_property("mono-level", mono)

    def set_band(self, lower, upper):
        """Applique une bande issue de l'analyse du mix, pas une voix isolée."""
        lower, upper = int(lower), int(upper)
        if not (200 <= lower <= 400 and 1800 <= upper <= 3800 and lower < upper):
            raise ValueError("Bande de fréquences invalide")
        self.band = (lower, upper)
        for effect in (self.low_filter, self.high_filter):
            if effect is not None:
                effect.set_property("lower-frequency", float(lower))
                effect.set_property("upper-frequency", float(upper))

    def enable(self):
        if self.active:
            return True
        self.error = ""
        try:
            self.output_sink = self.pactl("get-default-sink")
            if not re.fullmatch(r"[a-zA-Z0-9_.:-]+", self.output_sink):
                raise RuntimeError("Sortie audio non reconnue")
            self.sink = f"{SINK_PREFIX}{os.getpid()}"
            self.module_id = int(self.pactl("load-module", "module-null-sink", f"sink_name={self.sink}",
                                            "sink_properties=device.description=Labfy_Paroles_Karaoke"))
            _, level, mono = STRENGTHS[self.strength]
            lower, upper = self.band
            pipeline = (
                f'pulsesrc device="{self.sink}.monitor" buffer-time=100000 latency-time=20000 '
                '! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 '
                '! audioconvert ! audioresample ! audio/x-raw,format=F32LE,channels=2,rate=48000 '
                '! tee name=split '
                'audiomixer name=mix ! audioconvert '
                f'! pulsesink device="{self.output_sink}" sync=true '
                'split. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 '
                f'! audiowsincband name=outside mode=band-reject lower-frequency={lower} '
                f'upper-frequency={upper} length={FIR_LENGTH} low-latency=true ! mix. '
                'split. ! queue max-size-buffers=0 max-size-bytes=0 max-size-time=200000000 '
                f'! audiowsincband name=inside mode=band-pass lower-frequency={lower} '
                f'upper-frequency={upper} length={FIR_LENGTH} low-latency=true '
                f'! audiokaraoke name=vocals level={level} mono-level={mono} ! mix.'
            )
            self.pipeline = Gst.parse_launch(pipeline)
            self.karaoke = self.pipeline.get_by_name("vocals")
            self.low_filter = self.pipeline.get_by_name("outside")
            self.high_filter = self.pipeline.get_by_name("inside")
            if self.karaoke is None or self.low_filter is None or self.high_filter is None:
                raise RuntimeError("Filtre GStreamer indisponible")
            self.pipeline.get_bus().add_signal_watch()
            self.pipeline.get_bus().connect("message", self._bus_message)
            if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                raise RuntimeError("Impossible de démarrer le traitement audio")
            self.active = True
            self.poll()
            return self.active
        except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired, GLib.Error) as exc:
            self.error = str(exc)
            self.disable(keep_error=True)
            return False

    def poll(self):
        if not self.active:
            return
        try:
            sink_table = self.pactl("list", "short", "sinks")
            mapping = dict((name, int(ident)) for ident, name, *_ in
                           (line.split("\t") for line in sink_table.splitlines() if "\t" in line))
            if self.sink not in mapping:
                raise RuntimeError("Le circuit audio temporaire a disparu")
            for item in self.inputs():
                if not self.belongs_to_strawberry(item):
                    continue
                index = int(item["index"])
                current_sink = int(item["sink"])
                if index not in self.original:
                    self.original[index] = current_sink
                if mapping.get(self.sink) != current_sink:
                    self.pactl("move-sink-input", index, self.sink)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc)
            self.disable(keep_error=True)

    def disable(self, keep_error=False):
        if self.module_id is None and self.pipeline is None:
            self.active = False
            return False
        was_active = self.active
        self.active = False
        try:
            inputs = self.inputs()
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
            inputs = []
        for item in inputs:
            index = int(item["index"])
            if index not in self.original or not self.belongs_to_strawberry(item):
                continue
            try:
                self.pactl("move-sink-input", index, self.original[index])
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                try:
                    self.pactl("move-sink-input", index, self.pactl("get-default-sink"))
                except (OSError, RuntimeError, subprocess.TimeoutExpired):
                    pass
        # PipeWire peut confirmer le déplacement après la réponse de pactl.
        # Vérifier le retour avant de détruire le puits temporaire.
        if self.original:
            for retry in range(5):
                try:
                    active = {int(i["index"]): int(i["sink"]) for i in self.inputs()}
                    if all(active.get(index, origin) == origin
                           for index, origin in self.original.items()):
                        break
                    time.sleep(.05)
                except (OSError, RuntimeError, subprocess.TimeoutExpired, ValueError):
                    break
        self.original.clear()
        if self.pipeline is not None:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.karaoke = None
        self.low_filter = self.high_filter = None
        if self.module_id is not None:
            try:
                self.pactl("unload-module", self.module_id)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        self.module_id = self.sink = self.output_sink = None
        if not keep_error:
            self.error = ""
        return was_active
