"""Bounded timing telemetry for the monitor. All timestamps are in milliseconds."""
from array import array
from collections import deque
import fcntl
from math import ceil
from statistics import median
import termios

WINDOW_PACKETS = 1200
PCM_BYTES_PER_MS = 192  # 48 kHz stereo int16 entering the player.


class TimingWindow:
    def __init__(self):
        self.samples = deque(maxlen=WINDOW_PACKETS)

    def append(self, when, value):
        self.samples.append((when, value))

    def summary(self, relative=False):
        count = len(self.samples)
        ordered = sorted(value for _, value in self.samples)
        zero = ordered[0] if relative and ordered else 0
        result = {
            "n": count,
            "p50": round(ordered[ceil(.50 * count) - 1] - zero, 3) if count >= 5 else None,
            "p95": round(ordered[ceil(.95 * count) - 1] - zero, 3) if count >= 20 else None,
            "p99": round(ordered[ceil(.99 * count) - 1] - zero, 3) if count >= 100 else None,
            "max": round(ordered[-1] - zero, 3) if count else None,
            "trend_ms_per_s": None,
        }
        # Compare medians of the early and late quarters. Require wide enough
        # coverage for a trend so one burst cannot stand in for clock skew.
        if count >= 20 and self.samples[-1][0] - self.samples[0][0] >= 5000:
            samples = list(self.samples)
            size = count // 4
            first, last = samples[:size], samples[-size:]
            dt = median(t for t, _ in last) - median(t for t, _ in first)
            if dt > 0:
                result["trend_ms_per_s"] = round(
                    1000 * (median(v for _, v in last) - median(v for _, v in first)) / dt, 3)
        return result


class RelativeTransit:
    """Compare elapsed durations only. The clocks' offset and skew are unknown."""
    def __init__(self):
        self.window = TimingWindow()
        self.first = None
        self.last_send = None
        self.clock_resets = 0

    def observe(self, sent, received):
        if self.last_send is not None and sent < self.last_send:
            self.first = None
            self.window.samples.clear()
            self.clock_resets += 1
        if self.first is None:
            self.first = sent, received
        start_send, start_receive = self.first
        self.window.append(received, (received - start_receive) - (sent - start_send))
        self.last_send = sent

    def summary(self):
        return {**self.window.summary(relative=True), "clock_resets": self.clock_resets}


class TransportFreshness:
    """Quarantine a recovery burst after a large observed receive-only stall."""
    def __init__(self, budget_ms):
        self.budget_ms = budget_ms
        self.previous = None
        self.recovery = None

    def observe(self, sent, received):
        previous = self.previous
        self.previous = (sent, received)
        if previous is None or sent < previous[0]:
            self.recovery = None
            return None
        # Send and receive durations are compared only across the stall and
        # its bounded recovery window. An idle sender or steady clock drift
        # cannot establish a stale-PCM interval.
        if self.recovery is None:
            last_sent, last_received = previous
            if (received - last_received) - (sent - last_sent) < 250:
                return None
            self.recovery = (last_sent, last_received, received)
        start_sent, start_received, first_late_arrival = self.recovery
        if (received - start_received) - (sent - start_sent) <= self.budget_ms:
            self.recovery = None
            return None
        # Sustained uncertainty requires a fresh session. Do not replay old
        # speech or pretend the two hosts have synchronized absolute clocks.
        return "restart" if received - first_late_arrival >= 2000 else "stale"


class SubmissionCadence:
    """Count PCM accepted by stdin, independent of the device's sample clock."""
    def __init__(self):
        self.frames = 0
        self.samples = deque(maxlen=WINDOW_PACKETS)

    def observe(self, when, frames):
        self.frames += frames
        self.samples.append((when, self.frames))

    def summary(self):
        if len(self.samples) < 2:
            return None
        start, first = self.samples[0]
        end, last = self.samples[-1]
        return round(1000 * (last - first) / (end - start), 1) if end - start >= 5000 else None


def player_backlog(transport, pipe):
    """Snapshot local buffers only. FIONREAD excludes the playback process."""
    async_bytes = transport.get_write_buffer_size() if transport is not None else None
    pipe_bytes = None
    if pipe is not None:
        size = array("i", [0])
        try:
            fcntl.ioctl(pipe, termios.FIONREAD, size)
            pipe_bytes = size[0]
        except (OSError, ValueError):
            pass
    return {"asyncio_bytes": async_bytes, "kernel_pipe_bytes": pipe_bytes,
            "asyncio_pcm_ms": round(async_bytes / PCM_BYTES_PER_MS, 3)
            if async_bytes is not None else None,
            "kernel_pipe_pcm_ms": round(pipe_bytes / PCM_BYTES_PER_MS, 3)
            if pipe_bytes is not None else None}
