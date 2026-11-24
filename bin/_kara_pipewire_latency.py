#!/usr/bin/env python3
"""Mesure multisource du retour Kara sur la grille d'un seul graphe PipeWire."""

import argparse
from contextlib import contextmanager
from hashlib import sha256
import json
from math import isfinite
import os
from pathlib import Path
import selectors
import shlex
import signal
from statistics import median
import struct
import subprocess
import sys
import tempfile
from time import monotonic

import numpy as np

from _kara_voice_latency_witness import DEFAULT_SOURCE, dbfs, notify
from _kara_voice_witness_analysis import FIR, bands, cosine, similarity


SINK = "alsa_output.pci-0000_63_00.6.analog-stereo"
RATE = 48000
FRAMES_PER_MS = RATE // 1000
RATE_MS = 16
HEADER = struct.Struct("<4sIIIIIQQQ")
MAX_QUANTUM = 8192
ROUTE_NONE, ROUTE_KARA, ROUTE_MIXED, ROUTE_UNKNOWN = range(4)
NATIVE = Path(__file__).resolve().parents[1] / "native/src/_kara_pipewire_witness.c"
LABELS = ("micro", "monitor_FL", "monitor_FR")
METRICS = ("graph_interval", "monitor_to_witness_graph", "acoustic_interval")
SCHEMA = 1


class CaptureError(Exception):
    """A missing graph invariant invalidates the campaign, including previous trials."""


class GraphCapture:
    def __init__(self, fd, *, taps=7, cancelled=None):
        self.fd = fd
        os.set_blocking(fd, False)
        self.selector = selectors.DefaultSelector()
        self.selector.register(fd, selectors.EVENT_READ)
        self.previous = None
        self.pending = np.empty((3, 0), dtype=np.float32)
        self.frame = None
        self.route = None
        self.present = None
        self.driver = None
        self.min_quantum = MAX_QUANTUM
        self.max_quantum = 0
        self.header_mask = taps
        self.taps = taps
        self.cycle = None
        self.cancelled = cancelled

    def close(self):
        self.selector.close()

    def read_bytes(self, size, deadline):
        result = bytearray()
        while len(result) < size:
            if self.cancelled is not None and self.cancelled():
                raise CaptureError("capture_cancelled")
            try:
                block = os.read(self.fd, size - len(result))
            except BlockingIOError:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise CaptureError("graph_timeout")
                if self.cancelled is None and not self.selector.select(remaining):
                    raise CaptureError("graph_timeout")
                if self.cancelled is not None:
                    self.selector.select(min(remaining, .10))
                continue
            if not block:
                raise CaptureError("graph_eof")
            result.extend(block)
        return result

    def read_cycle(self, limit=None):
        # The receipt clock is a watchdog only. Spatial/temporal comparison
        # is based entirely on paired cycle positions in the native protocol.
        deadline = monotonic() + (8 if self.previous is None else 2)
        if limit is not None:
            deadline = min(deadline, limit)
        magic, frames, rate, driver, route, flags, pos, ns, xrun = HEADER.unpack(
            self.read_bytes(HEADER.size, deadline))
        meta, present = flags & 7, (flags >> 8) & 7
        if (magic != b"KPW1" or not 1 <= frames <= MAX_QUANTUM or rate != RATE
                or driver == 0xffffffff or route not in range(4) or flags & ~0x707
                or present & ~self.taps or meta & ~present
                or (self.taps & 1 and not present & 1)
                or (route != ROUTE_NONE and present != self.taps)
                or (self.taps == 1 and route != ROUTE_NONE)
                or ns == 0 or pos + frames >= 1 << 64):
            raise CaptureError("unusable_graph_packet")
        if self.previous is not None:
            end, old_ns, old_xrun = self.previous
            if driver != self.driver or ns <= old_ns:
                raise CaptureError("graph_clock_changed")
            if xrun != old_xrun:
                raise CaptureError("graph_xrun")
            if pos != end:
                raise CaptureError("graph_sample_gap")
        else:
            self.driver, self.frame = driver, pos
        payload = np.frombuffer(self.read_bytes(3 * frames * 4, deadline), dtype="<f4").reshape(3, frames)
        if not np.isfinite(payload).all():
            raise CaptureError("nonfinite_graph_pcm")
        self.previous = (pos + frames, ns, xrun)
        self.min_quantum = min(self.min_quantum, frames)
        self.max_quantum = max(self.max_quantum, frames)
        self.header_mask &= meta
        self.pending, self.route, self.present = payload, route, present
        self.cycle = {"driver": driver, "frame": pos, "monotonic_ns": ns, "xrun": xrun,
                      "quantum": frames, "present": present, "spa_headers": meta, "route": route}

    def read_frames(self, frames):
        if self.frame is None:
            self.read_cycle()
        start = self.frame
        result = np.empty((3, frames), dtype=np.float32)
        routes = set()
        present = 7
        offset = 0
        while offset < frames:
            if not self.pending.shape[1]:
                self.read_cycle()
            count = min(frames - offset, self.pending.shape[1])
            result[:, offset:offset + count] = self.pending[:, :count]
            self.pending = self.pending[:, count:]
            routes.add(self.route)
            present &= self.present
            self.frame += count
            offset += count
        return start, result, routes, present

    def clock_report(self):
        return {"driver_id": self.driver, "rate_hz": RATE,
                "observed_quantum_frames": [self.min_quantum, self.max_quantum],
                "all_cycles_spa_header_mask": self.header_mask,
                "common_tap_grid": "synchronous_dsp_cycle",
                "frame_grid_offset_between_taps": 0,
                "sink_input_to_monitor_bound_ms": None,
                "hardware_alignment_bound_ms": None,
                "hardware_timestamp_calibrated": False}


def build_helper(target):
    flags = subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "libpipewire-0.3"], text=True, timeout=5)
    subprocess.run(["cc", "-O2", "-std=gnu17", "-Wall", "-Wextra",
                    str(NATIVE), "-o", str(target), *shlex.split(flags)], check=True, timeout=20)


@contextmanager
def open_graph(helper, mic, sink, channel, *, preflight_player=None, track=None, cancelled=None):
    with tempfile.TemporaryDirectory(prefix="kara-pw-witness-") as directory:
        if helper is None:
            helper = Path(directory) / "witness"
            build_helper(helper)
        command = [str(helper), mic, sink, channel]
        taps = 7
        if track == "monitor":
            command.append("--passive-monitor")
            taps = 6
        elif track == "witness":
            command.append("--witness-only")
            taps = 1
        if preflight_player is not None:
            command.extend(("--preflight-player", preflight_player))
        with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, bufsize=0, start_new_session=True) as process:
            capture = None
            failure = None
            try:
                capture = GraphCapture(process.stdout.fileno(), taps=taps, cancelled=cancelled)
                yield capture
            except CaptureError as exc:
                failure = exc
            finally:
                if capture is not None:
                    capture.close()
                stop_process(process)
            # The native helper emits only short diagnostic lines. Keep
            # the actual failure in the JSON when stdout closes on error.
            os.set_blocking(process.stderr.fileno(), False)
            diagnostics = (process.stderr.read(8192) or b"").decode(errors="replace").strip()
            if diagnostics and track is None:
                notify(diagnostics)
            if process.returncode and (failure is None or str(failure) == "graph_eof"):
                prefix = "kara_pipewire_witness: "
                reason = next((line[len(prefix):] for line in diagnostics.splitlines()
                               if line.startswith(prefix)), "native_capture_failed")
                raise CaptureError(reason) from failure
            if failure is not None:
                raise failure


def stop_process(process):
    if process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=2)
    except (subprocess.TimeoutExpired, KeyboardInterrupt):
        process.kill()
        process.wait()
        raise
    return True


@contextmanager
def silent_playback(sink, name):
    # A separate, temporary playback node activates the sink's monitor PCM.
    # /dev/zero is the only input. WirePlumber must use the named sink or
    # leave the stream unlinked. This node is scoped to the preflight.
    command = ["pw-cat", "--playback", "--raw", "--format=s16", "--rate=48000", "--channels=2",
               f"--target={sink}",
               f"--properties={{ node.name = {name} node.dont-fallback = true node.dont-reconnect = true }}", "-"]
    with open("/dev/zero", "rb", buffering=0) as zeros, subprocess.Popen(
            command, stdin=zeros, stdout=subprocess.DEVNULL, start_new_session=True) as process:
        try:
            yield
        finally:
            stopped = stop_process(process)
        # pw-cat reports status 1 when SIGTERM closes the stream before EOF
        # and drain, as intended for an open-ended source of zeros.
        if not stopped or process.returncode not in (0, 1):
            raise CaptureError("preflight_playback_failed")


def run_preflight(args):
    timing = None
    result = {"status": "inconclusive", "reason": None}
    notify("Prévol PipeWire · Lecture silencieuse et vérification des trois canaux.")
    player = f"kara-pipewire-preflight.{os.getpid()}"
    try:
        with silent_playback(args.sink, player), open_graph(
                args.helper, args.mic, args.sink, args.mic_channel, preflight_player=player) as capture:
            deadline = monotonic() + 13
            frames = cycles = 0
            routes = set()
            while frames < 4 * RATE:
                if monotonic() >= deadline:
                    raise CaptureError("preflight_timeout")
                capture.read_cycle(deadline)
                if capture.present != 7 or capture.route != ROUTE_MIXED:
                    raise CaptureError("preflight_monitor_pcm_missing")
                if not cycles:
                    first_ns, xrun = capture.previous[1:]
                cycles += 1
                frames = capture.previous[0] - capture.frame
                routes.add(capture.route)
            timing = capture.clock_report()
            timing.update({"first_graph_frame": capture.frame, "end_graph_frame": capture.previous[0],
                           "first_cycle_nsec": first_ns, "last_cycle_nsec": capture.previous[1],
                           "driver_xrun_counter": xrun, "cycles_checked": cycles,
                           "frames_checked_per_channel": frames, "pcm_mask": 7,
                           "routes": sorted(routes)})
        result["status"] = "pipewire_preflight_ready"
    except (CaptureError, OSError, subprocess.SubprocessError) as exc:
        result["reason"] = str(exc)
        timing = None
        notify(f"Prévol PipeWire indéterminé · {exc}")
    return {"version": SCHEMA, "mode": "preflight", "mic": args.mic, "sink": args.sink,
            "mic_channel": args.mic_channel, "timing": timing, "summary": result}


def to_analysis(signal):
    return np.convolve(signal, FIR, mode="same")[::3]


def rms_bins(signal):
    width = 4 * FRAMES_PER_MS
    blocks = signal[:len(signal) // width * width].astype(np.float64).reshape(-1, width)
    return np.sqrt(np.mean(blocks * blocks, axis=1, dtype=np.float64))


def source_levels(signal):
    levels = rms_bins(signal)
    return {"peak_dbfs": round(dbfs(np.max(np.abs(signal))), 2),
            "rms_dbfs": round(dbfs(np.sqrt(np.mean(levels ** 2))), 2),
            "max_4ms_rms_dbfs": round(dbfs(levels.max()), 2)}


def events(signal, floor, minimum=36):
    levels = rms_bins(signal)
    active = np.flatnonzero(levels >= floor)
    if not len(active):
        return []
    return [(int(run[0]) * 4, (int(run[-1]) + 1) * 4)
            for run in np.split(active, np.flatnonzero(np.diff(active) > 10) + 1)
            if len(run) >= 3 and (run[-1] - run[0] + 1) * 4 >= minimum]


def waveform(signal):
    return signal - .84 * np.r_[0, signal[:-1]]


def match(template, target, low, high, floor, guard=0):
    """Match the digital/physical landmarks in several disjoint parts of a syllable.

    Guard is a graph-frame index on the 16 kHz grid. An acoustic candidate
    needs template landmarks entirely after the plausible direct voice.
    """
    hop = 24 * RATE_MS
    indices = np.arange(0, len(template) - hop + 1, hop)
    low = max(0, low)
    high = min(high, len(target) - len(template))
    if len(indices) < 2 or high < low:
        return []
    # Limit each FFT to the search interval and its template. Retain the
    # first preceding sample so the pre-emphasis is continuous at the crop.
    origin = max(0, low - 1)
    target = target[origin:high + len(template)]
    wave = waveform(target)
    ref = waveform(template)
    choices = np.arange(low - origin, high - origin + 1)
    guard -= origin
    windows = np.lib.stride_tricks.sliding_window_view(ref, hop)[indices]
    power = np.sum(windows ** 2, axis=1)
    choices_per_anchor = indices[:, None] + choices
    totals = np.r_[0, np.cumsum(wave ** 2)]
    observed_power = totals[choices_per_anchor + hop] - totals[choices_per_anchor]
    valid = (choices_per_anchor >= guard) & (power[:, None] >= power.max() * .015)
    size = 1 << (len(wave) + hop - 1).bit_length()
    matched = np.fft.irfft(np.fft.rfft(wave, n=size)
                           * np.fft.rfft(windows[:, ::-1], n=size), n=size)
    dot = np.take_along_axis(matched, choices_per_anchor + hop - 1, axis=1)
    norm = np.sqrt(np.maximum(power[:, None] * observed_power, 1e-16))
    correlations = np.where(valid, np.clip(dot / norm, -1, 1), 0)
    # A periodic vowel can give two strong pitch-period aliases while the
    # attack and tail disagree. Rank each lag by agreement across all its
    # eligible landmarks, so the trial's phase within the RMS grid does not
    # turn two locally strong anchors into an alternative arrival.
    agreeing = np.maximum((np.maximum(correlations, 0) ** 2).sum(axis=0),
                           (np.minimum(correlations, 0) ** 2).sum(axis=0))
    eligible = valid.sum(axis=0)
    scores = np.where(eligible >= 2, np.sqrt(agreeing / np.maximum(eligible, 2)), 0)
    pending = scores.copy()
    candidates = []
    for _ in range(7):
        index = int(np.argmax(pending))
        if pending[index] < .34:
            break
        lag = int(choices[index])
        start = max(0, guard - lag)
        observed = target[lag + start:lag + len(template)]
        if (len(observed) >= 48 * RATE_MS
                and np.count_nonzero(rms_analysis(observed) >= floor) >= 6):
            sign = 1 if np.sum(correlations[:, index] ** 3) >= 0 else -1
            supported = np.flatnonzero(sign * correlations[:, index] >= .32)
            if len(supported) >= 2:
                corr = sign * cosine(ref[start:], wave[lag + start:lag + len(ref)])
                envelope, spectrum = similarity(bands(template[start:]), bands(observed))
                left, right = max(0, index - 4 * RATE_MS), min(len(scores), index + 4 * RATE_MS + 1)
                peaks = left + np.argmax(sign * correlations[supported, left:right], axis=1)
                spread = int(np.ptp(np.r_[peaks, index]))
                high_confidence = (corr >= .45 and envelope >= .57 and spectrum >= .42
                                   and spread <= 4 * RATE_MS)
                # A candidate from an alternative close peak or another path
                # stays diagnostic even if the envelope agrees.
                neighboring = (np.abs(choices - lag) >= 2 * RATE_MS) & (np.abs(choices - lag) <= 10 * RATE_MS)
                competing = bool(np.any(scores[neighboring] >= max(.53, scores[index] * .9)))
                confidence = "high" if high_confidence and not competing else "low"
                if confidence == "low" and corr >= .28 and max(envelope, spectrum) >= .42:
                    confidence = "moderate"
                bounds = [int(choices[min(index, int(peaks.min()))]) - 4 * RATE_MS,
                          int(choices[max(index, int(peaks.max()))]) + 4 * RATE_MS]
                candidates.append({"frame_16k": lag + origin,
                                   "bounds_16k": [bound + origin for bound in bounds], "confidence": confidence,
                                   "waveform": round(corr, 3), "envelope": round(envelope, 3),
                                   "spectral": round(spectrum, 3), "nearby_competitor": competing,
                                   "landmarks": int(len(supported))})
        radius = 10 * RATE_MS
        pending[max(0, index - radius):index + radius + 1] = 0
    candidates.sort(key=lambda row: (row["confidence"] == "high", row["waveform"]), reverse=True)
    return candidates


def rms_analysis(signal):
    width = 4 * RATE_MS
    blocks = signal[:len(signal) // width * width].reshape(-1, width)
    return np.sqrt(np.mean(blocks ** 2, axis=1))


def unique(candidates):
    plausible = [row for row in candidates if row["confidence"] != "low"]
    return plausible[0] if len(plausible) == 1 and plausible[0]["confidence"] == "high" else None


def interval(match_row, reference):
    return {"ms": (match_row["frame_16k"] - reference) / RATE_MS,
            "range_ms": [(bound - reference) / RATE_MS for bound in match_row["bounds_16k"]]}


def inspect_trial(frame, sources, routes, noise, stage, acoustic_guard, present=7):
    measured = {label: source_levels(sources[i]) if (present >> i) & 1 else None
                for i, label in enumerate(LABELS)}
    row = {"start_graph_frame": frame, "levels": measured,
           "mic_event": None, "mic_events": [], "monitor_events": {}, "routes": sorted(routes),
           "status": "inconclusive",
           "digital_candidates": [], "acoustic_candidates": [],
           **dict.fromkeys(METRICS), "physical_output_delay": None}
    mic_floor = max(noise[0] * 6, .0015)
    direct_groups = events(sources[0], mic_floor)
    row["mic_events"] = direct_groups
    monitor_groups = [events(signal, max(background * 4, .00018), minimum=20)
                      for signal, background in zip(sources[1:], noise[1:], strict=True)]
    row["monitor_events"] = dict(zip(LABELS[1:], monitor_groups, strict=True))
    active_monitor = any(monitor_groups)
    if active_monitor:
        row["kara_output_evidence"] = ("exclusive_route_with_signal" if routes == {ROUTE_KARA}
                                       else "unattributed_monitor_signal")
    else:
        row["kara_output_evidence"] = "inactive_monitor_no_playback" if present != 7 else "no_monitor_event"
    if not direct_groups:
        return {**row, "reason": "direct_not_detected"}
    start, observed_end = direct_groups[0]
    mic_peak = np.max(rms_bins(sources[0][start * FRAMES_PER_MS:(start + 80) * FRAMES_PER_MS]))
    core_events = events(sources[0], max(mic_floor, mic_peak * .18))
    core = next((end for begin, end in core_events if start <= begin <= start + 44), None)
    if not 240 <= start <= 2200 or core is None or not 68 <= core - start <= 360:
        return {**row, "reason": "direct_outside_window_or_duration"}
    if np.max(np.abs(sources[0])) >= .995:
        return {**row, "reason": "micro_clipping"}
    row["mic_event"] = {"window_ms": [start, core], "start_graph_frame": frame + start * FRAMES_PER_MS}
    if stage == "control":
        if active_monitor or routes & {ROUTE_MIXED, ROUTE_UNKNOWN}:
            return {**row, "reason": "monitor_not_quiet_or_route_mixed"}
        if observed_end - start > 400 or len(direct_groups) != 1:
            return {**row, "reason": "control_extra_acoustic_event"}
        return {**row, "status": "control_ready", "control_tail_ms": observed_end - start}
    if not active_monitor:
        return {**row, "reason": "no_monitor_event"}

    mic = to_analysis(sources[0])
    origin = max(0, start - 4) * RATE_MS
    direct_template = mic[origin:min(core + 8, start + 300) * RATE_MS]
    digital = []
    cached = {}
    for index, groups in enumerate(monitor_groups, 1):
        if not groups:
            continue
        monitor = cached[index] = to_analysis(sources[index])
        floor = max(noise[index] * 4, .00018)
        for onset, end in groups:
            if not start - 40 <= onset <= start + 1520 or end - onset < 48:
                continue
            for candidate in match(direct_template, monitor, (onset - 44) * RATE_MS,
                                   (onset + 44) * RATE_MS, floor):
                digital.append({**candidate, "channel": LABELS[index], "channel_index": index,
                                "event_ms": [onset, end], "interval": interval(candidate, origin)})
    row["digital_candidates"] = digital
    # The same mixed signal often occurs in both monitor channels. Collapse
    # only a cross-channel agreement; different viable lags are ambiguous.
    plausible = [candidate for candidate in digital if candidate["confidence"] != "low"]
    strong = [candidate for candidate in plausible if candidate["confidence"] == "high"]
    if not strong:
        return {**row, "reason": "same_syllable_not_established_on_monitor"}
    chosen = max(strong, key=lambda candidate: candidate["waveform"])
    if any(abs(candidate["frame_16k"] - chosen["frame_16k"]) > 4 * RATE_MS for candidate in plausible):
        return {**row, "reason": "competing_monitor_alignments"}
    if routes != {ROUTE_KARA}:
        return {**row, "reason": "kara_route_not_exclusive"}
    row["kara_output_evidence"] = "same_syllable_on_exclusive_kara_route"
    row["graph_interval"] = {
        "ms": chosen["interval"]["ms"],
        "range_ms": [min(item["interval"]["range_ms"][0] for item in plausible),
                     max(item["interval"]["range_ms"][1] for item in plausible)],
    }
    monitor = cached[chosen["channel_index"]]
    monitor_origin = chosen["frame_16k"]
    end = min(chosen["event_ms"][1] + 8, monitor_origin // RATE_MS + 340) * RATE_MS
    reference = monitor[monitor_origin:end]
    guard = max(core + 12, start + acoustic_guard + 16) * RATE_MS
    low = max(origin + 35 * RATE_MS, monitor_origin - 80 * RATE_MS)
    high = monitor_origin + 350 * RATE_MS
    acoustic = match(reference, mic, low, high, max(noise[0] * 4, .00025), guard)
    row["acoustic_candidates"] = [{**candidate, "monitor_interval": interval(candidate, monitor_origin),
                                  "acoustic_interval": interval(candidate, origin)} for candidate in acoustic]
    found = unique(acoustic)
    if found:
        row["monitor_to_witness_graph"] = interval(found, monitor_origin)
        row["acoustic_interval"] = interval(found, origin)
        # The acoustic interval uses one mic channel but propagates the
        # uncertainty of both alignments used to identify the same landmark.
        digital_ms = row["graph_interval"]["ms"]
        digital_low, digital_high = row["graph_interval"]["range_ms"]
        low_ms, high_ms = row["acoustic_interval"]["range_ms"]
        row["acoustic_interval"]["range_ms"] = [low_ms - (digital_ms - digital_low),
                                              high_ms + (digital_high - digital_ms)]
    return {**row, "status": "graph_supported",
            "acoustic_status": "supported" if found else "inconclusive",
            "reason": None if found else "no_unique_delayed_acoustic_match"}


def metric_summary(rows, metric, required):
    valid = [row[metric] for row in rows if row.get(metric) is not None]
    if len(valid) < required:
        return None
    values = [row["ms"] for row in valid]
    if not all(isfinite(value) and len(row["range_ms"]) == 2
               and isfinite(row["range_ms"][0]) and isfinite(row["range_ms"][1])
               and row["range_ms"][0] <= value <= row["range_ms"][1]
               for value, row in zip(values, valid, strict=True)):
        return None
    middle = median(values)
    mad = median(abs(value - middle) for value in values)
    if mad > 8 or max(values) - min(values) > 24:
        return None
    return {"n": len(values), "median_ms": middle, "mad_ms": mad, "min_max_ms": [min(values), max(values)],
            "median_range_ms": [median(row["range_ms"][side] for row in valid) for side in (0, 1)]}


def summary(rows, stage, failure):
    result = {"status": "inconclusive", "attempts": len(rows), **dict.fromkeys(METRICS),
              "physical_output_delay": None}
    if failure:
        return {**result, "reason": failure}
    if stage == "control":
        if len(rows) == 3 and all(row["status"] == "control_ready" for row in rows):
            return {**result, "status": "pipewire_control_ready"}
        return {**result, "reason": "control_not_validated"}
    required, total = (3, 3) if stage == "diagnostic" else (8, 10)
    if len(rows) != total or metric_summary(rows, "graph_interval", required) is None:
        return {**result, "reason": "graph_pairs_insufficient_or_inconsistent"}
    if stage == "diagnostic":
        return {**result, "status": "pipewire_diagnostic_ready"}
    return {**result, "status": "pipewire_intervals",
            **{metric: metric_summary(rows, metric, required) for metric in METRICS}}


def run_campaign(args, guard):
    windows, rows = [], []
    timing = None
    failure = None
    count = 10 if args.mode == "measure" else 3
    try:
        with open_graph(args.helper, args.mic, args.sink, args.mic_channel) as capture:
            notify("Micro et monitor PipeWire · Gardez le silence pendant la stabilisation.")
            capture.read_frames(2 * RATE)
            _, ambient, _, _ = capture.read_frames(RATE)
            noise = [float(np.percentile(rms_bins(channel), 95)) for channel in ambient]
            for index in range(1, count + 1):
                notify(f"Préparation {index}/{count} · Silence.")
                _, preparation, _, _ = capture.read_frames(RATE)
                notify(f"DITES « TAC ! » MAINTENANT · {index}/{count} · Une syllabe, puis silence.")
                frame, values, routes, present = capture.read_frames(4 * RATE)
                early = any(events(preparation[channel], max(noise[channel] * 6, .003), minimum=24)
                            for channel in range(3))
                windows.append((frame, values, routes, early, present))
            # Also consume the following graph cycle, so a discontinuity that
            # surfaced while the final window was delivered still invalidates it.
            capture.read_frames(MAX_QUANTUM + 1)
            timing = capture.clock_report()
    except (CaptureError, OSError, subprocess.SubprocessError) as exc:
        failure = str(exc)
        timing = None
        notify(f"Capture PipeWire indéterminée · {failure}")
    for index, (frame, values, routes, early, present) in enumerate(windows, 1):
        row = inspect_trial(frame, values, routes, noise, args.mode, guard, present)
        if early or failure:
            row.update({**dict.fromkeys(METRICS), "status": "inconclusive",
                        "reason": "early_sound" if early else "unverified_capture"})
        rows.append(row)
        notify(f"Essai {index}/{count} · {row['status']} · {row.get('reason') or row['kara_output_evidence']}")
        direct = row["mic_event"]
        if direct:
            start, end = direct["window_ms"]
            notify(f"  Direct · {start} à {end} ms dans la fenêtre PCM")
        for label, levels in row["levels"].items():
            if levels is None:
                notify(f"  {label} · niveau indisponible sur une partie de la fenêtre · monitor inactif")
                continue
            groups = row["mic_events"] if label == "micro" else row["monitor_events"][label]
            detected = ", ".join(f"{start}–{end} ms" for start, end in groups) or "aucun événement"
            notify(f"  {label} · pic {levels['peak_dbfs']:.1f} dBFS"
                   f" · max RMS {levels['max_4ms_rms_dbfs']:.1f} dBFS · {detected}")
        for candidates, label in (("digital_candidates", "graphe"), ("acoustic_candidates", "acoustique")):
            for item in row[candidates]:
                evidence = item.get("interval") or item["acoustic_interval"]
                low, high = evidence["range_ms"]
                notify(f"  {label} · {evidence['ms']:.2f} ms · {low:.2f} à {high:.2f} ms · {item['confidence']}")
    report = summary(rows, args.mode, failure)
    notify(f"Campagne multisource · {report['status']}")
    return {"version": SCHEMA, "mode": args.mode, "mic": args.mic, "sink": args.sink,
            "mic_channel": args.mic_channel, "control_sha256": args.control_digest, "timing": timing,
            "summary": report, "trials": rows}


def load_report(path, args, stage, digest=None):
    with Path(path).open("rb") as file:
        raw = file.read(256 * 1024 + 1)
    if len(raw) > 256 * 1024:
        raise ValueError("rapport trop volumineux")
    report = json.loads(raw)
    if (report["version"] != SCHEMA or report["mode"] != stage or report["control_sha256"] != digest
            or report["mic"] != args.mic or report["sink"] != args.sink or report["mic_channel"] != args.mic_channel
            or report["timing"]["common_tap_grid"] != "synchronous_dsp_cycle"
            or summary(report["trials"], stage, None)["status"] != f"pipewire_{stage}_ready"):
        raise ValueError("rapport incohérent ou inconclusive")
    return report, sha256(raw).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("preflight", "control", "diagnostic", "measure"))
    parser.add_argument("--mic", default=DEFAULT_SOURCE, metavar="NŒUD")
    parser.add_argument("--sink", default=SINK, metavar="NŒUD", help="nœud de sortie portant les ports monitor")
    parser.add_argument("--mic-channel", default="FL", choices=("FL", "FR", "MONO"))
    parser.add_argument("--control", metavar="JSON")
    parser.add_argument("--diagnostic", metavar="JSON")
    parser.add_argument("--helper", type=Path, help="chemin d’un binaire natif déjà compilé")
    args = parser.parse_args()
    if bool(args.control) != (args.mode in ("diagnostic", "measure")) or bool(args.diagnostic) != (args.mode == "measure"):
        parser.error("diagnostic exige --control. measure exige --control et --diagnostic.")
    if args.helper:
        args.helper = args.helper.resolve()
    args.control_digest = None
    guard = 0
    try:
        if args.control:
            control, args.control_digest = load_report(args.control, args, "control")
            guard = max(row["control_tail_ms"] for row in control["trials"])
            if not 68 <= guard <= 400:
                raise ValueError("borne du contrôle hors plage")
        if args.diagnostic:
            load_report(args.diagnostic, args, "diagnostic", args.control_digest)
    except (OSError, TypeError, KeyError, ValueError, RecursionError) as exc:
        parser.error(f"rapport de référence inutilisable · {exc}")
    try:
        result = run_preflight(args) if args.mode == "preflight" else run_campaign(args, guard)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False), flush=True)
    except KeyboardInterrupt:
        notify("Mesure interrompue. Capture arrêtée.")
        return 130
    return int(result["summary"]["status"] == "inconclusive")


if __name__ == "__main__":
    # Service and terminal stops must both unwind the capture and playback.
    signal.signal(signal.SIGTERM, signal.default_int_handler)
    sys.exit(main())
