#!/usr/bin/env python3
"""Local HTTPS microphone monitor: phone -> Wi-Fi -> current PipeWire sink."""
import asyncio
import ipaddress
import json
from pathlib import Path
import secrets
import ssl
import subprocess
import struct
import time

import numpy as np
from scipy.signal import resample_poly
from aiohttp import WSMsgType, web

ROOT = Path.home() / ".local/state/kara/micro-test"
PORT = 8766
ACTIVE = asyncio.Lock()
ACTIVE_SESSION = {"ws": None}
PAGE = Path(__file__).with_name("_kara_micro_test.html")


def lan_ip():
    data = json.loads(subprocess.check_output(
        ["ip", "-j", "-4", "route", "get", "1.1.1.1"], timeout=3))
    candidate = data[0].get("prefsrc") or data[0].get("src")
    address = ipaddress.ip_address(candidate)
    if not address.is_private or address.is_loopback:
        raise RuntimeError("Pas d'adresse LAN privée")
    return str(address)


def load_token():
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = ROOT / "token"
    if not path.exists():
        with path.open("x") as stream:
            stream.write(secrets.token_urlsafe(24))
        path.chmod(0o600)
    return path.read_text().strip()


async def main():
    ip = lan_ip()
    token = load_token()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(ROOT / "server.crt", ROOT / "server.key")
    origin = f"https://{ip}:{PORT}"
    app = web.Application(client_max_size=32768)

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
        return secrets.compare_digest(request.query.get("token", ""), token)

    async def page(request):
        if not authorized(request):
            raise web.HTTPForbidden(text="Lien de test microphone invalide")
        return web.Response(body=PAGE.read_bytes(), content_type="text/html")

    async def worklet(request):
        if not authorized(request):
            raise web.HTTPForbidden(text="Module audio non autorise")
        return web.Response(body=PAGE.with_name("_kara_micro_worklet.js").read_bytes(),
                            content_type="text/javascript")

    async def audio(request):
        if not authorized(request) or request.headers.get("Origin") != origin:
            raise web.HTTPForbidden(text="Session microphone non autorisée")
        output_choice = request.query.get("output", "default")
        if output_choice not in ("default", "analog"):
            raise web.HTTPBadRequest(text="Sortie inconnue")
        try:
            capture_rate = int(request.query.get("rate", "48000"))
        except ValueError:
            raise web.HTTPBadRequest(text="Frequence invalide")
        if capture_rate not in (44100, 48000):
            raise web.HTTPBadRequest(text="Frequence non prise en charge")
        # The next authenticated connection takes over an abandoned tab.
        if ACTIVE.locked():
            previous = ACTIVE_SESSION["ws"]
            if previous is not None:
                await previous.close(code=4000, message=b"Nouvelle session micro")
        try:
            await asyncio.wait_for(ACTIVE.acquire(), timeout=5)
        except asyncio.TimeoutError:
            raise web.HTTPServiceUnavailable(text="Micro precedent en cours de fermeture")

        ws = web.WebSocketResponse(heartbeat=8, max_msg_size=16384)
        sink_target = ("alsa_output.pci-0000_63_00.6.analog-stereo"
                       if output_choice == "analog" else None)
        cmd = ["pw-cat", "--playback", "--raw", "--format", "s16",
               "--rate", "48000", "--channels", "2", "--latency", "10ms",
               "--volume", "0.85" if sink_target else "1.0"]
        if sink_target:
            cmd += ["--target", sink_target]
        cmd.append("-")
        player = None
        playback_task = None
        calibration_session = request.query.get("calibration") == "1"
        monitor_enabled = not calibration_session
        monitor_deadline = 0.0
        queue = asyncio.Queue(maxsize=4)  # max ~80ms of 20ms packets
        audio_stats = {"rms": 0.0, "peak": 0.0, "clipped": 0, "frames": 0}
        mic_volume = 160
        sink_input_id = None
        delivery_stats = {"rx_packets": 0, "queue_drops": 0, "last_queue": 0}
        network_stats = {"last_rx": None, "intervals": [], "gaps_over_40": 0,
                         "gaps_over_100": 0, "max_gap_ms": 0.0, "bursts_under_5": 0}
        transport = {"initial_client": None, "initial_server": None,
                     "min_offset_ms": None, "excess_ms": [], "max_excess_ms": 0.0,
                     "last_seq": None, "missing": 0, "reordered": 0}
        timing = {"start": time.monotonic(), "ready_ms": 0.0,
                  "write_count": 0, "write_total_ms": 0.0,
                  "write_max_ms": 0.0, "queue_wait_max_ms": 0.0,
                  "queue_wait_last_ms": 0.0, "write_over_10ms": 0}

        def find_mic_sink_input():
            # PipeWire may omit application.process.id for pw-cat.
            # Target only a unique matching pw-cat playback session.
            details = subprocess.check_output(
                ("pactl", "list", "sink-inputs"), text=True, timeout=2)
            matches = []
            for entry in details.split("Sink Input #")[1:]:
                if 'application.name = "pw-cat"' not in entry:
                    continue
                index = entry.splitlines()[0].strip()
                if index.isdigit():
                    matches.append(index)
            return matches[0] if len(matches) == 1 else None

        async def set_mic_volume(percent):
            nonlocal mic_volume, sink_input_id
            mic_volume = max(50, min(180, int(percent)))
            try:
                if sink_input_id is None:
                    sink_input_id = await asyncio.to_thread(find_mic_sink_input)
                if sink_input_id:
                    await asyncio.to_thread(subprocess.run,
                        ("pactl", "set-sink-input-volume", sink_input_id,
                         f"{mic_volume}%"), check=True, timeout=2)
            except (OSError, ValueError, subprocess.SubprocessError):
                sink_input_id = None

        async def playback():
            while True:
                packet = await queue.get()
                if packet is None:
                    break
                enqueued_at, data = packet
                queued_ms = (time.monotonic() - enqueued_at) * 1000
                timing["queue_wait_last_ms"] = queued_ms
                timing["queue_wait_max_ms"] = max(timing["queue_wait_max_ms"], queued_ms)
                if data is None:
                    break
                if player.returncode is not None:
                    break
                samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
                audio_stats["rms"] = float(np.sqrt(np.mean(samples * samples)))
                audio_stats["peak"] = float(np.max(np.abs(samples)))
                audio_stats["clipped"] += int(np.count_nonzero(np.abs(samples) >= 0.995))
                audio_stats["frames"] += len(samples)
                # AudioWorklet sends native 48k PCM (or 44.1k on some phones).
                if capture_rate == 44100:
                    samples = resample_poly(samples, 160, 147)
                # Keep headroom to avoid clipping when the mic stream is raised.
                # Gentle vocal-only compression: more audible quiet speech,
                # peak limited to 0.82 to prevent strong-voice distortion.
                samples = np.tanh(samples * 2.0) * 0.82
                stereo = np.repeat(samples[:, None], 2, axis=1)
                pcm = (stereo * 32767).astype("<i2")
                try:
                    write_started = time.monotonic()
                    player.stdin.write(pcm.tobytes())
                    await asyncio.wait_for(player.stdin.drain(), timeout=0.3)
                    write_ms = (time.monotonic() - write_started) * 1000
                    timing["write_count"] += 1
                    timing["write_total_ms"] += write_ms
                    timing["write_max_ms"] = max(timing["write_max_ms"], write_ms)
                    if write_ms > 10:
                        timing["write_over_10ms"] += 1
                except (BrokenPipeError, ConnectionResetError, asyncio.TimeoutError):
                    break

        try:
            player = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL)
            await asyncio.sleep(0.08)
            if player.returncode is not None:
                raise web.HTTPServiceUnavailable(text="Lecteur PipeWire non disponible")
            playback_task = asyncio.create_task(playback())
            await ws.prepare(request)
            ACTIVE_SESSION["ws"] = ws
            # Do not block microphone activation on pactl/volume discovery.
            # Read audio while PipeWire independently sets the mic-only gain.
            volume_task = asyncio.create_task(set_mic_volume(mic_volume))
            timing["ready_ms"] = (time.monotonic() - timing["start"]) * 1000
            await ws.send_json({"ready": True,
                                "server_ready_ms": round(timing["ready_ms"], 1),
                                "device": "Sortie analogique du PC" if sink_target
                                          else "Sortie PipeWire par defaut",
                                "mode": "pipewire-native-48k-stereo"})
            async for msg in ws:
                if msg.type == WSMsgType.BINARY:
                    data = msg.data
                    if 0 < len(data) <= 8192 and len(data) % 2 == 0:
                        delivery_stats["rx_packets"] += 1
                        received_at = time.monotonic()
                        if not calibration_session and len(data) >= 18 and data[:4] == b"KTS1":
                            seq, send_ms = struct.unpack_from("<Id", data, 4)
                            data = data[16:]  # PCM itself is unchanged.
                            if transport["initial_client"] is None:
                                transport["initial_client"] = send_ms
                                transport["initial_server"] = received_at * 1000
                            offset_ms = ((received_at * 1000 - transport["initial_server"])
                                         - (send_ms - transport["initial_client"]))
                            # Minimum seen is the reference. This is relative
                            # jitter, NOT absolute network one-way latency.
                            minimum = transport["min_offset_ms"]
                            if minimum is None or offset_ms < minimum:
                                transport["min_offset_ms"] = offset_ms
                            excess = max(0.0, offset_ms - transport["min_offset_ms"])
                            transport["excess_ms"].append(excess)
                            if len(transport["excess_ms"]) > 1200:
                                del transport["excess_ms"][:200]
                            transport["max_excess_ms"] = max(transport["max_excess_ms"], excess)
                            previous = transport["last_seq"]
                            if previous is not None:
                                if seq > previous + 1:
                                    transport["missing"] += seq - previous - 1
                                elif seq <= previous:
                                    transport["reordered"] += 1
                            transport["last_seq"] = seq
                        if network_stats["last_rx"] is not None:
                            gap_ms = (received_at - network_stats["last_rx"]) * 1000
                            gaps = network_stats["intervals"]
                            gaps.append(gap_ms)
                            if len(gaps) > 1200:
                                del gaps[:200]
                            network_stats["gaps_over_40"] += gap_ms > 40
                            network_stats["gaps_over_100"] += gap_ms > 100
                            network_stats["bursts_under_5"] += gap_ms < 5
                            network_stats["max_gap_ms"] = max(network_stats["max_gap_ms"], gap_ms)
                        network_stats["last_rx"] = received_at
                        if (not monitor_enabled or
                            (calibration_session and time.monotonic() >= monitor_deadline)):
                            continue  # reference or expired one-shot return gate
                        if queue.full():
                            try:
                                queue.get_nowait()
                                delivery_stats["queue_drops"] += 1
                            except asyncio.QueueEmpty:
                                pass
                        queue.put_nowait((time.monotonic(), data))
                        delivery_stats["last_queue"] = queue.qsize()
                elif msg.type == WSMsgType.TEXT:
                    if msg.data == "stop":
                        break
                    if msg.data == "monitor:off":
                        monitor_enabled = False
                        # Discard stale packets before the reference phase.
                        while not queue.empty():
                            queue.get_nowait()
                    elif msg.data == "monitor:on":
                        monitor_enabled = True
                        if calibration_session:
                            # Fail closed even if the browser freezes; one short
                            # input burst only, never continuous acoustic feedback.
                            monitor_deadline = time.monotonic() + 0.52
                    if msg.data.startswith("ping:") and len(msg.data) < 80:
                        await ws.send_str("pong:" + msg.data[5:])
                    if msg.data.startswith("volume:") and len(msg.data) <= 12:
                        try:
                            requested = int(msg.data[7:])
                            if requested != mic_volume:
                                volume_task = asyncio.create_task(set_mic_volume(requested))
                        except ValueError:
                            pass
                    if msg.data == "levels":
                        await ws.send_json({"levels": True,
                                            "rms": round(audio_stats["rms"], 4),
                                            "peak": round(audio_stats["peak"], 4),
                                            "clipped": audio_stats["clipped"],
                                            "frames": audio_stats["frames"],
                                            "rx_packets": delivery_stats["rx_packets"],
                                            "rx_gap_p95_ms": round(sorted(network_stats["intervals"])[int(.95 * (len(network_stats["intervals"])-1))], 1) if network_stats["intervals"] else 0,
                                            "rx_gap_max_ms": round(network_stats["max_gap_ms"], 1),
                                            "rx_gaps_over_40": network_stats["gaps_over_40"],
                                            "rx_gaps_over_100": network_stats["gaps_over_100"],
                                            "rx_bursts_under_5": network_stats["bursts_under_5"],
                                            "transport_excess_p95_ms": round(sorted(transport["excess_ms"])[int(.95 * (len(transport["excess_ms"])-1))], 1) if transport["excess_ms"] else 0,
                                            "transport_excess_max_ms": round(transport["max_excess_ms"], 1),
                                            "sequence_missing": transport["missing"],
                                            "sequence_reordered": transport["reordered"],
                                            "queue_drops": delivery_stats["queue_drops"],
                                            "queue_packets": queue.qsize(),
                                            "uptime_sec": round(time.monotonic() - timing["start"], 1),
                                            "drop_per_sec": round(delivery_stats["queue_drops"] / max(1, time.monotonic() - timing["start"]), 2),
                                            "write_avg_ms": round(timing["write_total_ms"] / max(1, timing["write_count"]), 2),
                                            "write_max_ms": round(timing["write_max_ms"], 2),
                                            "write_over_10ms": timing["write_over_10ms"],
                                            "queue_wait_last_ms": round(timing["queue_wait_last_ms"], 1),
                                            "queue_wait_max_ms": round(timing["queue_wait_max_ms"], 1),
                                            "queue_est_ms": round(queue.qsize() * 20, 1),
                                            "server_ready_ms": round(timing["ready_ms"], 1)})
                else:
                    break
        finally:
            if playback_task:
                playback_task.cancel()
                try:
                    await playback_task
                except asyncio.CancelledError:
                    pass
            if player:
                if player.stdin:
                    player.stdin.close()
                if player.returncode is None:
                    player.terminate()
                try:
                    await asyncio.wait_for(player.wait(), timeout=2)
                except asyncio.TimeoutError:
                    player.kill()
                    await player.wait()
            if ACTIVE_SESSION["ws"] is ws:
                ACTIVE_SESSION["ws"] = None
            ACTIVE.release()
        return ws

    app.router.add_get("/", page)
    app.router.add_get("/worklet.js", worklet)
    app.router.add_get("/audio", audio)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, ip, PORT, ssl_context=context).start()
    print(f"MIC_TEST_URL={origin}/?token={token}", flush=True)
    print("MIC_OUTPUT=PipeWire native pw-cat, 48k stereo", flush=True)
    await asyncio.Event().wait()


if __name__ == "__main__":
    asyncio.run(main())
