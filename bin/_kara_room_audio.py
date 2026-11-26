"""Turn-owned wireless capture, reusing the protected Kara return monitor."""
import asyncio
from dataclasses import dataclass, field
from ipaddress import ip_address
import os
from secrets import token_hex, token_urlsafe
import socket
import struct
from subprocess import SubprocessError
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit, urlunsplit

from aiohttp import web
import numpy as np
from scipy.signal import resample_poly

from _kara_audio_calibration import selected_target
from _kara_mobile_room import RoomError
from _kara_monitor_profiles import (DEFAULTS, MIX_TRIALS, PROFILES, PROFILE_PATH, device_key,
                                    output_port, pulse_sinks, read_profiles)


@dataclass
class Route:
    owner: str
    player: str
    turn: str
    song: str
    sink: str
    device: str
    settings: dict
    volume: int
    sender: socket.socket
    claim: str | None = None
    claimed: object = None
    armed_until: float = 0.
    closed: bool = False
    receiving: bool = False
    failed_blocks: int = 0
    last_sample: float = 0.
    last_arrival: float = 0.
    muted: bool = False
    return_enabled: bool = True
    recording_consent: bool = False
    recording_on: bool = False
    connection: str | None = None
    next_metrics: float = 0.
    counters: dict = field(default_factory=dict)
    gain: float = 0.
    sent: int = 0
    clock_excess: float | None = None
    frame_origin: float | None = None
    frame_end: int | None = None
    seq: int | None = None
    scoring: bool = True
    authorized_start: float | None = None

    @property
    def context(self):
        return {"player": self.player, "turn": self.turn, "song": self.song}

    @property
    def wireless_output(self):
        return self.sink.startswith("bluez_output.")

    @property
    def return_drive(self):
        return self.settings.get("turn_return_drive", 1.)


class RoomAudio:
    def __init__(self, diagnostics):
        self.diagnostics = diagnostics
        self.lock = Lock()
        self.ready = Event()
        self.loop = None
        self.runner = None
        self.thread = None
        self.failure = None
        self.silent = False
        self.probe_url = None
        self.route = None
        self.url = None
        self.origin = None
        self.profile = "device"
        self.mix_trial_name = os.environ.get("KARA_MIX_TRIAL")
        if self.mix_trial_name and self.mix_trial_name not in MIX_TRIALS:
            raise ValueError("Profil de calibration Kara inconnu.")
        self.mix_trial = MIX_TRIALS.get(self.mix_trial_name)
        self.output = self.mix_trial.output if self.mix_trial else "default"
        self.calibrating_mix = os.environ.get("KARA_MIX_CALIBRATION") == "1"
        self.ceiling = 50 if self.calibrating_mix or self.mix_trial else 100
        # Preferences are scoped to the ephemeral room identity and physical
        # output. They cannot change the PCM consumed by the score engine.
        self.preferences = {}
        self.default_preferences = {}
        self.preference_device = None
        self.last_authorized = None

    def status(self):
        return {"available": bool(self.runner and not self.silent), "profile": self.profile, "output": self.output,
                "ceiling": self.ceiling, "guard": True, "error": self.failure, "trial": self.mix_trial_name}

    def start(self, address, network, origin, port, context, *, silent=False):
        if self.thread is not None:
            return
        from _kara_micro_test import create_app
        self.origin = origin
        self.silent = silent
        self.failure = None
        self.url = urlunsplit(("wss", f"{urlsplit(origin).hostname}:{port}", "/audio", "", ""))
        self.probe_url = self.url.removesuffix("/audio") + "/probe"
        self.ready.clear()

        def serve():
            async def run():
                self.loop = asyncio.get_running_loop()
                app = create_app("", origin, room_audio=self)

                async def probe(request):
                    if request.headers.get("Origin") != origin:
                        raise web.HTTPForbidden()
                    ws = web.WebSocketResponse(timeout=1, max_msg_size=128)
                    await ws.prepare(request)
                    await ws.send_json({"wss": True})
                    await ws.close()
                    return ws

                app.router.add_get("/probe", probe)

                @web.middleware
                async def local(request, handler):
                    if not request.remote or network is None or ip_address(request.remote) not in network:
                        raise web.HTTPForbidden()
                    return await handler(request)

                app.middlewares.insert(0, local)
                runner = web.AppRunner(app, access_log=None, shutdown_timeout=2)
                await runner.setup()
                try:
                    await web.TCPSite(runner, address, port, ssl_context=context, backlog=16).start()
                    self.runner = runner
                    self.quit = asyncio.Event()
                    self.ready.set()
                    await self.quit.wait()
                finally:
                    await runner.cleanup()
                    self.runner = None

            try:
                asyncio.run(run())
            except (OSError, ValueError):
                self.failure = "L’écouteur WSS n’a pas démarré."
            finally:
                self.ready.set()
                self.loop = None

        self.thread = Thread(target=serve, name="kara-wifi-room", daemon=True)
        self.thread.start()

    def shutdown(self):
        self.close_turn()
        with self.lock:
            self.preferences.clear()
            self.preference_device = None
        if self.loop is not None and self.runner:
            self.loop.call_soon_threadsafe(self.quit.set)
        if self.thread is not None:
            self.thread.join(timeout=4)
            if not self.thread.is_alive():
                self.thread = None

    def _route_settings(self):
        if self.loop is None or not self.runner:
            raise RoomError("Écouteur WSS indisponible.", 409)
        future = None
        try:
            # Resolve at turn admission or on an explicit preference edit.
            # Polling and the PCM path never execute system commands.
            trial = self.mix_trial
            if trial and trial.output == "default":
                targets = [entry["name"] for entry in pulse_sinks()
                           if entry.get("description") == "EDENWOOD PARTY 250 V2"
                           and entry.get("properties", {}).get("api.bluez5.codec") == "sbc"]
                if len(targets) != 1:
                    raise RoomError("Le profil attend la sortie EDENWOOD en SBC.", 409)
                sink, port = targets[0], None
            else:
                future = asyncio.run_coroutine_threadsafe(selected_target(self.output), self.loop)
                sink = future.result(timeout=1)
                port = output_port(sink)
            device = device_key(sink, port) or sink
            settings = dict(read_profiles(PROFILE_PATH).get(device, DEFAULTS))
        except RoomError:
            raise
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, TimeoutError, SubprocessError):
            if future is not None:
                future.cancel()
            raise RoomError("Sortie du retour vocal indisponible.", 409) from None
        if self.mix_trial:
            trial = self.mix_trial
            if trial.output == "analog" and port != "analog-output-speaker":
                raise RoomError("Le profil attend les haut-parleurs analogiques du Vivobook.", 409)
            settings.update(profile=trial.profile, pulse_buffer="standard", experimental=trial.experimental,
                            turn_return_drive=trial.drive)
        elif self.profile != "device":
            settings.update(profile=self.profile, pulse_buffer="standard", experimental=self.profile != "stable")
        if self.calibrating_mix and not self.mix_trial and sink.startswith("bluez_output."):
            # Calibration starts below the protected legacy loop gain.
            # The organizer can increase the ceiling in explicit steps.
            settings["turn_return_drive"] = 3.
        settings["echo_guard"] = True
        return sink, device, settings

    def open_turn(self, owner, player, turn, song, *, scoring=True, require_consent=False):
        self.close_turn()
        sink, device, settings = self._route_settings()
        sender, reader = socket.socketpair(socket.AF_UNIX, socket.SOCK_DGRAM)
        sender.setblocking(False)
        with self.lock:
            self.preference_device = device
            volume = self.preferences.get((owner, device),
                                          self.default_preferences.get(owner, min(100, settings["volume_percent"])))
            self.route = Route(owner, player, turn, song, sink, device, settings, volume, sender, scoring=scoring)
            self.last_authorized = None
            self.route.return_enabled = not require_consent
        self.diagnostics.emit("audio", "route", player=player, turn=turn, song=song, output=sink,
                              profile=settings["profile"], guard=True, requested_percent=volume, scoring=scoring,
                              trial=self.mix_trial_name,
                              capture_settings={key: settings[key] for key in
                                                ("mic_gain_percent", "chrome_echo", "chrome_noise", "pulse_buffer")
                                                if key in settings})
        return reader

    def close_turn(self):
        with self.lock:
            route, self.route = self.route, None
            if route is None:
                return
            route.closed = True
            route.sender.close()
            ws = route.claimed
        self.diagnostics.emit("audio", "capture-end", **route.context, blocks=route.sent,
                              bridge_drops=route.failed_blocks)
        if ws is not None and self.loop:
            asyncio.run_coroutine_threadsafe(ws.close(code=4000, message=b"Tour termine"), self.loop)

    def receiving(self, *, recent=False):
        with self.lock:
            route = self.route
            return bool(route and route.receiving and (not recent or time.monotonic() - route.last_arrival < 1.5))

    def record_playing(self, playing):
        # Warm-up and the countdown still feed the live application. The
        # recorder starts only at the playing aperture of the current tour.
        with self.lock:
            if self.route:
                self.route.recording_on = playing
                if not playing:
                    self.route.authorized_start = None

    def capture_window(self, turn, begin, end=None):
        with self.lock:
            allowed = self.last_authorized
            return bool(allowed and allowed[0] == turn and allowed[1] <= begin <= allowed[2]
                        and (end is None or begin < end <= allowed[2]))

    def selected_sink(self):
        with self.lock:
            return self.route.sink if self.route else None

    def ticket(self, owner, *, consent=False):
        with self.lock:
            route = self.route
            if self.silent or route is None or route.owner != owner or route.closed or not self.runner:
                raise RoomError("Le micro est réservé au joueur du tour.", 403)
            if route.claimed is not None or route.claim is None and time.monotonic() < route.armed_until:
                raise RoomError("Ton microphone est déjà connecté.", 409)
            route.claim = token_urlsafe(24)
            route.recording_consent = consent is True
            route.armed_until = time.monotonic() + 45.
            return {"url": self.url, "ticket": route.claim, "volume": route.volume,
                    "capture": route.settings["mic_gain_percent"], "echo": route.settings["chrome_echo"],
                    "noise": route.settings["chrome_noise"]}

    def claim(self, ticket):
        with self.lock:
            route = self.route
            if (self.silent or route is None or route.armed_until <= time.monotonic() or route.claim is None
                    or ticket != route.claim or route.claimed is not None):
                raise web.HTTPForbidden(text="Tour microphone invalide")
            route.claim = None
            # Before attach() the request owns a short handshake slot.
            # A failed client can obtain a new ticket when that slot expires.
            route.armed_until = time.monotonic() + 8
            return route

    def attach(self, route, ws):
        with self.lock:
            if route is not self.route or route.claimed is not None:
                return False
            route.claimed = ws
            route.connection = token_hex(16)
            route.authorized_start = None
            route.receiving = False
            route.gain = 0.
            route.seq = None
            route.clock_excess = None
            route.frame_origin = None
            route.frame_end = None
            route.next_metrics = 0.
            route.counters.clear()
            self.diagnostics.emit("audio", "microphone-connected", **route.context,
                                  connection=route.connection, voice_consent=route.recording_consent)
            return True

    def detach(self, route, ws, *, reason="closed", code=None):
        with self.lock:
            if route.claimed is ws:
                route.claimed = None
                route.receiving = False
                self.diagnostics.emit("audio", "disconnect", **route.context, reason=reason, code=code,
                                      connection=route.connection)
                route.recording_consent = False
            route.armed_until = 0.

    def metrics(self, route, levels):
        clock = time.monotonic()
        if clock < route.next_metrics:
            return
        route.next_metrics = clock + 2
        self.diagnostics.emit("audio", "sample", **route.context, levels=levels, connection=route.connection)
        for counter, kind in (("queue_drops", "overflow"), ("transport_stale_drops", "capture-gap"),
                              ("sequence_missing", "capture-gap"), ("pulse_underruns", "xrun"),
                              ("clipped", "clipping")):
            current = levels.get(counter)
            if type(current) is not int:
                continue
            increase = current - route.counters.get(counter, 0)
            if increase > 0:
                self.diagnostics.emit("audio", kind, **route.context, counter=counter, increase=increase,
                                      connection=route.connection)
            route.counters[counter] = current

    def return_pcm(self, route, pcm, sequence):
        with self.lock:
            if route is self.route and route.recording_on and route.recording_consent:
                self.diagnostics.pcm("return", 48000, 2, pcm, sequence=sequence, **route.context,
                                     connection=route.connection)

    def consented(self):
        with self.lock:
            return bool(self.route and self.route.recording_consent)

    def record_received(self, route, data, rate, received, sequence, send_ms, frame_end):
        # Capture decoded WebSocket payload before freshness, frame ordering,
        # bridge and scoring. Rejections remain observable at their own boundary.
        with self.lock:
            if route is self.route and route.recording_on and route.recording_consent:
                self.diagnostics.pcm("wifi", rate, 1, data, sequence=sequence, **route.context,
                                     connection=route.connection,
                                     packet_timing={"frame_end": frame_end, "send_ms": send_ms, "received_s": received})

    def allowed(self, route, ws):
        with self.lock:
            return route is self.route and route.claimed is ws

    def apply_return(self, route, samples):
        # Audio samples headed to Whisper have already taken the independent
        # capture route. Only the playback stream uses this bounded ramp.
        with self.lock:
            exponent = 2 if route.wireless_output and route.return_drive > 1 else 3
            target = (min(route.volume, self.ceiling) / 100.) ** exponent if self._return_enabled(route) else 0.
        first = route.gain
        route.gain += (target - first) * min(1., len(samples) / 2400.)
        return samples * np.linspace(first, route.gain, len(samples), dtype=np.float32)

    def applied(self, route):
        with self.lock:
            return min(route.volume, self.ceiling) if self._return_enabled(route) else 0

    def _return_enabled(self, route):
        return route is self.route and route.return_enabled and not route.muted

    def enable_return(self):
        with self.lock:
            if self.route:
                self.route.return_enabled = True

    def requested(self, route):
        with self.lock:
            return route.volume if route is self.route else 0

    def muted(self, route):
        with self.lock:
            return route is self.route and route.muted

    def volume(self, route, value):
        if value == "reset":
            value = min(route.settings["volume_percent"], 100)
        else:
            try:
                value = int(value)
            except ValueError:
                return
        if not 0 <= value <= 100:
            return
        with self.lock:
            if route is not self.route:
                return
            route.volume = value
            route.muted = False
            self.preferences[(route.owner, route.device)] = value
            applied = min(value, self.ceiling)
        self.diagnostics.emit("audio", "voice-volume", **route.context, requested=value, applied=applied)

    def administrate(self, action, value):
        if action == "audio-cut":
            self.mute_return()
        elif action == "audio-ceiling" and type(value) is int and 0 <= value <= 100:
            if self.mix_trial and value > self.mix_trial.ceiling:
                raise RoomError("Le niveau dépasse le plafond de ce profil de calibration.")
            with self.lock:
                self.ceiling = value
        elif action == "audio-profile" and (value == "device" or isinstance(value, str) and value in PROFILES):
            if self.mix_trial and value != "device":
                raise RoomError("Le lecteur est fixé par le profil de calibration.")
            self.profile = value
        elif action == "audio-output" and value in ("default", "analog"):
            if self.mix_trial and value != self.mix_trial.output:
                raise RoomError("La sortie est fixée par le profil de calibration.")
            self.output = value
            with self.lock:
                self.preference_device = None
        else:
            raise RoomError("Réglage de retour invalide.")
        self.diagnostics.emit("audio", action, value=value)

    def set_player_volume(self, owner, player, percent):
        if type(percent) is not int or not 0 <= percent <= 100:
            raise RoomError("Volume du joueur invalide.")
        with self.lock:
            current = self.route
        if current and current.owner == owner:
            self.volume(current, str(percent))
        else:
            with self.lock:
                device = self.preference_device
            if device is None:
                _, device, _ = self._route_settings()
            with self.lock:
                self.preference_device = device
                self.preferences[(owner, device)] = percent
            self.diagnostics.emit("audio", "voice-preference", player=player, requested=percent)

    def preferred_volume(self, owner):
        with self.lock:
            if self.route and self.route.owner == owner:
                return self.route.volume
            return self.preferences.get((owner, self.preference_device), self.default_preferences.get(owner))

    def mute_return(self):
        with self.lock:
            route = self.route
            if route:
                route.muted = True
                self.diagnostics.emit("audio", "return-cut", **route.context)

    def default_player_volume(self, owner, percent):
        with self.lock:
            self.default_preferences[owner] = percent

    def reset_player_volume(self, owner):
        self.forget_player(owner)
        with self.lock:
            route = self.route
        if route and route.owner == owner:
            self.volume(route, "reset")

    def forget_player(self, owner):
        with self.lock:
            self.default_preferences.pop(owner, None)
            for key in tuple(self.preferences):
                if key[0] == owner:
                    self.preferences.pop(key)

    def pcm(self, route, data, rate, received, sequence, send_clock, frame_end=None):
        # Training keeps Wi-Fi capture and the return. With no Whisper
        # consumer, sending to its socket would discard good microphone blocks.
        if route.scoring:
            reading = np.frombuffer(data, dtype="<i2").astype(np.float32)
            scaled = resample_poly(reading, 1, 3) if rate == 48000 else resample_poly(reading, 160, 441)
            pcm = np.clip(np.rint(scaled), -32768, 32767).astype("<i2").tobytes()
        with self.lock:
            if route is not self.route:
                return
            if frame_end is not None:
                # Render-frame positions preserve PCM spacing. Reception
                # anchors each activation once; callback jitter cannot punch
                # holes in contiguous blocks. Real frame jumps remain holes.
                first_frame = frame_end - len(data) // 2
                if first_frame < 0 or route.frame_end is not None and first_frame < route.frame_end:
                    return
                if route.frame_origin is None:
                    route.frame_origin = received - frame_end / rate
                end = route.frame_origin + frame_end / rate
                if end > received + .250:
                    return
                route.frame_end = frame_end
            else:
                transit = received - send_clock / 1000.
                route.clock_excess = transit if route.clock_excess is None else min(route.clock_excess, transit)
                end = send_clock / 1000. + route.clock_excess
            if end <= route.last_sample:
                return
            route.receiving = True
            route.last_arrival = received
            if route.scoring:
                try:
                    route.sender.send(struct.pack("<d", end) + pcm)
                except (BlockingIOError, OSError):
                    route.failed_blocks += 1
                    return
            route.last_sample = end
            route.seq = sequence
            route.sent += 1
            timing = {"frame_end": frame_end, "send_ms": send_clock, "received_s": received}
            if route.recording_on and route.recording_consent and route.scoring:
                # The preceding block can contain warm-up samples. Use the
                # end of the first admitted block as the conservative bound.
                if route.authorized_start is None:
                    route.authorized_start = end
                self.last_authorized = (route.turn, route.authorized_start, end)
                self.diagnostics.pcm("whisper", 16000, 1, pcm, capture_end=end, sequence=sequence,
                                     packet_timing=timing, connection=route.connection, **route.context)
