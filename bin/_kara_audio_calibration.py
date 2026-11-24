"""Bounded, server-only calibration probes. No microphone data reaches playback."""
import asyncio
import re
import time

import numpy as np
from aiohttp import WSMsgType, web

RATE = 48000
FRAMES = 4800
PROBE_INTERVAL = 1.0
SESSION_TIMEOUT = 60.0
IDLE_TIMEOUT = 15.0
STARTUP_TIMEOUT = 2.0
DRAIN_TIMEOUT = 0.3
EXIT_TIMEOUT = 1.5
ANALOG_TARGET = "alsa_output.pci-0000_63_00.6.analog-stereo"


def generate_probe(method):
    """Return deterministic 100 ms mono s16le PCM, peak <= 0.18 full scale."""
    if method not in ("chirp", "pn", "warmup"):
        raise ValueError("Unknown probe method")
    t = np.arange(FRAMES) / RATE
    if method in ("chirp", "warmup"):
        signal = np.sin(2 * np.pi * (600 * t + 0.5 * 54000 * t * t))
    else:
        rng = np.random.default_rng(20261123)
        spectrum = np.fft.rfft(rng.choice([-1.0, 1.0], FRAMES))
        frequencies = np.fft.rfftfreq(FRAMES, 1 / RATE)
        # Smooth spectral shoulders, strictly zero outside 600..6000 Hz.
        band = np.clip((frequencies - 600) / 300, 0, 1)
        band *= np.clip((6000 - frequencies) / 600, 0, 1)
        signal = np.fft.irfft(spectrum * band, FRAMES)
    # Raised-cosine envelope avoids abrupt onset and termination (10 ms ramps).
    envelope = np.ones(FRAMES)
    ramp = np.sin(np.linspace(0, np.pi / 2, 480)) ** 2
    envelope[:480] = ramp
    envelope[-480:] = ramp[::-1]
    signal *= envelope
    signal *= 0.18 / np.max(np.abs(signal))
    return np.rint(signal * 32767).astype("<i2").tobytes()


async def await_owned(task):
    """Defer even repeated owner cancellation until the owned task finishes."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    # Retrieve failures (including child cancellation) before leaving ownership.
    result = task.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def close_process(process):
    await await_owned(asyncio.create_task(_close_process(process)))


async def _close_process(process):
    if process.stdin:
        try:
            process.stdin.close()
        except (OSError, RuntimeError):
            pass
    if process.returncode is None:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
    try:
        await asyncio.wait_for(process.wait(), 2.0)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            # The child can exit between the timed wait and signal delivery.
            pass
        await asyncio.wait_for(process.wait(), 0.5)


async def selected_target(output):
    if output == "analog":
        return ANALOG_TARGET
    process = await asyncio.wait_for(asyncio.create_subprocess_exec(
        "pactl", "get-default-sink", stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL), STARTUP_TIMEOUT)
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), STARTUP_TIMEOUT)
        target = stdout.decode().strip()
        if process.returncode != 0 or not target or len(target) > 512:
            raise RuntimeError("default_target_unavailable")
        return target
    finally:
        await close_process(process)


async def calibration_audio(request, active, session, output):
    # Never take over a live or starting session, including another calibration.
    if active.locked():
        raise web.HTTPConflict(text="Session audio deja active")
    await active.acquire()
    session["calibration"] = True
    ws = web.WebSocketResponse(heartbeat=8, timeout=1.0, max_msg_size=16384)
    worker = None
    started = time.monotonic()
    counts = {"warmup": 0, "chirp": 0, "pn": 0}
    last_request = None
    last_ping = None

    async def fail(error):
        # Stop sound before the WebSocket close handshake can block. The worker
        # cleans its own process before calling fail(), so never cancel itself.
        if worker is not None and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await await_owned(worker)
            except (asyncio.CancelledError, OSError, RuntimeError, asyncio.TimeoutError):
                pass
        if not ws.closed:
            await ws.send_json({"error": error})
            await ws.close(code=1008, message=error.encode())

    async def emit(method, received_at):
        process = None
        failed = False
        try:
            pcm = generate_probe(method)
            spawn_at = time.monotonic()
            process = await asyncio.wait_for(asyncio.create_subprocess_exec(
                "pw-cat", "--playback", "--raw", "--format", "s16",
                "--rate", str(RATE), "--channels", "1", "--latency", "10ms",
                "--target", target,
                "--properties", "node.dont-reconnect=true node.dont-fallback=true node.dont-move=true",
                "-", stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL), STARTUP_TIMEOUT)
            spawned_at = time.monotonic()
            if process.returncode is not None:
                raise RuntimeError("player_exited_before_write")
            write_at = time.monotonic()
            process.stdin.write(pcm)
            await asyncio.wait_for(process.stdin.drain(), DRAIN_TIMEOUT)
            drained_at = time.monotonic()
            # Only this fixed PCM enters stdin. EOF uses pw-cat's drain path;
            # --sample-count would quit before that path in PipeWire 1.6.11.
            process.stdin.close()
            await asyncio.wait_for(process.wait(), EXIT_TIMEOUT)
            exited_at = time.monotonic()
            if process.returncode != 0:
                raise RuntimeError("player_failed")
            counts[method] += 1
            await ws.send_bytes(pcm)
            await ws.send_json({
                "probe": method, "rate": RATE, "channels": 1,
                "format": "s16le", "frames": FRAMES, "duration_ms": 100,
                "target": target, "configured_latency_ms": 10,
                "process_startup_ms": (spawned_at - spawn_at) * 1000,
                "request_to_write_ms": (write_at - received_at) * 1000,
                "drain_ms": (drained_at - write_at) * 1000,
                "request_to_drain_ms": (drained_at - received_at) * 1000,
                "request_to_exit_ms": (exited_at - received_at) * 1000,
                "completed_count": counts[method],
            })
        except (OSError, RuntimeError, asyncio.TimeoutError, ConnectionError):
            failed = True
        finally:
            if process is not None:
                try:
                    await close_process(process)
                except (OSError, RuntimeError, asyncio.TimeoutError):
                    failed = True
        if failed:
            await fail("probe_failed")

    try:
        await ws.prepare(request)
        session["ws"] = ws
        try:
            target = await selected_target(output)
        except (OSError, RuntimeError, UnicodeError, asyncio.TimeoutError):
            await fail("target_unavailable")
            return ws
        await ws.send_json({
            "ready": True, "device": target, "target": target,
            "output": output, "mode": "calibration", "rate": RATE,
            "channels": 1, "format": "s16le", "frames": FRAMES,
            "duration_ms": 100, "configured_latency_ms": 10,
            "server_ready_ms": (time.monotonic() - started) * 1000,
            "probe_interval_ms": PROBE_INTERVAL * 1000,
            "max_warmup": 1, "max_per_method": 6, "max_useful": 12,
            "session_timeout_ms": SESSION_TIMEOUT * 1000,
            "idle_timeout_ms": IDLE_TIMEOUT * 1000,
        })
        while not ws.closed:
            remaining = SESSION_TIMEOUT - (time.monotonic() - started)
            if remaining <= 0:
                await fail("session_timeout")
                break
            try:
                msg = await asyncio.wait_for(ws.receive(), min(IDLE_TIMEOUT, remaining))
            except asyncio.TimeoutError:
                await fail("session_timeout" if remaining <= IDLE_TIMEOUT else "idle_timeout")
                break
            if msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
            if msg.type == WSMsgType.TEXT and msg.data == "stop":
                break
            if msg.type == WSMsgType.TEXT and msg.data.startswith("ping:"):
                now = time.monotonic()
                if (not re.fullmatch(r"ping:[0-9]{1,16}(?:\.[0-9]{1,17})?", msg.data)
                        or (last_ping is not None and now - last_ping < 1.0)):
                    await fail("invalid_or_rate_limited_ping")
                    break
                last_ping = now
                await ws.send_str("pong:" + msg.data[5:])
                continue
            if msg.type != WSMsgType.TEXT or msg.data not in (
                    "probe:chirp", "probe:pn", "probe:warmup"):
                await fail("unsupported_command")
                break
            received_at = time.monotonic()
            method = msg.data[6:]
            error = None
            if worker is not None and not worker.done():
                error = "probe_in_progress"
            elif last_request is not None and received_at - last_request < PROBE_INTERVAL:
                error = "probe_rate_limited"
            elif counts[method] >= (1 if method == "warmup" else 6):
                error = "probe_limit"
            if error:
                await fail(error)
                break
            last_request = received_at
            worker = asyncio.create_task(emit(method, received_at))
    finally:
        try:
            if worker is not None:
                worker.cancel()
                try:
                    await await_owned(worker)
                except asyncio.CancelledError:
                    pass
        finally:
            try:
                await ws.close()
            finally:
                if session["ws"] is ws:
                    session["ws"] = None
                session["calibration"] = False
                active.release()
    return ws
