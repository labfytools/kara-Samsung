#!/usr/bin/env python3
"""Local HTTPS microphone monitor: phone -> Wi-Fi -> current PipeWire sink."""
import asyncio
import fcntl
import hmac
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import ssl
import subprocess
import struct
import sys
from tempfile import NamedTemporaryFile
import time

import numpy as np
from scipy.signal import firwin, resample_poly
from aiohttp import WSMsgType, web
from _kara_audio_calibration import ANALOG_TARGET, await_owned, calibration_audio, close_process, selected_target
from _kara_blackbox_clock import browser_anchor
from _kara_monitor_profiles import (DEFAULTS, ECHO_GUARD_DB, PROFILE_PATH, PROFILES, RETURN_PEAK,
                                    device_key, output_port, read_profiles)
from _kara_voice_latency import RelativeTransit, SubmissionCadence, TimingWindow, TransportFreshness, player_backlog

ROOT = Path.home() / ".local/state/kara/micro-test"
PORT = 8766
ACTIVE = asyncio.Lock()
ACTIVE_SESSION = {"ws": None}
PAGE = Path(__file__).with_name("_kara_micro_test.html")

MONITOR_VERSION = 2
PW_LATENCY_MS = 10
PULSE_LATENCY_MS = 20
PULSE_SHORT_LATENCY_MS = 15
PULSE_PROCESS_MS = 5
PULSE_BUFFER_METRICS = re.compile(
    rb"Buffer metrics: maxlength=(\d+), tlength=(\d+), prebuf=(\d+), minreq=(\d+)")
MONITOR_NODE = "kara-microphone"
RESAMPLE_UP, RESAMPLE_DOWN = 160, 147
RESAMPLE_DELAY = 12
MONITOR_FIR = firwin(3201, 1 / RESAMPLE_UP, window=("kaiser", 5.0)).astype(np.float32)
DEFAULT_MONITOR_VOLUME = 50


def monitor_peak(guard):
    # Reduce the gain on every acoustic round trip. Fold it into the existing
    # shaper so every sample is still emitted immediately, even in quiet speech.
    return RETURN_PEAK * 10 ** (-ECHO_GUARD_DB / 20) if guard else RETURN_PEAK


def shape_monitor(samples, peak, drive=1.):
    # The guard preserves the legacy small-signal loop gain and ceiling.
    # Leave speech linear up to the soft knee, then limit without lookahead.
    signal = samples * (2.0 * drive)
    knee = .65
    magnitude = np.abs(signal)
    limited = np.minimum(magnitude, knee) + (1. - knee) * np.tanh(
        np.maximum(magnitude - knee, 0.) / (1. - knee))
    return np.copysign(limited * peak, signal)


def resample_live_block(samples, history):
    # Keep the same FIR response between 10ms blocks. A history of one phase
    # period (147 input / 160 output frames) keeps the polyphase grid aligned.
    # Using a 12-frame output delay makes the symmetric FIR causal here:
    # its half-length spans only 1600/147 < 12 output frames.
    converted = resample_poly(np.concatenate((history, samples)), RESAMPLE_UP,
                              RESAMPLE_DOWN, window=MONITOR_FIR)
    history[:] = samples[-RESAMPLE_DOWN:]
    return converted[RESAMPLE_UP - RESAMPLE_DELAY:-RESAMPLE_DELAY]


async def read_pulse_status(stderr, status):
    # pacat reports actual stream underruns on stderr in verbose mode.
    # Discard unrelated messages, bound partial lines, and never log raw output.
    pending = b""
    while chunk := await stderr.read(1024):
        lines = (pending + chunk).replace(b"\r", b"\n").split(b"\n")
        pending = lines.pop()[-512:]
        for line in lines:
            if b"Stream underrun." in line:
                status["pulse_underruns"] += 1
            elif match := PULSE_BUFFER_METRICS.search(line):
                status["pulse_buffer_bytes"] = dict(zip(
                    ("maxlength", "tlength", "prebuf", "minreq"), map(int, match.groups())))


def set_stream_volume(stream_name, percent, *, verify=False, sink_index=None):
    # Match the unique live stream owned by this session, even if other pw-cat
    # instances or Strawberry are present. Re-resolve the index on each change.
    def owned_stream():
        inputs = json.loads(subprocess.check_output(
            ("pactl", "--format=json", "list", "sink-inputs"), timeout=2))
        owned = [entry for entry in inputs if entry["properties"].get("node.name") == stream_name]
        if len(owned) == 1 and type(owned[0]["index"]) is int and owned[0]["index"] >= 0:
            return owned[0]
        return {}

    def unit_gain(stream):
        channels = stream.get("volume", {})
        return bool(channels) and all(value["value"] == 65536 for value in channels.values())

    def on_output(stream):
        return sink_index is None or (stream.get("sink") == sink_index and stream.get("mute") is False)

    try:
        stream = owned_stream()
        if not stream or not on_output(stream):
            return False
        if verify and unit_gain(stream):
            return True
        index = stream["index"]
        subprocess.run(("pactl", "set-sink-input-volume", str(index),
                        f"{percent}%"), check=True, timeout=2,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not verify:
            return True
        verified = owned_stream()
        return verified.get("index") == index and on_output(verified) and unit_gain(verified)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        return False


async def prepare_room_stream(player, stream_name, *, native, sink_index):
    # A native pw-cat may block on its first stdin read, delaying the graph's
    # acknowledgement of the volume change. Clock one silent 10 ms block at
    # a time during startup only. Live microphone PCM remains unqueued here.
    silent = bytes(480 * 2 * 2)
    for attempt in range(2):
        gain = asyncio.create_task(asyncio.to_thread(
            set_stream_volume, stream_name, 100, verify=True, sink_index=sink_index))
        try:
            if native:
                await asyncio.wait((gain,), timeout=.02)
                until = time.monotonic() + 2.
                while not gain.done() and time.monotonic() < until:
                    player.stdin.write(silent)
                    await asyncio.wait_for(player.stdin.drain(), timeout=.3)
                    await asyncio.wait((gain,), timeout=.01)
            if await gain:
                return True
            if attempt == 0:
                # Pulse and native policy may publish the sink input just
                # after the player starts. Retry with a silent block only.
                player.stdin.write(silent)
                await asyncio.wait_for(player.stdin.drain(), timeout=.3)
                await asyncio.sleep(.10)
        except (OSError, TimeoutError):
            return False
        finally:
            # No volume command may outlive the stream identity it targets.
            await asyncio.gather(gain, return_exceptions=True)
    return False


def lan_ip():
    data = json.loads(subprocess.check_output(
        ["ip", "-j", "-4", "route", "get", "1.1.1.1"], timeout=3))
    candidate = data[0].get("prefsrc") or data[0].get("src")
    address = ipaddress.ip_address(candidate)
    if not address.is_private or address.is_loopback:
        raise RuntimeError("Pas d'adresse LAN privée")
    return str(address)


def load_token(*, rotate=False):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / "token"
    if not rotate:
        try:
            with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW), "r", encoding="ascii") as source:
                if os.fstat(source.fileno()).st_mode & 0o077:
                    raise OSError("Le jeton Micro Test doit avoir les permissions 0600.")
                value = source.read(129).strip()
            if not re.fullmatch(r"[a-zA-Z0-9_-]{32}", value):
                raise ValueError("Jeton Micro Test invalide.")
            return value
        except FileNotFoundError:
            pass
    # Write with mode 0600 from creation. A crash before publication keeps
    # the previous credential usable until the explicit rotation completes.
    draft = None
    value = secrets.token_urlsafe(24)
    try:
        with NamedTemporaryFile(mode="w", encoding="ascii", dir=ROOT, delete=False) as target:
            draft = Path(target.name)
            target.write(value + "\n")
            target.flush()
            os.fsync(target.fileno())
        if rotate:
            draft.replace(path)
        else:
            try:
                os.link(draft, path)
            except FileExistsError:
                return load_token()
        descriptor = os.open(ROOT, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        return value
    finally:
        if draft is not None:
            draft.unlink(missing_ok=True)


def browser_diagnostics(message):
    # Only fields produced by the return monitor are accepted. This snapshot
    # must never store PCM, page content, or a device's browser identifiers.
    def numbers(fields, keys):
        if not isinstance(fields, dict):
            return {}
        return {key: value for key in keys if type(value := fields.get(key)) in (int, float)
                and -2 ** 53 < value < 2 ** 53}

    try:
        data = json.loads(message)
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    capture, client = data.get("capture"), data.get("client")
    if not isinstance(capture, dict) or not isinstance(client, dict):
        return None
    settings = numbers(capture, ("context_rate_hz", "base_latency_ms", "latency", "sampleRate", "channelCount"))
    for key in ("echoCancellation", "noiseSuppression", "autoGainControl"):
        value = capture.get(key)
        if type(value) is bool or (key == "echoCancellation" and value in ("all", "remote-only")):
            settings[key] = value
    for key in ("requested_echo", "requested_noise", "requested_auto_gain"):
        if type(capture.get(key)) is bool:
            settings[key] = capture[key]
    if capture.get("platform") in ("android", "ios", "other"):
        settings["platform"] = capture["platform"]
    if capture.get("browser") in ("chromium", "firefox", "safari", "other"):
        settings["browser"] = capture["browser"]
    if capture.get("client_revision") == 3:
        settings["client_revision"] = 3
    delivery = numbers(client, ("sent", "dropped", "worklet_duplicates", "worklet_missing_frames",
                                "max_quantum_frames", "worklet_frames_per_s",
                                "buffered_bytes", "buffered_bytes_max"))
    timings = client.get("timings_ms")
    if isinstance(timings, dict):
        delivery["timings_ms"] = {
            key: numbers(timings.get(key), ("n", "p50", "p95", "p99", "max", "trend_ms_per_s"))
            for key in ("assembly_audio_ms", "worklet_age_audio_ms", "callback_gap_ms",
                        "js_before_send_ms", "ws_send_api_ms", "rtt_ms")}
    return {"capture": settings, "client": delivery}


def create_app(token, origin, *, local_diagnostics=False, profiles_path=PROFILE_PATH, room_audio=None):
    app = web.Application(client_max_size=32768)
    # Keep one in-memory snapshot from the latest return session. The
    # authenticated phone supplies aggregate measurements, while only the
    # bound host can read them back. No audio is retained by this endpoint.
    snapshot = {}

    @web.middleware
    async def headers(request, handler):
        response = await handler(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = ("default-src 'none'; "
            "script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; form-action 'none'")
        return response

    app.middlewares.append(headers)

    def authorized(request):
        supplied = request.query.get("token", "")
        return supplied.isascii() and secrets.compare_digest(supplied, token)

    async def monitor_route(choice):
        try:
            sink = await selected_target(choice)
            try:
                port = await asyncio.to_thread(output_port, sink)
            except (OSError, subprocess.SubprocessError):
                if choice != "analog":
                    raise
                # The native player still supports the explicit analog target
                # on setups without Pulse. Its active port is unverified.
                port = None
        except (OSError, ValueError, TypeError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
            raise web.HTTPServiceUnavailable(text="Sortie audio indisponible") from exc
        return sink, port

    def route_stamp(sink, port):
        # The browser gets an opaque stamp for this exact route. Storage uses
        # a separate, stable identity across reconnects and A2DP codec changes.
        return hmac.digest(token.encode(), f"{sink}\0{port or ''}".encode(), "sha256").hex()

    async def monitor_preferences(request):
        if not authorized(request):
            raise web.HTTPForbidden()
        choice = request.query.get("output", "default")
        if choice not in ("default", "analog"):
            raise web.HTTPBadRequest(text="Sortie inconnue")
        sink, port = await monitor_route(choice)
        try:
            stored = (await asyncio.to_thread(read_profiles, profiles_path)).get(device_key(sink, port))
        except (OSError, ValueError, RecursionError):
            stored = None
        settings = dict(stored or DEFAULTS)
        if settings["profile"] != "stable" and not shutil.which("pacat"):
            settings.update(profile="stable", pulse_buffer="standard")
        return web.json_response({"settings": settings, "saved": stored is not None,
                                  "route": route_stamp(sink, port)})

    async def diagnostics(request):
        address = request.transport.get_extra_info("sockname") if request.transport else None
        if not authorized(request) or not address or request.remote != address[0]:
            raise web.HTTPForbidden()
        return web.json_response(snapshot)

    async def page(request):
        if not authorized(request):
            raise web.HTTPForbidden(text="Lien de test microphone invalide")
        return web.Response(body=PAGE.read_bytes(), content_type="text/html")

    async def worklet(request):
        if not authorized(request):
            raise web.HTTPForbidden(text="Module audio non autorise")
        return web.Response(body=PAGE.with_name("_kara_micro_worklet.js").read_bytes(),
                            content_type="text/javascript")

    async def calibration_script(request):
        # Public, nonsecret DSP code; no cross-origin permission is granted.
        path = PAGE.with_name("_kara_audio_calibration.js")
        if not path.is_file():
            raise web.HTTPNotFound()
        return web.Response(body=path.read_bytes(), content_type="text/javascript")

    async def audio(request):
        if request.headers.get("Origin") != origin or (room_audio is None and not authorized(request)):
            raise web.HTTPForbidden(text="Session microphone non autorisée")
        route = room_audio.claim(request.query.get("token", "")) if room_audio else None
        output_choice = request.query.get("output", "default") if route is None else "default"
        if output_choice not in ("default", "analog"):
            raise web.HTTPBadRequest(text="Sortie inconnue")
        try:
            capture_rate = int(request.query.get("rate", "48000"))
        except ValueError:
            raise web.HTTPBadRequest(text="Frequence invalide")
        if capture_rate not in (44100, 48000):
            raise web.HTTPBadRequest(text="Frequence non prise en charge")
        if room_audio is None and request.query.get("calibration") == "1":
            return await calibration_audio(request, ACTIVE, ACTIVE_SESSION, output_choice)
        profile = route.settings["profile"] if route else request.query.get("profile", "stable")
        if profile not in PROFILES:
            raise web.HTTPBadRequest(text="Profil de retour inconnu")
        block_ms, queue_packets = PROFILES[profile]
        pcm_bytes = capture_rate * block_ms // 1000 * 2
        # In a room, the saved device profile owns the choice of playback
        # backend. A low-latency packet size alone does not enable Pulse.
        experimental = profile != "stable" and (route is None or route.settings["experimental"])
        requested_buffer = route.settings["pulse_buffer"] if route else request.query.get("pulse_buffer", "standard")
        if route and not experimental:
            requested_buffer = "standard"
        if requested_buffer not in ("standard", "short") or (requested_buffer == "short" and not experimental):
            raise web.HTTPBadRequest(text="Tampon du lecteur incompatible")
        pulse_latency = PULSE_SHORT_LATENCY_MS if requested_buffer == "short" else PULSE_LATENCY_MS
        guard_arg = "1" if route else request.query.get("echo_guard", "1")
        if guard_arg not in ("0", "1"):
            raise web.HTTPBadRequest(text="Protection acoustique invalide")
        echo_guard = guard_arg == "1"
        peak = monitor_peak(echo_guard)
        max_volume = 100 if echo_guard else 180

        def selected_volume(value):
            return max(50, min(max_volume, int(value)))

        try:
            mic_volume = 100 if route else selected_volume(request.query.get("volume", str(DEFAULT_MONITOR_VOLUME)))
        except ValueError:
            raise web.HTTPBadRequest(text="Volume de retour invalide")
        if ACTIVE_SESSION.get("calibration"):
            raise web.HTTPConflict(text="Calibration audio en cours")
        # Reserve the latest authenticated start before any await. A reconnect
        # may arrive before the previous player has even published its socket.
        offer = object()
        ACTIVE_SESSION["monitor_offer"] = offer
        sink_target = route.sink if route else ANALOG_TARGET if output_choice == "analog" else None
        stamp = None if route else request.query.get("route")
        try:
            if stamp is not None:
                sink_target, active_port = await monitor_route(output_choice)
                if not re.fullmatch("[0-9a-f]{64}", stamp) or not hmac.compare_digest(
                        stamp, route_stamp(sink_target, active_port)):
                    raise web.HTTPConflict(text="La sortie a changé")
            if ACTIVE_SESSION.get("monitor_offer") is not offer:
                raise web.HTTPConflict(text="Demande remplacée par une nouvelle session micro")
            if ACTIVE_SESSION.get("calibration"):
                raise web.HTTPConflict(text="Calibration audio en cours")
            previous = ACTIVE_SESSION["ws"] if ACTIVE.locked() else None
            if previous is not None and route is None:
                await previous.close(code=4000, message=b"Nouvelle session micro")
            async with asyncio.timeout(5):
                await ACTIVE.acquire()
        except (TimeoutError, asyncio.CancelledError) as exc:
            if ACTIVE_SESSION.get("monitor_offer") is offer:
                ACTIVE_SESSION.pop("monitor_offer")
            if isinstance(exc, TimeoutError):
                raise web.HTTPServiceUnavailable(text="Micro precedent en cours de fermeture") from exc
            raise
        except web.HTTPException:
            if ACTIVE_SESSION.get("monitor_offer") is offer:
                ACTIVE_SESSION.pop("monitor_offer")
            raise
        if ACTIVE_SESSION.get("monitor_offer") is not offer:
            ACTIVE.release()
            raise web.HTTPConflict(text="Demande remplacée par une nouvelle session micro")

        ws = web.WebSocketResponse(heartbeat=8, timeout=1.0, max_msg_size=16384,
                                  receive_timeout=4.0 if route else None)
        # A preference-aware stream keeps the device it was started for. A
        # subsequent activation creates fresh capture, queue and filter state.
        pinned = ("node.dont-move=true", "node.dont-fallback=true", "node.dont-reconnect=true") if stamp or route else ()
        stream_name = f"{MONITOR_NODE}-{secrets.token_hex(8)}"
        if experimental:
            # Unlike raw pw-cat's blocking fread in its process callback,
            # pacat reads stdin when the Pulse stream requests writable data.
            # The existing PipeWire Pulse server owns playback and prebuffer.
            initial_volume = min(65536, round(65536 * mic_volume / 100))
            cmd = ["pacat", "--playback", "--raw", "--format=s16le", "--rate=48000",
                   "--channels=2", f"--latency-msec={pulse_latency}",
                   f"--process-time-msec={PULSE_PROCESS_MS}", "--verbose",
                   f"--device={sink_target or '@DEFAULT_SINK@'}", f"--volume={initial_volume}",
                   "--client-name=Kara retour micro",  # Own Pulse stream-restore state.
                   f"--stream-name={MONITOR_NODE}", f"--property=node.name={stream_name}"]
            cmd.extend(f"--property={value}" for value in pinned)
        else:
            # pw-cat takes a linear amplitude, while pactl uses the cubic
            # PulseAudio volume scale. Set a conservative gain from first audio.
            initial_volume = (min(mic_volume, 100) / 100) ** 3
            cmd = ["pw-cat", "--playback", "--raw", "--format", "s16",
                   "--rate", "48000", "--channels", "2", "--latency", f"{PW_LATENCY_MS}ms",
                   "--properties", " ".join((f"node.name={stream_name}", f"media.name={MONITOR_NODE}", *pinned)),
                   "--volume", f"{initial_volume:.8f}"]
            if sink_target:
                cmd += ["--target", sink_target]
            cmd.append("-")
        player = None
        playback_task = None
        volume_task = None
        status_task = None
        pulse_status = {"pulse_underruns": 0 if experimental else None, "pulse_buffer_bytes": None}
        monitor_enabled = True
        disconnect_reason = "closed"
        clock_probe = None
        clock_sequence = 0
        queue = asyncio.Queue(maxsize=queue_packets)
        history = np.zeros(RESAMPLE_DOWN, dtype=np.float32) if experimental and capture_rate == 44100 else None
        audio_stats = {"rms": 0.0, "peak": 0.0, "clipped": 0, "frames": 0}
        delivery_stats = {"rx_packets": 0, "queue_drops": 0, "invalid_packets": 0, "transport_stale_drops": 0}
        network_stats = {"last_rx": None, "gaps_over_40": 0,
                         "gaps_over_100": 0, "max_gap_ms": 0.0, "bursts_under_5": 0}
        transport = {"last_seq": None, "missing": 0, "reordered": 0}
        transit = RelativeTransit()
        freshness = TransportFreshness(queue_packets * block_ms) if experimental else None
        cadence = SubmissionCadence()
        distributions = {key: TimingWindow() for key in
                         ("arrival_gap_ms", "receive_ms", "queue_ms", "dsp_encode_ms",
                          "stdin_write_ms", "stdin_write_drain_ms")}
        backlog_windows = {key: TimingWindow() for key in ("asyncio_pcm_ms", "kernel_pipe_pcm_ms")}
        queue_high_water = 0
        timing = {"start": time.monotonic(), "ready_ms": 0.0,
                  "write_count": 0, "write_total_ms": 0.0,
                  "dsp_total_ms": 0.0, "dsp_max_ms": 0.0,
                  "write_max_ms": 0.0, "queue_wait_max_ms": 0.0,
                  "queue_wait_last_ms": 0.0, "write_over_10ms": 0}
        fed = False
        wire_framed = None
        last_browser_diagnostics = 0.0
        idle_since = None
        starvations = 0
        starvation_ms = 0.0
        # Allow scheduler/Worklet-quantum jitter before flagging a supply wait.
        # This observes the queue reader only. PipeWire may still have PCM.
        supply_grace_ms = block_ms + 5

        def supply_wait_ms(now):
            if idle_since is None:
                return 0.0
            return max(0.0, (now - idle_since) * 1000 - supply_grace_ms)

        async def apply_volume():
            # Slider events coalesce. Keep this task owned until the thread
            # returns so teardown cannot target a reused stream index.
            while True:
                requested = mic_volume
                await asyncio.to_thread(set_stream_volume, stream_name, requested)
                if mic_volume == requested:
                    return

        async def playback():
            nonlocal fed, idle_since
            while True:
                if monitor_enabled and fed and queue.empty():
                    idle_since = time.monotonic()
                enqueued_at, data, sequence = await queue.get()
                dsp_started = time.monotonic()
                dsp_ms_clock = dsp_started * 1000
                queued_ms = (dsp_started - enqueued_at) * 1000
                distributions["queue_ms"].append(dsp_ms_clock, queued_ms)
                timing["queue_wait_last_ms"] = queued_ms
                timing["queue_wait_max_ms"] = max(timing["queue_wait_max_ms"], queued_ms)
                samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                audio_stats["rms"] = float(np.sqrt(np.mean(samples * samples)))
                magnitude = np.abs(samples)
                audio_stats["peak"] = float(np.max(magnitude))
                audio_stats["clipped"] += int(np.count_nonzero(magnitude >= 0.995))
                audio_stats["frames"] += len(samples)
                # AudioWorklet sends native 48k PCM (or 44.1k on some phones).
                if capture_rate == 44100:
                    if history is None:
                        samples = resample_poly(samples, RESAMPLE_UP, RESAMPLE_DOWN)
                    else:
                        samples = resample_live_block(samples, history)
                if route:
                    audio_stats["resampled_rms"] = float(np.sqrt(np.mean(samples * samples)))
                samples = shape_monitor(samples, peak, route.return_drive if route else 1.)
                if route:
                    audio_stats["guarded_rms"] = float(np.sqrt(np.mean(samples * samples)))
                    audio_stats["guarded_peak"] = float(np.max(np.abs(samples)))
                    samples = room_audio.apply_return(route, samples)
                    audio_stats["return_rms"] = float(np.sqrt(np.mean(samples * samples)))
                    audio_stats["return_peak"] = float(np.max(np.abs(samples)))
                stereo = np.repeat(samples[:, None], 2, axis=1)
                pcm = (stereo * 32767).astype("<i2").tobytes()
                if route:
                    room_audio.return_pcm(route, pcm, sequence)
                write_started = time.monotonic()
                dsp_ms = (write_started - dsp_started) * 1000
                distributions["dsp_encode_ms"].append(dsp_ms_clock, dsp_ms)
                timing["dsp_total_ms"] += dsp_ms
                timing["dsp_max_ms"] = max(timing["dsp_max_ms"], dsp_ms)
                try:
                    if player.returncode is not None:
                        raise BrokenPipeError
                    player.stdin.write(pcm)
                    write_returned = time.monotonic()
                    distributions["stdin_write_ms"].append(
                        write_returned * 1000, (write_returned - write_started) * 1000)
                    await asyncio.wait_for(player.stdin.drain(), timeout=0.3)
                    drained_at = time.monotonic()
                    if route and audio_stats["return_peak"] >= .001:
                        room_audio.return_submitted(route, drained_at)
                    audio_stats["meter_at"] = drained_at
                    write_ms = (drained_at - write_started) * 1000
                    distributions["stdin_write_drain_ms"].append(drained_at * 1000, write_ms)
                    cadence.observe(drained_at * 1000, len(pcm) // 4)
                    timing["write_count"] += 1
                    timing["write_total_ms"] += write_ms
                    timing["write_max_ms"] = max(timing["write_max_ms"], write_ms)
                    fed = monitor_enabled
                    if write_ms > 10:
                        timing["write_over_10ms"] += 1
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                    await ws.close(code=1011, message=b"Lecteur PipeWire interrompu")
                    return

        try:
            # Shield creation so a cancellation cannot orphan a child whose
            # handle arrives just after the WebSocket request was cancelled.
            async def start_player():
                nonlocal player
                options = ({"pipesize": os.sysconf("SC_PAGE_SIZE"),
                            "limit": 4096, "env": {**os.environ, "LC_ALL": "C"}}
                           if experimental else {})
                player = await asyncio.wait_for(asyncio.create_subprocess_exec(
                    *cmd, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.PIPE if experimental else asyncio.subprocess.DEVNULL,
                    **options), timeout=2)

            try:
                await await_owned(asyncio.create_task(start_player()))
            except (OSError, asyncio.TimeoutError) as exc:
                raise web.HTTPServiceUnavailable(text="Lecteur PipeWire non disponible") from exc
            stdin_transport = getattr(player.stdin, "transport", None)
            pipe = None
            pipe_bytes = None
            if stdin_transport is not None:
                pipe = stdin_transport.get_extra_info("pipe")
                pipe_bytes = fcntl.fcntl(pipe, fcntl.F_GETPIPE_SZ)
                if experimental:
                    # At most one PCM write can wait in asyncio. drain() still
                    # means delivery to the kernel, never acoustic playback.
                    stdin_transport.set_write_buffer_limits(high=0, low=0)
            if experimental:
                status_task = asyncio.create_task(read_pulse_status(player.stderr, pulse_status))
            await asyncio.sleep(0.08)
            if player.returncode is not None:
                raise web.HTTPServiceUnavailable(text="Lecteur PipeWire non disponible")
            if route:
                # Stream restore can silently attenuate even a pw-cat
                # launched at 1. Set and verify unit gain before admitting
                # PCM. Only the room's protected DSP owns the return fader.
                unit_gain = await await_owned(asyncio.create_task(
                    prepare_room_stream(player, stream_name, native=not experimental,
                                        sink_index=route.settings["output_index"])))
                if not unit_gain:
                    room_audio.diagnostics.emit("audio", "error", **route.context, operation="return-stream-gain")
                    raise web.HTTPServiceUnavailable(text="Sortie du retour ou gain PipeWire non vérifié")
                room_audio.diagnostics.emit("audio", "return-stream-gain", **route.context, applied_db=0.,
                                            output_verified=True)
            if ACTIVE_SESSION.get("monitor_offer") is not offer:
                raise web.HTTPConflict(text="Demande remplacée par une nouvelle session micro")
            await ws.prepare(request)
            if ACTIVE_SESSION.get("monitor_offer") is not offer or (route and not room_audio.attach(route, ws, output_verified=True)):
                await ws.close(code=4000, message=b"Nouvelle session micro")
                return ws
            ACTIVE_SESSION["ws"] = ws
            playback_task = asyncio.create_task(playback())
            # Do not block microphone activation on pactl/volume discovery.
            # Read audio while PipeWire independently sets the mic-only gain.
            if route is None:
                volume_task = asyncio.create_task(apply_volume())
            timing["ready_ms"] = (time.monotonic() - timing["start"]) * 1000
            settings = {"ready": True,
                                "server_ready_ms": round(timing["ready_ms"], 1),
                                "device": "Sortie analogique du PC" if sink_target == ANALOG_TARGET
                                          else "Sortie PipeWire par defaut",
                                "mode": "pipewire-pulse-48k-stereo" if experimental
                                        else "pipewire-native-48k-stereo",
                                "profile": profile, "monitor_version": MONITOR_VERSION,
                                "echo_guard": echo_guard,
                                "echo_guard_attenuation_db": ECHO_GUARD_DB if echo_guard else 0,
                                "block_ms": block_ms, "queue_max_ms": queue_packets * block_ms,
                                "requested_latency_ms": pulse_latency if experimental else PW_LATENCY_MS,
                                "requested_process_ms": PULSE_PROCESS_MS if experimental else None,
                                "pipe_bytes": pipe_bytes,
                                "supply_grace_ms": supply_grace_ms}
            if local_diagnostics:
                settings["local_diagnostics"] = True
                snapshot.clear()
                snapshot.update(settings=settings, volume_percent=mic_volume, active=True)
            if route:
                settings["applied_percent"] = room_audio.applied(route)
                settings["requested_percent"] = room_audio.requested(route)
                settings["return_muted"] = room_audio.muted(route)
                settings["device"] = route.settings["output_description"]
                settings["output_verified"] = True
            await ws.send_json(settings)
            async for msg in ws:
                if route and not room_audio.allowed(route, ws):
                    disconnect_reason = "route-ended"
                    break
                if route and time.monotonic() - (network_stats["last_rx"] or timing["start"]) > 4.:
                    disconnect_reason = "no-pcm"
                    await ws.close(code=4001, message=b"Flux PCM interrompu")
                    break
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    header = (24 if route and len(data) == pcm_bytes + 24 and data.startswith(b"KTS2")
                              else 16 if len(data) == pcm_bytes + 16 and data.startswith(b"KTS1") else 0)
                    framed = bool(header)
                    # The original stable client may send a bare 20ms block.
                    # Wire mode is fixed after the first valid block so legacy
                    # packets cannot bypass sequence checks mid-session.
                    if ((wire_framed is None or wire_framed == header)
                            and (framed or (route is None and profile == "stable" and len(data) == pcm_bytes))):
                        received_at = time.monotonic()
                        frame_end = None
                        if framed:
                            seq, send_ms = struct.unpack_from("<Id", data, 4)
                            if not math.isfinite(send_ms) or not 0 <= send_ms < 2 ** 53:
                                delivery_stats["invalid_packets"] += 1
                                continue
                            if header == 24:
                                frame_end, = struct.unpack_from("<Q", data, 16)
                                if not pcm_bytes // 2 <= frame_end < 2 ** 53:
                                    delivery_stats["invalid_packets"] += 1
                                    continue
                            if route:
                                room_audio.record_received(route, memoryview(data)[header:], capture_rate,
                                                           received_at, seq, send_ms, frame_end)
                            previous = transport["last_seq"]
                            # TCP preserves order. Never replay a duplicate or
                            # move the sequence cursor back on a malformed client.
                            step = seq + 1 if previous is None else (seq - previous) & 0xffffffff
                            if previous is not None and (step == 0 or step >= 0x80000000):
                                transport["reordered"] += 1
                                delivery_stats["duplicate_packets"] = delivery_stats.get("duplicate_packets", 0) + (step == 0)
                                continue
                            transport["missing"] += step - 1
                            transport["last_seq"] = seq
                            data = memoryview(data)[header:]
                            transit.observe(send_ms, received_at * 1000)
                        wire_framed = header
                        delivery_stats["rx_packets"] += 1
                        if network_stats["last_rx"] is not None:
                            gap_ms = (received_at - network_stats["last_rx"]) * 1000
                            distributions["arrival_gap_ms"].append(received_at * 1000, gap_ms)
                            network_stats["gaps_over_40"] += gap_ms > 40
                            network_stats["gaps_over_100"] += gap_ms > 100
                            network_stats["bursts_under_5"] += gap_ms < 5
                            network_stats["max_gap_ms"] = max(network_stats["max_gap_ms"], gap_ms)
                        network_stats["last_rx"] = received_at
                        if not monitor_enabled:
                            continue
                        if freshness is not None:
                            # Experimental profiles accept framed packets only.
                            freshness_error = freshness.observe(send_ms, received_at * 1000)
                            if freshness_error:
                                delivery_stats["transport_stale_drops"] += 1
                                if history is not None:
                                    history.fill(0)
                                if freshness_error == "restart":
                                    await ws.close(code=1011, message=b"Reprise audio trop tardive")
                                    break
                                continue
                        if route and not room_audio.accept_pcm(
                                route, len(data) // 2, capture_rate, received_at, send_ms, frame_end):
                            delivery_stats["invalid_packets"] += 1
                            continue
                        wait_ms = supply_wait_ms(received_at)
                        starvations += wait_ms > 0
                        starvation_ms += wait_ms
                        idle_since = None
                        if queue.full():
                            # Drop the oldest pending block, preserving order
                            # among the blocks that will still reach the player.
                            queue.get_nowait()
                            delivery_stats["queue_drops"] += 1
                        # Separate dispatch of the complete WebSocket message
                        # from waiting for the writer to take the queued block.
                        enqueued_at = time.monotonic()
                        distributions["receive_ms"].append(
                            enqueued_at * 1000, (enqueued_at - received_at) * 1000)
                        queue.put_nowait((enqueued_at, data, seq if framed else None))
                        queue_high_water = max(queue_high_water, queue.qsize())
                    else:
                        delivery_stats["invalid_packets"] += 1
                elif msg.type == WSMsgType.TEXT:
                    if route and msg.data.startswith("clock:") and len(msg.data) <= 512:
                        received_ns = time.monotonic_ns()
                        try:
                            response = json.loads(msg.data[6:])
                        except (ValueError, RecursionError):
                            continue
                        if isinstance(response, dict) and clock_probe and response.get("id") == clock_probe[0]:
                            anchor = browser_anchor(clock_probe[1], received_ns, response)
                            clock_probe = None
                            if anchor:
                                room_audio.diagnostics.emit("clock", "mobile-anchor", **route.context,
                                                            connection=route.connection, rate=capture_rate, **anchor)
                        continue
                    if msg.data == "stop":
                        disconnect_reason = "user"
                        break
                    if route and msg.data in ("stop:background", "stop:pagehide", "stop:media-ended",
                                              "stop:audio-suspended", "stop:room-ended"):
                        disconnect_reason = msg.data[5:]
                        break
                    if route is None and msg.data == "monitor:off":
                        monitor_enabled = False
                        fed = False
                        idle_since = None
                        if history is not None:
                            history.fill(0)
                        if freshness is not None:
                            freshness = TransportFreshness(queue_packets * block_ms)
                        # Discard stale packets before the reference phase.
                        while not queue.empty():
                            queue.get_nowait()
                    elif route is None and msg.data == "monitor:on":
                        monitor_enabled = True
                    if msg.data.startswith("ping:") and len(msg.data) < 80:
                        await ws.send_str("pong:" + msg.data[5:])
                    if (local_diagnostics or route) and msg.data.startswith("diagnostics:") and len(msg.data) <= 4096:
                        now = time.monotonic()
                        if now - last_browser_diagnostics >= 2:
                            last_browser_diagnostics = now
                            mobile = browser_diagnostics(msg.data.removeprefix("diagnostics:"))
                            if mobile is not None:
                                if route:
                                    room_audio.diagnostics.emit("audio", "browser-capture", **route.context,
                                                                connection=route.connection, **mobile)
                                else:
                                    snapshot.update(mobile)
                    if msg.data.startswith("volume:") and len(msg.data) <= 12:
                        if route:
                            room_audio.volume(route, msg.data[7:])
                        else:
                            try:
                                requested = selected_volume(msg.data[7:])
                                if requested != mic_volume:
                                    mic_volume = requested
                                    if volume_task.done():
                                        volume_task = asyncio.create_task(apply_volume())
                            except ValueError:
                                pass
                    if msg.data == "levels":
                        now = time.monotonic()
                        ongoing_wait_ms = supply_wait_ms(now)
                        recent = {key: window.summary() for key, window in distributions.items()}
                        relative = transit.summary()
                        backlog = player_backlog(stdin_transport, pipe)
                        for key, window in backlog_windows.items():
                            if backlog[key] is not None:
                                window.append(now * 1000, backlog[key])
                        levels = {"levels": True,
                                            "rms": round(audio_stats["rms"], 4),
                                            "peak": round(audio_stats["peak"], 4),
                                            "clipped": audio_stats["clipped"],
                                            "frames": audio_stats["frames"],
                                            "rx_packets": delivery_stats["rx_packets"],
                                            "rx_gap_p95_ms": recent["arrival_gap_ms"]["p95"] or 0,
                                            "rx_gap_max_ms": round(network_stats["max_gap_ms"], 1),
                                            "rx_gaps_over_40": network_stats["gaps_over_40"],
                                            "rx_gaps_over_100": network_stats["gaps_over_100"],
                                            "rx_bursts_under_5": network_stats["bursts_under_5"],
                                            "transport_excess_p95_ms": relative["p95"] or 0,
                                            "transport_excess_max_ms": relative["max"] or 0,
                                            "sequence_missing": transport["missing"],
                                            "sequence_reordered": transport["reordered"],
                                            "duplicate_packets": delivery_stats.get("duplicate_packets", 0),
                                            "queue_drops": delivery_stats["queue_drops"],
                                            "transport_stale_drops": delivery_stats["transport_stale_drops"],
                                            "invalid_packets": delivery_stats["invalid_packets"],
                                            "source_starvations": starvations + (ongoing_wait_ms > 0),
                                            "source_starvation_ms": round(starvation_ms + ongoing_wait_ms, 1),
                                            **pulse_status,
                                            "queue_packets": queue.qsize(),
                                            "uptime_sec": round(now - timing["start"], 1),
                                            "drop_per_sec": round(delivery_stats["queue_drops"] / max(1, now - timing["start"]), 2),
                                            "write_avg_ms": round(timing["write_total_ms"] / max(1, timing["write_count"]), 2),
                                            "write_max_ms": round(timing["write_max_ms"], 2),
                                            "write_over_10ms": timing["write_over_10ms"],
                                            "dsp_avg_ms": round(timing["dsp_total_ms"] / max(1, timing["write_count"]), 3),
                                            "dsp_max_ms": round(timing["dsp_max_ms"], 3),
                                            "queue_wait_last_ms": round(timing["queue_wait_last_ms"], 1),
                                            "queue_wait_max_ms": round(timing["queue_wait_max_ms"], 1),
                                            "queue_est_ms": queue.qsize() * block_ms,
                                            "server_ready_ms": round(timing["ready_ms"], 1),
                                            "diagnostic": {"timings_ms": recent,
                                                           "relative_transit_ms": relative,
                                                           "player_backlog": backlog,
                                                           "player_backlog_samples_ms": {
                                                               key: window.summary() for key, window in backlog_windows.items()},
                                                           "submission_frames_per_s": cadence.summary(),
                                                           "queue_high_water_packets": queue_high_water}}
                        if local_diagnostics:
                            snapshot.update(server=levels, volume_percent=mic_volume)
                        if route:
                            levels["applied_percent"] = room_audio.applied(route)
                            levels["requested_percent"] = room_audio.requested(route)
                            levels["return_muted"] = room_audio.muted(route)
                            fresh = now - audio_stats.get("meter_at", 0.) < .25
                            for name in ("rms", "peak", "resampled_rms", "guarded_rms", "guarded_peak",
                                         "return_rms", "return_peak"):
                                levels[name] = round(audio_stats.get(name, 0.), 5) if fresh else 0.
                            levels["return_drive"] = route.return_drive
                            levels["return_fader_gain"] = route.gain
                            levels["return_peak_limit"] = peak
                            levels["stage"] = "microphone-test" if route.testing else "turn"
                            room_audio.metrics(route, levels)
                            if (room_audio.diagnostics.active()
                                    and (clock_probe is None or time.monotonic_ns() - clock_probe[1] > 2_000_000_000)
                                    and int(now / 2) != clock_sequence):
                                clock_sequence = int(now / 2)
                                clock_probe = (clock_sequence, time.monotonic_ns())
                                levels["clock_probe"] = clock_sequence
                        await ws.send_json(levels)
                else:
                    break
        except TimeoutError:
            disconnect_reason = "timeout"
            await ws.close(code=4001, message="Flux micro sans réponse".encode())
        finally:
            async def cleanup():
                try:
                    if playback_task:
                        playback_task.cancel()
                        await asyncio.gather(playback_task, return_exceptions=True)
                    if player:
                        await close_process(player)
                finally:
                    if status_task:
                        status_task.cancel()
                        await asyncio.gather(status_task, return_exceptions=True)
                    if volume_task:
                        await volume_task

            try:
                await await_owned(asyncio.create_task(cleanup()))
            finally:
                if route:
                    room_audio.diagnostics.emit("audio", "transport-end", **route.context,
                                                connection=route.connection, received=delivery_stats["rx_packets"],
                                                missing=transport["missing"], reordered=transport["reordered"],
                                                duplicates=delivery_stats.get("duplicate_packets", 0),
                                                invalid=delivery_stats["invalid_packets"], rate=capture_rate)
                    room_audio.detach(route, ws, reason=disconnect_reason, code=ws.close_code)
                if ACTIVE_SESSION["ws"] is ws:
                    ACTIVE_SESSION["ws"] = None
                    if local_diagnostics:
                        snapshot["active"] = False
                if ACTIVE_SESSION.get("monitor_offer") is offer:
                    ACTIVE_SESSION.pop("monitor_offer")
                ACTIVE.release()
        return ws

    if room_audio is None:
        app.router.add_get("/", page)
        app.router.add_get("/worklet.js", worklet)
        app.router.add_get("/calibration.js", calibration_script)
        app.router.add_get("/monitor-preferences", monitor_preferences)
    app.router.add_get("/audio", audio)
    if local_diagnostics:
        app.router.add_get("/monitor-diagnostics", diagnostics)
    return app


async def main():
    ip = lan_ip()
    token = load_token()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(ROOT / "server.crt", ROOT / "server.key")
    origin = f"https://{ip}:{PORT}"
    app = create_app(token, origin, local_diagnostics=os.environ.get("KARA_LOCAL_DIAGNOSTICS") == "1")
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, ip, PORT, ssl_context=context).start()
    print(f"MIC_TEST_ORIGIN={origin} (jeton dans le fichier privé Micro Test)", flush=True)
    print("MIC_OUTPUT=PipeWire native pw-cat, 48k stereo", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    if sys.argv[1:] == ["--rotate-token"]:
        load_token(rotate=True)
    elif sys.argv[1:]:
        raise SystemExit("Usage : _kara_micro_test.py [--rotate-token]")
    else:
        asyncio.run(main())
