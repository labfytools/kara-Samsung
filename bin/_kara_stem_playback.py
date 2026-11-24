"""Lecture d'une paire instrumentale/voix calée sur Strawberry (MPRIS).

Le flux Strawberry reste en lecture vers un puits virtuel silencieux pendant
que le mélange des deux stems est envoyé à sa sortie audio d'origine.
Aucune source de microphone n'est modifiée. Désactivation = retour normal.
"""
import json
import os
from pathlib import Path
import subprocess
import time

import gi
gi.require_version("Gst", "1.0")
gi.require_version("GstController", "1.0")
from gi.repository import Gst, GLib, GstController

from _kara_party import ORIGINAL_VOICE_LEVELS
from _kara_stems import cached_stems
from _kara_vocal_effect import VocalEffect

Gst.init(None)
STRENGTHS = tuple(("Instrumental" if not level else f"Voix {level} %", level / 100)
                  for level in ORIGINAL_VOICE_LEVELS)
# Even simultaneous full-scale samples leave room for the protected return
# (bounded to 0.82 * 10**(-9/20)). Neither fader scales the other stem.
TURN_MUSIC_GAIN = .43
TURN_GUIDE_GAIN = .24
GAIN_RAMP = 40 * Gst.MSECOND


class StemPlayback:
    def __init__(self):
        self.song = None
        self.stems = None
        self.pipeline = None
        self.bus = None
        self.bus_handler = None
        self.controls = {}
        self.targets = {}
        self.ramps = {}
        self.turn_mix = None
        self.master = 1.
        self.output_override = None
        self.sink = None
        self.module = None
        self.original = {}
        self.active = False
        self.playing = False
        self.strength = 1
        self.error = ""

    @staticmethod
    def pactl(*args):
        return VocalEffect.pactl(*args)

    def set_song(self, song):
        if song == self.song:
            return
        self.disable()
        self.song = song
        self.stems = cached_stems(song) if song is not None else None
        self.error = ""

    def refresh_stems(self):
        if self.song is None:
            return
        self.stems = cached_stems(self.song)

    def set_strength(self, index):
        if not 0 <= index < len(STRENGTHS):
            raise ValueError("Niveau vocal invalide")
        self.strength = index
        if self.turn_mix is None:
            self._gain("vocal", STRENGTHS[index][1])

    def set_turn_mix(self, music, guide, ceiling):
        self.turn_mix = (music, guide, ceiling)
        self._gain("music", TURN_MUSIC_GAIN * min(music, ceiling) / 100)
        self._gain("vocal", TURN_GUIDE_GAIN * guide / 100)
        self._gain("master", 1.)

    def clear_turn_mix(self):
        self.turn_mix = None
        self._gain("music", 1.)
        self.set_strength(self.strength)
        self.set_master(self.master)

    def _gain(self, name, target):
        previous = self.targets.get(name, target)
        self.targets[name] = target
        source = self.controls.get(name)
        if source is None or target == previous:
            return
        ok, position = self.pipeline.query_position(Gst.Format.TIME)
        if self.playing and ok and position >= 0:
            current = previous
            if name in self.ramps:
                start, initial, final = self.ramps[name]
                progress = min(1., max(0., (position - start) / GAIN_RAMP))
                current = initial + (final - initial) * progress
            source.unset_all()
            source.set(position, current)
            source.set(position + GAIN_RAMP, target)
            self.ramps[name] = (position, current, target)
        else:
            self.ramps.pop(name, None)
            source.unset_all()
            source.set(0, target)

    def _bus(self, bus, message):
        if bus == self.bus and message.type == Gst.MessageType.ERROR:
            err, _debug = message.parse_error()
            self.error = f"Lecture instrumentale interrompue : {err.message}"
            GLib.idle_add(self._stop_on_error, self.pipeline)

    def _stop_on_error(self, pipeline):
        if self.pipeline is pipeline:
            self.disable(keep_error=True)
        return GLib.SOURCE_REMOVE

    def _set_pipeline(self, stems, output):
        # Emplacements des fichiers affectés comme propriétés GStreamer : aucun
        # risque de confusion avec les espaces ou guillemets des noms de chansons.
        sink = ('pulsesink name=out sync=true volume=1.0' if output is not None else
                'audio/x-raw,format=F32LE ! appsink name=out sync=false max-buffers=4')
        pipeline = (
            f'audiomixer name=mix ! audioconvert ! audioresample ! volume name=master ! {sink} '
            'filesrc name=instrumental ! decodebin ! audioconvert ! audioresample '
            ' ! queue max-size-time=40000000 max-size-buffers=0 max-size-bytes=0 ! volume name=music ! mix. '
            'filesrc name=voicefile ! decodebin ! audioconvert ! audioresample '
            ' ! queue max-size-time=40000000 max-size-buffers=0 max-size-bytes=0 ! volume name=vocal ! mix.'
        )
        self.pipeline = Gst.parse_launch(pipeline)
        self.pipeline.get_by_name("instrumental").set_property("location", str(stems[0]))
        self.pipeline.get_by_name("voicefile").set_property("location", str(stems[1]))
        if output is not None:
            self.pipeline.get_by_name("out").set_property("device", output)
        # The two branches use segment time from the same pipeline. Gain
        # changes insert control points and never seek or flush the decoders.
        if self.turn_mix is None:
            self.clear_turn_mix()
        else:
            self.set_turn_mix(*self.turn_mix)
        for name, value in self.targets.items():
            volume = self.pipeline.get_by_name(name)
            source = GstController.InterpolationControlSource.new()
            source.set_property("mode", GstController.InterpolationMode.LINEAR)
            source.set(0, value)
            volume.add_control_binding(GstController.DirectControlBinding.new_absolute(volume, "volume", source))
            self.controls[name] = source
        self.bus = self.pipeline.get_bus()
        self.bus.add_signal_watch()
        self.bus_handler = self.bus.connect("message", self._bus)
        if self.pipeline.set_state(Gst.State.PAUSED) == Gst.StateChangeReturn.FAILURE:
            raise RuntimeError("Impossible de préparer la lecture instrumentale")
        response, state, _ = self.pipeline.get_state(3 * Gst.SECOND)
        if response == Gst.StateChangeReturn.FAILURE or state != Gst.State.PAUSED:
            raise RuntimeError("Le décodage des deux pistes a échoué")

    def set_master(self, value):
        self.master = value
        if self.turn_mix is None:
            self._gain("master", value)

    def enable(self, position=0.0, playing=False):
        if self.active:
            return True
        self.error = ""
        self.refresh_stems()
        if self.stems is None:
            self.error = "Instrumental absent : utiliser Valider la partie"
            return False
        try:
            inputs = [i for i in VocalEffect.inputs() if VocalEffect.belongs_to_strawberry(i)]
            if not inputs:
                raise RuntimeError("Strawberry ne produit pas de son actuellement")
            original_output = self.output_override or self.pactl("get-default-sink")
            self.sink = f"labfy_karaoke_mute_{os.getpid()}"
            self.module = int(self.pactl("load-module", "module-null-sink", f"sink_name={self.sink}",
                                         "sink_properties=device.description=Paroles_Source_Originale_Masquee"))
            self._set_pipeline(self.stems, original_output)
            self._seek(position)
            if playing:
                if self.pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
                    raise RuntimeError("La lecture instrumentale n'a pas démarré")
            self.playing = bool(playing)
            for item in inputs:
                idx = int(item["index"])
                self.original[idx] = int(item["sink"])
                self.pactl("move-sink-input", idx, self.sink)
            self.active = True
            return True
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, GLib.Error) as exc:
            self.error = str(exc)
            self.disable(keep_error=True)
            return False

    def _seek(self, position):
        if self.pipeline is None:
            return False
        nanos = round(max(0., float(position)) * Gst.SECOND)
        for name, source in self.controls.items():
            source.unset_all()
            source.set(0, self.targets[name])
        self.ramps.clear()
        return self.pipeline.seek_simple(Gst.Format.TIME, Gst.SeekFlags.FLUSH | Gst.SeekFlags.ACCURATE, nanos)

    def sync(self, song, position, playing):
        if not self.active:
            return
        if song != self.song:
            self.disable()
            return
        if bool(playing) != self.playing:
            status = self.pipeline.set_state(Gst.State.PLAYING if playing else Gst.State.PAUSED)
            if status == Gst.StateChangeReturn.FAILURE:
                self.error = "Impossible de synchroniser la lecture instrumentale"
                self.disable(keep_error=True)
                return
            self.playing = bool(playing)
        if playing:
            good, current = self.pipeline.query_position(Gst.Format.TIME)
            if good and abs(current / Gst.SECOND - position) > .45:
                self._seek(position)

    def poll(self):
        if not self.active:
            return
        try:
            for item in VocalEffect.inputs():
                if not VocalEffect.belongs_to_strawberry(item):
                    continue
                idx = int(item["index"])
                if idx not in self.original:
                    self.original[idx] = int(item["sink"])
                mapping = {str(name):int(ident) for ident,name,*_ in
                           (line.split("\t") for line in self.pactl("list", "short", "sinks").splitlines())}
                if mapping.get(self.sink) != int(item["sink"]):
                    self.pactl("move-sink-input", idx, self.sink)
        except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
            self.error = str(exc)
            self.disable(keep_error=True)

    def disable(self, keep_error=False):
        was_active = self.active
        self.active = False
        self.playing = False
        for idx, original in list(self.original.items()):
            try:
                current = next((i for i in VocalEffect.inputs() if int(i["index"]) == idx), None)
                if current is not None and VocalEffect.belongs_to_strawberry(current):
                    self.pactl("move-sink-input", idx, original)
            except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                try:
                    self.pactl("move-sink-input", idx, self.pactl("get-default-sink"))
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    pass
        if self.original:
            for _ in range(5):
                try:
                    active = {int(i["index"]):int(i["sink"]) for i in VocalEffect.inputs()}
                    if all(active.get(idx, orig) == orig for idx, orig in self.original.items()):
                        break
                except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired):
                    break
                time.sleep(.05)
        self.original.clear()
        if self.pipeline is not None:
            if self.bus is not None:
                self.bus.disconnect(self.bus_handler)
                self.bus.remove_signal_watch()
                self.bus = self.bus_handler = None
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
        self.controls.clear()
        self.ramps.clear()
        if self.module is not None:
            try:
                self.pactl("unload-module", self.module)
            except (OSError, RuntimeError, subprocess.TimeoutExpired):
                pass
        self.sink = self.module = None
        if not keep_error:
            self.error = ""
        return was_active
