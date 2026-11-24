#!/usr/bin/env python3
"""Mesure acoustique de Kara avec un micro témoin PipeWire."""

import argparse
from contextlib import contextmanager
import json
from math import ceil, log10
import os
from select import poll
import signal
from statistics import median
import subprocess
import sys
from time import monotonic

import numpy as np

import _kara_voice_witness_analysis as voice

RATE = voice.PCM_RATE
PCM_DTYPE = "<i2"
FRAME_BYTES = 2
BYTES_PER_SECOND = RATE * FRAME_BYTES
BIN_MS = 2
BIN_FRAMES = RATE * BIN_MS // 1000
STABILIZATION_FRAMES = 2 * RATE
AMBIENT_FRAMES = RATE
PREPARATION_FRAMES = 1350 * RATE // 1000
PROMPT_GUARD_MS = 250
DIRECT_DEADLINE_MS = 2200
PAIR_MIN_MS = 35
PAIR_MAX_MS = 1100
# Keep observing after the latest admissible return to catch late or extra events.
TRIAL_FRAMES = (DIRECT_DEADLINE_MS + PAIR_MAX_MS + 400) * RATE // 1000
DEFAULT_SOURCE = "alsa_input.pci-0000_63_00.6.analog-stereo"
WITNESS_NODE = "kara-voice-latency-witness"
CLIP_SAMPLE = 32604
NOISE_MARGIN_DB = 10
SENSITIVITY_DB = 3
INSPECTION_NOISE_MARGINS_DB = (10, 5)
START_TIMEOUT_S = 5
CADENCE_TOLERANCE_S = .150
IDLE_TIMEOUT_S = .200
PROMPT_OUTPUT_LIMIT_S = .080
READ_BYTES = 8192


class CaptureError(Exception):
    def __init__(self, reason, message):
        super().__init__(message)
        self.reason = reason


class PcmCapture:
    """Consume bounded windows from one nonblocking PCM stream."""

    def __init__(self, fd):
        self.fd = fd
        os.set_blocking(fd, False)
        self.ready = poll()
        self.ready.register(fd)
        self.pending = bytearray()
        self.received_bytes = 0
        self.origin = None
        self.peak_slip = 0.0
        self.last_read = None
        self.opened = monotonic()

    @property
    def frame(self):
        # Window boundaries always consume complete S16LE frames, even if reads split one.
        return (self.received_bytes - len(self.pending)) // FRAME_BYTES

    def _read(self):
        now = monotonic()
        if self.last_read is not None and now - self.last_read >= IDLE_TIMEOUT_S:
            raise CaptureError("pcm_delayed", "Lecture PCM en retard ou flux interrompu.")
        try:
            block = os.read(self.fd, READ_BYTES)
        except BlockingIOError:
            return False
        if not block:
            raise CaptureError("pcm_eof", "Fin prématurée du flux PCM.")

        self.received_bytes += len(block)
        self.last_read = now
        if self.origin is None:
            self.origin = now
        # These are delivery-time sanity checks, not ADC timestamps. A catch-up
        # burst must not advance the experiment or become a fresh attempt.
        slip = now - self.origin - self.received_bytes / BYTES_PER_SECOND
        if self.peak_slip - slip > CADENCE_TOLERANCE_S:
            raise CaptureError("pcm_backlog", "Le PCM arrive en rafale, avec des données en attente.")
        if slip > CADENCE_TOLERANCE_S:
            raise CaptureError("pcm_delayed", "La cadence PCM a pris du retard.")
        self.peak_slip = max(self.peak_slip, slip)
        self.pending.extend(block)
        return True

    def read_frames(self, frames):
        size = frames * FRAME_BYTES
        while len(self.pending) < size:
            if self._read():
                continue
            if self.last_read is None:
                remaining = self.opened + START_TIMEOUT_S - monotonic()
            else:
                remaining = self.last_read + IDLE_TIMEOUT_S - monotonic()
            if remaining <= 0:
                raise CaptureError("pcm_timeout", "Aucune donnée PCM reçue du micro témoin.")
            self.ready.poll(ceil(remaining * 1000))
        raw = self.pending[:size]
        del self.pending[:size]
        return np.frombuffer(raw, dtype=PCM_DTYPE)

    def drain_available(self):
        # Fold every queued complete frame into the preparation window before
        # inviting the user. The cadence check bounds this drain even for a
        # producer that never lets the pipe become empty.
        while self._read():
            pass
        return self.read_frames(len(self.pending) // FRAME_BYTES)


def capture_command(source):
    # PipeWire's PulseAudio endpoint accepts an explicit source and a small
    # capture fragment. parec writes raw bytes without stdio buffering.
    return [
        "parec", "--raw", "--format=s16le", f"--rate={RATE}", "--channels=1",
        "--latency-msec=40", "--process-time-msec=20", f"--device={source}",
        f"--client-name={WITNESS_NODE}", f"--stream-name={WITNESS_NODE}",
        f"--property=node.name={WITNESS_NODE}", "--property=node.dont-fallback=true",
        "--property=node.dont-move=true",
        "--property=node.dont-reconnect=true",
    ]


def stop_capture(process):
    status = process.poll()
    if status is not None:
        return f"parec s’est arrêté avant la fermeture demandée (code {status})."
    # Keep the read end open until the child has stopped. SIGTERM is sent only
    # to this capture process, whose stderr stays visible for genuine failures.
    try:
        process.terminate()
        deadline = monotonic() + 1
        fd = process.stdout.fileno()
        os.set_blocking(fd, False)
        while (status := process.poll()) is None:
            remaining = deadline - monotonic()
            if remaining <= 0:
                process.kill()
                process.wait()
                return "parec n’a pas répondu à l’arrêt normal et a dû être arrêté de force."
            # Leave room for the producer to finish an in-flight write.
            try:
                os.read(fd, READ_BYTES)
            except BlockingIOError:
                pass
            try:
                status = process.wait(timeout=min(.02, remaining))
                break
            except subprocess.TimeoutExpired:
                pass
    except KeyboardInterrupt:
        process.kill()
        process.wait()
        raise
    if status not in (0, -signal.SIGTERM):
        return f"Erreur de parec à la fermeture (code {status})."
    return None


@contextmanager
def open_capture(source):
    # Own a single child and isolate it from terminal Ctrl+C. Cleanup orders
    # stop, reap, then close for success, read failure, and interruption alike.
    with subprocess.Popen(capture_command(source), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, bufsize=0, start_new_session=True) as process:
        try:
            yield PcmCapture(process.stdout.fileno())
        except BaseException:
            problem = stop_capture(process)
            if problem:
                notify(problem)
            raise
        else:
            problem = stop_capture(process)
            if problem:
                raise CaptureError("capture_exit", problem)


def dbfs(amplitude):
    # A finite display floor keeps silence representable in JSON.
    return 20 * log10(max(float(amplitude), 1e-6))


def rms_bins(samples):
    blocks = samples.astype(np.float32).reshape(-1, BIN_FRAMES) / 32768
    return np.sqrt(np.mean(blocks * blocks, axis=1))


def peak_sample(samples):
    # Widen before abs so the negative S16LE rail cannot wrap.
    return int(np.abs(samples.astype(np.int32)).max(initial=0))


def calibrate(samples, manual_threshold=None):
    levels = rms_bins(samples)
    rms = dbfs(np.sqrt(np.mean(levels * levels)))
    p95 = dbfs(np.percentile(levels, 95))
    floor = max(rms, p95)
    threshold = min(-22, max(-44, ceil(floor + NOISE_MARGIN_DB)))
    if manual_threshold is not None:
        threshold = manual_threshold
    quarter_levels = [dbfs(np.percentile(quarter, 95)) for quarter in np.split(levels, 4)]
    if floor > threshold - NOISE_MARGIN_DB:
        status = "ambient_too_loud"
    elif (peak_sample(samples) >= CLIP_SAMPLE
          or dbfs(levels.max()) >= threshold - SENSITIVITY_DB
          or max(quarter_levels) - max(-60, min(quarter_levels)) > 6):
        status = "ambient_unstable"
    else:
        status = "ok"
    return {"status": status, "rms_dbfs": rms, "p95_dbfs": p95, "threshold_dbfs": threshold}


def groups(levels, threshold):
    active = np.flatnonzero(levels >= threshold)
    if not active.size:
        return []
    # Fuse nearby energy from one click. Keep separable clusters for rejection.
    clusters = np.split(active, np.flatnonzero(np.diff(active) * BIN_MS > 16) + 1)
    return [(int(cluster[0]) * BIN_MS, (int(cluster[-1] - cluster[0]) + 1) * BIN_MS,
             float(levels[cluster].max())) for cluster in clusters]


def reflection_possible(clusters):
    # A nearby weaker cluster can be a reflection or a second sound. The
    # heuristic supplies a diagnostic only and never chooses an event pair.
    return any(later[0] - earlier[0] <= 100 and later[2] <= .5 * earlier[2]
               for earlier, later in zip(clusters, clusters[1:]))


def identify(samples, threshold_dbfs, mode, noise_p95_dbfs=-120, preparation=None):
    if len(samples) != TRIAL_FRAMES:
        return {"status": "incomplete"}
    peak = peak_sample(samples)
    clipped = peak >= CLIP_SAMPLE
    diagnostics = {"peak_dbfs": dbfs(peak / 32768), "clipped": clipped}
    if clipped:
        return {**diagnostics, "status": "clipping"}

    levels = rms_bins(samples)
    low_threshold = 10 ** ((threshold_dbfs - SENSITIVITY_DB) / 20)
    if preparation is not None:
        # End-align full bins to the invitation. A loud pre-invite event
        # could otherwise leave only its echo inside the attempt window.
        preparation = preparation[len(preparation) % BIN_FRAMES:]
        preparation_peak = peak_sample(preparation)
        if preparation_peak >= CLIP_SAMPLE or np.any(rms_bins(preparation) >= low_threshold):
            return {**diagnostics, "status": "early_event", "phase": "preparation",
                    "preparation_peak_dbfs": dbfs(preparation_peak / 32768),
                    "preparation_clipped": preparation_peak >= CLIP_SAMPLE}
    lowest = groups(levels, low_threshold)
    if lowest and lowest[0][0] < PROMPT_GUARD_MS:
        return {**diagnostics, "status": "early_event"}
    if lowest and lowest[0][0] > DIRECT_DEADLINE_MS:
        return {**diagnostics, "status": "late_event"}

    required = 1 if mode == "control" else 2
    nominal = groups(levels, 10 ** (threshold_dbfs / 20))
    if not nominal:
        weak_floor = max(threshold_dbfs - 9, noise_p95_dbfs + 5)
        reason = "weak_event" if dbfs(levels.max()) >= weak_floor else "missing_event"
        return {**diagnostics, "status": reason}
    if len(nominal) > required:
        reason = "possible_reflections" if reflection_possible(nominal) else "ambiguous_events"
        return {**diagnostics, "status": reason, "event_count": len(nominal)}
    if len(nominal) < required and len(lowest) < required:
        return {**diagnostics, "status": "missing_event", "phase": "return"}
    if len(lowest) > required and reflection_possible(lowest):
        return {**diagnostics, "status": "possible_reflections", "event_count": len(lowest)}

    highest = groups(levels, 10 ** ((threshold_dbfs + SENSITIVITY_DB) / 20))
    starts = []
    for clusters in (nominal, lowest, highest):
        if len(clusters) != required:
            return {**diagnostics, "status": "threshold_sensitive"}
        if any(duration > 100 for _, duration, _ in clusters):
            return {**diagnostics, "status": "event_too_long"}
        current = [start for start, _, _ in clusters]
        if not PROMPT_GUARD_MS <= current[0] <= DIRECT_DEADLINE_MS:
            return {**diagnostics, "status": "threshold_sensitive"}
        starts.append(current)
    if any(max(times) - min(times) > 8 for times in zip(*starts)):
        return {**diagnostics, "status": "threshold_sensitive"}

    result = {**diagnostics, "status": "accepted", "direct_ms": starts[0][0]}
    if mode == "control":
        return result
    # Differences use sample indices on the same PCM timeline. Delivery and
    # terminal timestamps cannot enter the interval or its sensitivity bounds.
    differences = [returned - direct for direct, returned in starts]
    if min(differences) < PAIR_MIN_MS or max(differences) > PAIR_MAX_MS:
        return {**diagnostics, "status": "pair_outside_range"}
    return {**result, "return_ms": starts[0][1], "witness_interval_ms": differences[0],
            "threshold_and_bin_range_ms": [min(differences) - BIN_MS, max(differences) + BIN_MS]}


def inspect_trial(samples, decision, ambient):
    """Describe candidate events without feeding them back into the V2 decision."""
    nominal_db = ambient["threshold_dbfs"]
    noise_floor = max(ambient["rms_dbfs"], ambient["p95_dbfs"])
    diagnostic_db = [noise_floor + margin for margin in INSPECTION_NOISE_MARGINS_DB
                     if noise_floor + margin < nominal_db]
    report = {"threshold_dbfs": nominal_db, "noise_floor_dbfs": noise_floor,
              "diagnostic_thresholds_dbfs": diagnostic_db, "v2_detection": [],
              "direct": None, "secondary_peaks": []}
    if samples is None or decision["status"] in ("clipping", "incomplete"):
        return report

    levels = rms_bins(samples)
    low_db = nominal_db - SENSITIVITY_DB
    v2_thresholds = (low_db, nominal_db, nominal_db + SENSITIVITY_DB)
    by_threshold = {db: groups(levels, 10 ** (db / 20))
                    for db in dict.fromkeys((*v2_thresholds, *diagnostic_db))}
    for db in v2_thresholds:
        clusters = by_threshold[db]
        evidence = {"threshold_dbfs": db, "event_count": len(clusters)}
        if 1 <= len(clusters) <= 2:
            evidence["start_ms"] = [cluster[0] for cluster in clusters]
            evidence["duration_ms"] = [cluster[1] for cluster in clusters]
        report["v2_detection"].append(evidence)

    lowest = by_threshold[low_db]
    if (decision.get("phase") == "preparation" or not lowest
            or not PROMPT_GUARD_MS <= lowest[0][0] <= DIRECT_DEADLINE_MS):
        return report
    # Anchor to the first plausible V2 event. Prefer its nominal onset when
    # available, since lowering the threshold can expose an earlier tail.
    direct = lowest[0]
    direct_db = low_db
    nominal = by_threshold[nominal_db]
    if nominal and nominal[0][0] < direct[0] + direct[1]:
        direct = nominal[0]
        direct_db = nominal_db
    direct_ms, direct_duration, direct_amplitude = direct
    direct_level = dbfs(direct_amplitude)
    report["direct"] = {"start_ms": direct_ms, "duration_ms": direct_duration,
                        "level_dbfs": direct_level, "detection_threshold_dbfs": direct_db}

    # Full-window grouping preserves a group's true onset at either edge of
    # the search window. A lower threshold can join two stronger groups via
    # their tails. Keep the stronger detections and do not count the joined
    # group again or give a tail its own onset.
    occupied = np.zeros(len(levels), dtype=bool)
    candidates = []
    for db in sorted({nominal_db, low_db, *diagnostic_db}, reverse=True):
        for cluster in by_threshold[db]:
            start, duration, _ = cluster
            span = occupied[start // BIN_MS:(start + duration) // BIN_MS]
            if np.any(span):
                continue
            span[:] = True
            if PAIR_MIN_MS <= start - direct_ms <= PAIR_MAX_MS:
                candidates.append((cluster, db))

    previous = direct
    for cluster, db in sorted(candidates):
        start, duration, amplitude = cluster
        level = dbfs(amplitude)
        # Proximity and attenuation suggest a possible reflection. The witness
        # has only one channel and cannot attribute any of these peaks to Kara.
        report["secondary_peaks"].append({
            "start_ms": start, "delay_ms": start - direct_ms, "duration_ms": duration,
            "level_dbfs": level, "relative_db": level - direct_level,
            "detection_threshold_dbfs": db,
            "possible_reflection": (reflection_possible((direct, cluster))
                                    or reflection_possible((previous, cluster))),
        })
        previous = cluster
    return report


def summarize(trials, mode, target=5, failure=None):
    accepted = [row for row in trials if row["status"] == "accepted"]
    result = {"attempts": len(trials), "accepted": len(accepted)}
    if failure is None and any(row["status"] == "incomplete" for row in trials):
        failure = "incomplete_capture"
    if failure:
        return {**result, "status": failure, "median_witness_ms": None}
    if mode == "control":
        status = "single_events" if len(accepted) >= max(5, target) else "inconclusive"
        return {**result, "status": status}
    if len(accepted) < max(8, ceil(.8 * len(trials))):
        return {**result, "status": "insufficient_pairs", "median_witness_ms": None}
    values = [row["witness_interval_ms"] for row in accepted]
    center = median(values)
    return {**result, "status": "witness_intervals", "median_witness_ms": center,
            "mad_ms": median(abs(value - center) for value in values),
            "min_max_ms": [min(values), max(values)],
            "median_threshold_and_bin_range_ms": [
                median(row["threshold_and_bin_range_ms"][index] for row in accepted)
                for index in (0, 1)]}


def notify(message):
    print(message, file=sys.stderr, flush=True)


def describe_trial(row):
    level = f"Pic capté {row['peak_dbfs']:.1f} dBFS."
    if row["status"] == "accepted":
        return f"Essai accepté. {level}"
    if row.get("preparation_clipped"):
        return ("Essai rejeté. Saturation pendant la préparation. Gardez le silence avant l’invitation. "
                f"Pic avant l’invite {row['preparation_peak_dbfs']:.1f} dBFS.")
    reasons = {
        "clipping": ("Signal saturé ou proche du rail. Claquez plus doucement ou plus loin du témoin. "
                     "Si nécessaire, baissez le gain d’entrée manuellement."),
        "early_event": "Son détecté avant la fenêtre utile. Attendez l’invitation et gardez le silence entre les essais.",
        "late_event": "Claquement trop tardif. Recommencez juste après l’invitation.",
        "weak_event": "Claquement trop faible. Rapprochez les mains du Vivobook.",
        "missing_event": ("Retour distinct introuvable. Vérifiez qu’il joue et que le témoin l’entend."
                          if row.get("phase") == "return"
                          else "Aucun claquement détecté. Rapprochez les mains du Vivobook."),
        "ambiguous_events": "Plusieurs événements distincts. Faites un seul claquement, puis silence.",
        "possible_reflections": "Réflexions possibles. La mesure est ambiguë. Éloignez-vous des surfaces proches.",
        "threshold_sensitive": "Détection instable selon le seuil. Vérifiez le placement et le niveau du son.",
        "event_too_long": "Son trop long ou réverbérant. Faites un claquement bref.",
        "pair_outside_range": f"Paire hors de l’intervalle {PAIR_MIN_MS} à {PAIR_MAX_MS} ms.",
    }
    return f"Essai rejeté. {reasons[row['status']]} {level}"


def describe_inspection(report):
    nominal = report["threshold_dbfs"]
    extra = ", ".join(f"{db:.1f}" for db in report["diagnostic_thresholds_dbfs"])
    lines = [f"Pics acoustiques · seuil nominal {nominal:.1f} dBFS · "
             f"fond {report['noise_floor_dbfs']:.1f} dBFS.",
             f"Seuils de diagnostic sous le nominal · {extra} dBFS." if extra
             else "Aucun seuil de diagnostic supplémentaire au-dessous du nominal."]
    for detection in report["v2_detection"]:
        count = detection["event_count"]
        line = f"  V2 à {detection['threshold_dbfs']:.1f} dBFS · {count} groupe(s)"
        if "start_ms" in detection:
            intervals = [f"{start} ms (durée {duration} ms)" for start, duration
                         in zip(detection["start_ms"], detection["duration_ms"])]
            line += " · " + ", ".join(intervals)
        lines.append(line)

    direct = report["direct"]
    if direct is not None:
        label = " · sous le seuil nominal" if direct["detection_threshold_dbfs"] < nominal else ""
        lines.append(f"Direct présumé · t={direct['start_ms']} ms depuis le début de la fenêtre PCM · "
                     f"{direct['level_dbfs']:.1f} dBFS RMS max sur {BIN_MS} ms{label}.")
    else:
        lines.append("Claquement direct non localisable.")

    if report["secondary_peaks"]:
        lines.append("Pics secondaires d’origine indéterminée · débuts de groupes sur la fenêtre PCM.")
        for peak in report["secondary_peaks"]:
            label = " · sous le seuil nominal" if peak["detection_threshold_dbfs"] < nominal else ""
            reflection = " · réflexion possible" if peak["possible_reflection"] else ""
            lines.append(f"  +{peak['delay_ms']} ms · t={peak['start_ms']} ms · "
                         f"{peak['level_dbfs']:.1f} dBFS ({peak['relative_db']:+.1f} dB vs direct) · "
                         f"durée {peak['duration_ms']} ms · "
                         f"détecté à {peak['detection_threshold_dbfs']:.1f} dBFS{label}{reflection}.")
    elif direct is not None:
        lines.append(f"Aucun second événement identifiable entre {PAIR_MIN_MS} et {PAIR_MAX_MS} ms après le direct.")
    else:
        lines.append("Aucun second événement identifiable sans repère direct.")
    return "\n".join(lines)


def identify_voice(samples, preparation, ambient, stage, control_ranges, *, control_ready=True):
    row = {"status": "inconclusive", "direct_ms": None, "candidates": []}
    if len(samples) != TRIAL_FRAMES:
        return {**row, "reason": "incomplete"}
    peak = peak_sample(samples)
    row["peak_dbfs"] = dbfs(peak / 32768)
    if peak >= CLIP_SAMPLE:
        return {**row, "reason": "clipping"}
    threshold = ambient["threshold_dbfs"]
    preparation = preparation[len(preparation) % BIN_FRAMES:]
    low = 10 ** ((threshold - SENSITIVITY_DB) / 20)
    if peak_sample(preparation) >= CLIP_SAMPLE or np.any(rms_bins(preparation) >= low):
        return {**row, "reason": "early_event"}

    # Voice onset establishes a template origin, not the latency clock.
    # Reject a missing or unstable anchor before comparing delayed copies.
    levels = rms_bins(samples)
    onsets = []
    for offset in (-SENSITIVITY_DB, 0, SENSITIVITY_DB):
        active = levels >= 10 ** ((threshold + offset) / 20)
        sustained = np.flatnonzero(active[:-2] & active[1:-1] & active[2:])
        if not len(sustained):
            return {**row, "reason": "direct_missing_or_weak"}
        onsets.append(int(sustained[0]) * BIN_MS)
    if onsets[0] < PROMPT_GUARD_MS or onsets[-1] > DIRECT_DEADLINE_MS:
        return {**row, "reason": "direct_outside_window"}
    if onsets[-1] - onsets[0] > 12:
        return {**row, "reason": "direct_onset_unstable"}
    row["direct_ms"] = onsets[1]
    row["direct_onset_range_ms"] = [onsets[0], onsets[-1] + BIN_MS]
    analysis_end = row["direct_ms"] + voice.SEARCH_AFTER_ONSET_MS
    late = levels[analysis_end // BIN_MS:] >= low
    if np.any(late[:-2] & late[1:-1] & late[2:]):
        return {**row, "reason": "unexpected_late_sound"}
    noise = max(ambient["p95_dbfs"], ambient["rms_dbfs"])
    reports, clean = voice.candidates(samples, row["direct_ms"], noise)
    row["candidates"] = reports
    if stage == "control":
        # A repeatable reflection is useful control evidence. An unexplained
        # second sound cannot establish the room's baseline.
        if clean or any(item["confidence"] == "high" for item in reports):
            return {**row, "status": "control_examined"}
        return {**row, "reason": "unexplained_control_sound"}
    supported, reason = voice.classify(reports, control_ranges)
    if not control_ready:
        return {**row, "reason": "control_not_validated"}
    if supported:
        return {**row, "status": "supported", "witness_interval_ms": supported["delay_ms"],
                "lag_range_ms": supported["lag_range_ms"]}
    return {**row, "reason": reason}


def describe_voice(row):
    direct = row["direct_ms"]
    status = {"control_examined": "contrôle examiné", "supported": "candidat soutenu"}.get(
        row["status"], row["status"])
    lines = [f"Voix · {status}" + (f" · direct à {direct} ms sur le PCM." if direct is not None else ".")]
    if "reason" in row:
        reasons = {
            "incomplete": "Fenêtre PCM incomplète.",
            "clipping": "Signal saturé ou proche du rail.",
            "early_event": "Son détecté pendant la préparation.",
            "direct_missing_or_weak": "Voix directe absente ou trop faible.",
            "direct_outside_window": "Voix directe hors de la fenêtre utile.",
            "direct_onset_unstable": "Début du direct instable selon le seuil.",
            "unexpected_late_sound": "Autre son détecté en fin d’essai.",
            "unexplained_control_sound": "Durée ou sons observés incompatibles avec un contrôle exploitable.",
            "candidate_matches_control": "Les candidats plausibles recoupent le contrôle retour coupé.",
            "no_supported_candidate": "Preuves insuffisantes d’une reproduction distincte.",
            "competing_candidates": "Plusieurs délais de restitution restent plausibles.",
            "insufficient_candidate_confidence": "Confiance insuffisante pour attribuer le candidat au retour.",
            "control_not_validated": "Candidats affichés pour diagnostic. Le contrôle vocal reste à valider.",
        }
        lines.append(f"  {reasons[row['reason']]}")
    if not row["candidates"]:
        lines.append("  Aucun candidat de restitution identifiable.")
    labels = {"high": "élevée", "moderate": "modérée", "low": "faible"}
    for item in row["candidates"]:
        room = " · compatible avec le contrôle" if item.get("matches_control") else ""
        overlap = " · chevauchement possible" if item["overlap"] else ""
        competitor = item["nearby_competitor_ms"]
        alias = f" · autre alignement à +{competitor:.2f} ms" if competitor is not None else ""
        lines.append(f"  +{item['delay_ms']:.2f} ms · confiance {labels[item['confidence']]}"
                     f" · plage {item['lag_range_ms'][0]:.2f} à {item['lag_range_ms'][1]:.2f} ms"
                     f" · forme {item['waveform']:.2f}, enveloppe {item['envelope']:.2f},"
                     f" spectre {item['spectral']:.2f}{overlap}{room}{alias}")
    return "\n".join(lines)


def run_campaign(source, mode, count, retries, threshold, emit=notify, *, inspect_peaks=False,
                 voice_stage=None, control_ranges=(), control_digest=None, reference_floors=(),
                 control_ready=True):
    rows = []
    voice_windows = []
    ambient = None
    failure = None
    error = None
    attempt_frame = None
    try:
        with open_capture(source) as capture:
            microphone = f"Vivobook ({source})" if source == DEFAULT_SOURCE else source
            emit(f"Micro témoin · {microphone}\nStabilisation du microphone… Gardez le silence.")
            capture.read_frames(STABILIZATION_FRAMES)
            emit("Mesure du bruit ambiant…")
            ambient = calibrate(capture.read_frames(AMBIENT_FRAMES), threshold)
            threshold = ambient["threshold_dbfs"]
            noise_label = "Bruit ambiant OK" if ambient["status"] == "ok" else "Bruit ambiant"
            emit(f"{noise_label} · RMS {ambient['rms_dbfs']:.1f} dBFS · "
                 f"fond p95 {ambient['p95_dbfs']:.1f} dBFS. Seuil {threshold:.1f} dBFS.")
            if ambient["status"] != "ok":
                failure = ambient["status"]
                problem = ("Bruit ambiant trop élevé" if failure == "ambient_too_loud"
                           else "Bruit ambiant instable")
                emit(f"{problem}. Faites le silence, puis relancez la mesure.")
            elif voice_stage and any(abs(max(-70, floor) - max(-70, ambient["p95_dbfs"])) > 6
                                     for floor in reference_floors):
                failure = "ambient_changed"
                emit("Le bruit de fond a changé depuis la référence. Refaire le contrôle vocal.")
            else:
                accepted = 0
                for attempt in range(1, count + retries + 1):
                    if attempt <= count:
                        label = f"essai {attempt}/{count}"
                    else:
                        label = f"nouvelle tentative {attempt - count}/{retries}"
                    emit(f"\nPréparation {label} · Silence.")
                    preparation = capture.read_frames(PREPARATION_FRAMES)
                    queued = capture.drain_available()
                    attempt_frame = capture.frame
                    prompted_at = monotonic()
                    if voice_stage:
                        emit("DITES « TAC ! » MAINTENANT ! Une syllabe brève et isolée, puis silence.")
                    else:
                        emit("CLAQUE MAINTENANT ! Un seul claquement, puis silence.")
                    if monotonic() - prompted_at > PROMPT_OUTPUT_LIMIT_S:
                        raise CaptureError("prompt_delayed", "L’affichage de l’invitation a pris du retard.")
                    samples = capture.read_frames(TRIAL_FRAMES)
                    if voice_stage:
                        # Defer the bounded analysis until after capture so FFT
                        # work and diagnostics cannot stall the one PCM stream.
                        voice_windows.append((attempt_frame, samples, np.concatenate((preparation, queued))))
                        attempt_frame = None
                        continue
                    row = identify(samples, threshold, mode, ambient["p95_dbfs"],
                                   np.concatenate((preparation, queued)))
                    if inspect_peaks:
                        row["peak_inspection"] = inspect_trial(samples, row, ambient)
                    rows.append({"start_frame": attempt_frame, **row})
                    attempt_frame = None
                    message = describe_trial(row)
                    if inspect_peaks:
                        message += "\n" + describe_inspection(row["peak_inspection"])
                    emit(message)
                    accepted += row["status"] == "accepted"
                    if accepted == count:
                        break
                # Catch late EOF or backlog before making a campaign publishable.
                capture.drain_available()
    except (CaptureError, OSError) as exc:
        failure = "incomplete_capture"
        reason = exc.reason if isinstance(exc, CaptureError) else "capture_io"
        error = {"reason": reason, "message": str(exc)}
        message = f"Capture témoin incomplète. {exc}"
        if attempt_frame is not None:
            row = {"status": "incomplete", "start_frame": attempt_frame, "reason": reason}
            if inspect_peaks:
                row["peak_inspection"] = inspect_trial(None, row, ambient)
                message += "\n" + describe_inspection(row["peak_inspection"])
            rows.append(row)
        emit(message)

    if voice_stage:
        voice_rows = []
        for index, (frame, samples, preparation) in enumerate(voice_windows, 1):
            row = identify_voice(samples, preparation, ambient, voice_stage, control_ranges,
                                 control_ready=control_ready)
            emit(f"Essai {index}/{count} · {describe_voice(row)}")
            voice_rows.append({"start_frame": frame, **row})
        voice_rows.extend(rows)
        if failure is None and not control_ready:
            failure = "control_not_validated"
        summary = voice.summarize(voice_rows, voice_stage, failure)
        emit(f"Campagne vocale · {summary['status']}")
        return {"mode": mode, "rate": RATE, "source": source, "threshold_dbfs": threshold,
                "ambient": ambient, "error": error, "summary": summary, "trials": voice_rows,
                "voice": {"version": voice.REFERENCE_VERSION, "stage": voice_stage,
                          "control_sha256": control_digest}}
    return {"mode": mode, "rate": RATE, "source": source, "threshold_dbfs": threshold,
            "ambient": ambient, "error": error, "summary": summarize(rows, mode, count, failure),
            "trials": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("control", "return", "voice"))
    parser.add_argument("--source", default=DEFAULT_SOURCE, help="nom exact du nœud PipeWire du micro")
    parser.add_argument("--threshold-dbfs", type=float, help="seuil fixe en dBFS (calibré par défaut)")
    parser.add_argument("--trials", type=int, help="nombre cible (contrôle 5, retour 10, voix 3 ou 10)")
    parser.add_argument("--retries", type=int, help="tentatives supplémentaires (claquements 2, voix 0)")
    parser.add_argument("--inspect-peaks", action="store_true",
                        help="en mode retour, affiche les pics candidats et les seuils de chaque essai")
    voice_pass = parser.add_mutually_exclusive_group()
    voice_pass.add_argument("--voice-control", action="store_true", help="contrôle vocal avec Kara coupé")
    voice_pass.add_argument("--voice-reference", metavar="JSON", help="rapport du contrôle vocal retour coupé")
    parser.add_argument("--voice-diagnostic", metavar="JSON", help="rapport du diagnostic vocal pour les dix essais")
    args = parser.parse_args()
    if args.inspect_peaks and args.mode != "return":
        parser.error("--inspect-peaks s’utilise avec --mode return")
    retries = args.retries if args.retries is not None else (0 if args.mode == "voice" else 2)
    voice_options = {}
    if args.mode == "voice":
        count = args.trials if args.trials is not None else 3
        if not args.voice_control and args.voice_reference is None:
            parser.error("--mode voice exige --voice-control ou --voice-reference JSON")
        if retries != 0 or count not in (3, 10):
            parser.error("la voix utilise 3 ou 10 essais et aucune tentative supplémentaire")
        if count == 10 and (args.voice_reference is None or args.voice_diagnostic is None):
            parser.error("dix essais vocaux exigent les rapports --voice-reference et --voice-diagnostic")
        if count == 3 and args.voice_diagnostic is not None:
            parser.error("--voice-diagnostic s’utilise avec --trials 10")
        stage = "control" if args.voice_control else "diagnostic" if count == 3 else "measurement"
        voice_options["voice_stage"] = stage
    else:
        if args.voice_control or args.voice_reference is not None or args.voice_diagnostic is not None:
            parser.error("les options vocales exigent --mode voice")
        count = args.trials if args.trials is not None else (5 if args.mode == "control" else 10)
        if not 1 <= count <= 20 or not 0 <= retries <= 3:
            parser.error("1 à 20 essais et 0 à 3 tentatives supplémentaires")
    if args.threshold_dbfs is not None and not -60 <= args.threshold_dbfs <= -12:
        parser.error("le seuil fixe doit être compris entre −60 et −12 dBFS")
    if (not args.source.strip() or args.source.isdecimal()
            or args.source.startswith("@") or args.source.endswith(".monitor")):
        parser.error("sélectionnez un vrai micro témoin par son nom de nœud")
    try:
        if args.voice_reference is not None:
            try:
                threshold, floor, ranges, digest, ready = voice.read_reference(
                    args.voice_reference, "control", args.source, require_ready=count == 10)
                if args.threshold_dbfs is not None and args.threshold_dbfs != threshold:
                    raise ValueError("le seuil vocal doit être identique à celui du contrôle")
                args.threshold_dbfs = threshold
                floors = [floor]
                if args.voice_diagnostic is not None:
                    diagnostic_threshold, diagnostic_floor, _, _, _ = voice.read_reference(
                        args.voice_diagnostic, "diagnostic", args.source, digest)
                    if diagnostic_threshold != threshold:
                        raise ValueError("le seuil du diagnostic diffère du contrôle")
                    floors.append(diagnostic_floor)
                voice_options.update(control_ranges=ranges, control_digest=digest, reference_floors=floors,
                                     control_ready=ready)
            except (OSError, ValueError) as exc:
                parser.error(f"référence vocale inutilisable · {exc}")
        result = run_campaign(args.source, args.mode, count, retries, args.threshold_dbfs,
                              inspect_peaks=args.inspect_peaks, **voice_options)
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False), flush=True)
    except KeyboardInterrupt:
        notify("\nMesure interrompue. Capture arrêtée.")
        return 130
    return 0 if result["summary"]["status"] in (
        "single_events", "witness_intervals", "voice_control_ready", "voice_diagnostic_ready",
        "voice_witness_intervals",
    ) else 1


if __name__ == "__main__":
    sys.exit(main())
