"""Analyse acoustique des syllabes courtes du micro témoin Kara."""

from hashlib import sha256
import json
from pathlib import Path
from statistics import median

import numpy as np


# The witness produces 48 kHz PCM. The anti-alias FIR permits a 16 kHz
# correlation grid, retaining speech information up to about 7 kHz.
PCM_RATE = 48000
RATE = 16000
MS = RATE // 1000
MIN_DELAY = 35
MAX_DELAY = 1100
MAX_VOICE = 340
SEARCH_AFTER_ONSET_MS = MAX_DELAY + MAX_VOICE + 32
MAX_DIAGNOSTIC_SPREAD = 24
HOP_MS = 4
HOP = HOP_MS * MS
FRAME = 16 * MS
ANCHOR = 24 * MS
BAND_EDGES = (125, 375, 750, 1500, 2750, 4750, 7000)
FREQUENCIES = np.fft.rfftfreq(FRAME, 1 / RATE)
FIR_TIME = np.arange(-48, 49)
FIR = (2 * 7000 / PCM_RATE * np.sinc(2 * 7000 / PCM_RATE * FIR_TIME)
       * np.hanning(FIR_TIME.size))
FIR /= FIR.sum()
REFERENCE_VERSION = 1
MAX_REFERENCE_BYTES = 128 * 1024


def bands(signal):
    windows = np.lib.stride_tricks.sliding_window_view(signal, FRAME)[::HOP]
    spectrum = np.abs(np.fft.rfft(windows * np.hanning(FRAME))) ** 2
    return np.column_stack([
        spectrum[:, (FREQUENCIES >= low) & (FREQUENCIES < high)].sum(axis=1)
        for low, high in zip(BAND_EDGES, BAND_EDGES[1:])
    ])


def similarity(template, observed):
    # Center each band in time. A fixed speaker EQ changes a band's level
    # but need not change the progression from consonant to vowel and back.
    left = np.sqrt(template.sum(axis=1))
    right = np.sqrt(observed.sum(axis=1))
    envelope = cosine(left - left.mean(), right - right.mean())
    left = np.log1p(template / max(float(template.max()) * .005, 1e-15))
    right = np.log1p(observed / max(float(observed.max()) * .005, 1e-15))
    spectral = cosine(left - left.mean(axis=0), right - right.mean(axis=0))
    return envelope, spectral


def cosine(left, right):
    denominator = np.sqrt(np.sum(left * left) * np.sum(right * right))
    return float(np.sum(left * right) / denominator) if denominator else 0.0


def waveform_matches(wave, anchors, noise_amplitude):
    """Normalized finite-window correlations for every delay on the PCM grid."""
    first, last = MIN_DELAY * MS, MAX_DELAY * MS
    nfft = 1 << (len(wave) + ANCHOR - 1).bit_length()
    spectrum = np.fft.rfft(wave, n=nfft)
    cumulative = np.r_[0, np.cumsum(wave ** 2)]
    delays = np.arange(first, last + 1)
    positions = anchors[:, None] + delays
    target_energy = cumulative[positions + ANCHOR] - cumulative[positions]
    templates = np.lib.stride_tricks.sliding_window_view(wave, ANCHOR)[anchors]
    energy = (templates ** 2).sum(axis=1)
    usable = energy >= max(energy.max() * .004, noise_amplitude ** 2 * ANCHOR)
    cross = np.fft.irfft(spectrum * np.fft.rfft(templates[:, ::-1], n=nfft), n=nfft)
    dot = np.take_along_axis(cross, positions + ANCHOR - 1, axis=1)
    scale = np.sqrt(energy[:, None] * np.maximum(target_energy, 1e-15))
    correlation = np.divide(dot, scale, out=np.zeros_like(dot), where=scale > 0)
    correlation[~usable] = 0
    return np.clip(correlation, -1, 1), dot / np.maximum(energy[:, None], 1e-15), usable


def quiet_groups(levels, floor):
    active = np.flatnonzero(levels >= floor)
    if not active.size:
        return []
    # Gaps within the syllable (especially before the final consonant)
    # are not themselves evidence of a second event.
    return [(int(run[0]), int(run[-1]) + 1)
            for run in np.split(active, np.flatnonzero(np.diff(active) > 11) + 1)
            if len(run) >= 3]


def block_rms(signal):
    blocks = signal[:len(signal) // HOP * HOP].reshape(-1, HOP)
    return np.sqrt(np.mean(blocks ** 2, axis=1))


def competing_tail_lag(wave, tail_start, tail_end, delay, coherence):
    # The search suppresses nearby sidelobes to find distinct candidates.
    # Independently inspect that suppressed neighborhood before accepting:
    # a nearly periodic vowel or two similar arrivals can both peak there.
    low = max(MIN_DELAY * MS, delay - 6 * MS)
    high = min(MAX_DELAY * MS, delay + 6 * MS)
    lags = np.arange(low, high + 1)
    alternate = np.abs(lags - delay) >= 1.5 * MS
    if not np.any(alternate):
        return None
    template = wave[tail_start:tail_end]
    target = wave[tail_start + low:tail_end + high]
    cumulative = np.r_[0, np.cumsum(target ** 2)]
    length = len(template)
    energy = (cumulative[length:] - cumulative[:-length]) * np.sum(template ** 2)
    scores = np.abs(np.correlate(target, template, mode="valid")) / np.sqrt(np.maximum(energy, 1e-15))
    scores[~alternate] = 0
    candidate = int(np.argmax(scores))
    if scores[candidate] >= max(.55, coherence * .87):
        return float(lags[candidate] / MS)
    return None


def assess(raw, wave, anchors, correlation, gains, usable, lag_index, floor, all_groups):
    delay = MIN_DELAY * MS + lag_index
    # Fit a signed gain to aligned windows. In a mixture, a delayed onset
    # can be buried under the direct vowel. Require support at distinct
    # positions and verify a delayed tail in the observed waveform itself.
    aligned = correlation[:, lag_index]
    polarity = 1 if np.sum(aligned ** 3) >= 0 else -1
    support = np.flatnonzero((polarity * aligned >= .19) & usable
                             & (polarity * gains[:, lag_index] >= .006))
    if len(support) < 2:
        return None
    strength = polarity * aligned[support]
    fit = support[strength >= max(.35, float(strength.max()) * .66)]
    if not len(fit):
        return None
    gain = float(np.median(gains[fit, lag_index]))
    support = support[(np.abs(gains[support, lag_index]) >= abs(gain) * .25)
                      & (np.abs(gains[support, lag_index]) <= abs(gain) * 4)]
    if len(support) < 2 or abs(gain) > 1.25:
        return None

    # Causal subtraction is used only to locate a plausible direct endpoint.
    # Scores below compare PCM from the original mixture. A constant gain
    # is an approximation, so the endpoint uses a floor tied to direct level.
    residual = raw.copy()
    for start in range(delay, len(raw), delay):
        end = min(start + delay, len(raw))
        residual[start:end] -= gain * residual[start - delay:end - delay]
    levels = block_rms(residual)
    direct_floor = max(floor, float(levels[:MAX_VOICE // HOP_MS].max()) * .16)
    last_direct = np.flatnonzero(levels >= direct_floor)
    if not len(last_direct):
        return None
    direct_end = (int(last_direct[-1]) + 1) * HOP
    tail_start = max(0, direct_end - delay)
    tail_end = min(direct_end + 12 * MS, MAX_VOICE * MS)
    tail_length = tail_end - tail_start
    tail_wave = 0.0
    tail_audible = False
    envelope, spectral = 0.0, 0.0
    if tail_length >= 36 * MS and tail_end + delay <= len(raw):
        original = slice(tail_start, tail_end)
        returned = slice(tail_start + delay, tail_end + delay)
        audible = block_rms(raw[returned]) >= floor
        tail_audible = np.count_nonzero(audible) >= 5
        tail_wave = polarity * cosine(wave[original], wave[returned])
        envelope, spectral = similarity(bands(raw[original]), bands(raw[returned]))

    # Local peaks expose sensitivity to the chosen speech anchor.
    # The feature hop is kept as an allowance, even for exact synthetic copies.
    low = max(0, lag_index - 8 * MS)
    high = min(correlation.shape[1], lag_index + 8 * MS + 1)
    strong_anchors = support[polarity * aligned[support] >= .45]
    if not len(strong_anchors):
        strong_anchors = support
    local = polarity * correlation[strong_anchors, low:high]
    maxima = (local >= np.pad(local[:, :-1], ((0, 0), (1, 0)), constant_values=-1))
    maxima &= (local >= np.pad(local[:, 1:], ((0, 0), (0, 1)), constant_values=-1))
    # Pitch aliases can be nearly as high within one anchor. Keep the
    # nearest local peak when it is within .07 of that anchor's maximum.
    viable = maxima & (local >= local.max(axis=1, keepdims=True) - .07)
    offsets = np.abs(np.arange(low, high) - lag_index)
    peaks = low + np.argmin(np.where(viable, offsets, high - low), axis=1)
    spread = float(np.ptp(np.r_[peaks, lag_index]) / MS)
    delay_ms = delay / MS
    uncertainty = [(MIN_DELAY * MS + min(lag_index, int(peaks.min()))) / MS - HOP_MS,
                   (MIN_DELAY * MS + max(lag_index, int(peaks.max()))) / MS + HOP_MS]
    span = (anchors[support[-1]] - anchors[support[0]]) / MS
    # A high-amplitude event elsewhere in the recording is a competing
    # explanation. Short trailing noise under this floor is left in the data.
    unexpected = any(end * HOP > direct_end + delay + 28 * MS
                     for _, end in all_groups)
    usable_direct = 70 * MS <= direct_end <= MAX_VOICE * MS
    independent_tail = np.any((anchors[support] + delay >= direct_end - 4 * MS)
                              & (polarity * aligned[support] >= .42))
    has_structure = tail_audible and tail_wave >= .54 and envelope >= .52 and spectral >= .42
    confidence = "low"
    nearby_competitor = None
    if usable_direct and has_structure and span >= 48 and independent_tail and not unexpected:
        confidence = "high" if spread <= 6 else "moderate"
    elif usable_direct and tail_audible and tail_wave >= .32 and max(envelope, spectral) >= .45:
        confidence = "moderate"
    if confidence == "high":
        nearby_competitor = competing_tail_lag(wave, tail_start, tail_end, delay, tail_wave)
        if nearby_competitor is not None:
            confidence = "moderate"
            uncertainty = [min(uncertainty[0], nearby_competitor - HOP_MS),
                           max(uncertainty[1], nearby_competitor + HOP_MS)]
    return {
        "delay_ms": round(delay_ms, 3),
        "lag_range_ms": [round(value, 3) for value in uncertainty],
        "confidence": confidence,
        "overlap": delay < direct_end,
        "anchor_count": int(len(support)),
        "anchor_span_ms": float(span),
        "anchor_spread_ms": round(spread, 3),
        "waveform": round(tail_wave, 3),
        "envelope": round(envelope, 3),
        "spectral": round(spectral, 3),
        "relative_db": round(float(20 * np.log10(abs(gain))), 1),
        "nearby_competitor_ms": None if nearby_competitor is None else round(nearby_competitor, 3),
    }


def separated_candidates(raw, wave, groups, reports):
    """Surface isolated voice-like events even if processing obscures phase."""
    first, end = groups[0]
    duration = (end - first) * HOP
    if not 70 * MS <= duration <= MAX_VOICE * MS:
        return
    for start, stop in groups[1:]:
        delay = (start - first) * HOP
        if not MIN_DELAY * MS <= delay <= MAX_DELAY * MS:
            continue
        length = min(duration, (stop - start) * HOP)
        if length < 40 * MS:
            continue
        left = slice(first * HOP, first * HOP + length)
        right = slice(start * HOP, start * HOP + length)
        envelope, spectral = similarity(bands(raw[left]), bands(raw[right]))
        waveform = abs(cosine(wave[left], wave[right]))
        similar_length = length >= .75 * max(duration, (stop - start) * HOP)
        moderate = envelope >= .68 and spectral >= .65 and similar_length
        confidence = "moderate" if moderate else "low"
        delay_ms = delay / MS
        # An envelope-only onset is a hypothesis with a coarser sensitivity
        # allowance. It can veto attribution, but cannot publish a latency.
        if any(abs(item["delay_ms"] - delay_ms) <= 24
               and (item["confidence"] != "low" or confidence == "low") for item in reports):
            continue
        ratio = np.sqrt(np.sum(raw[right] ** 2) / np.sum(raw[left] ** 2))
        reports.append({
            "delay_ms": delay_ms, "lag_range_ms": [delay_ms - 24, delay_ms + 24],
            "confidence": confidence, "overlap": False,
            "anchor_count": 0, "anchor_span_ms": 0, "anchor_spread_ms": None,
            "waveform": round(waveform, 3), "envelope": round(envelope, 3),
            "spectral": round(spectral, 3), "relative_db": round(float(20 * np.log10(ratio)), 1),
            "nearby_competitor_ms": None,
        })


def candidates(samples, onset_ms, noise_dbfs):
    # Include a little pre-roll so the first correlation anchor retains the
    # attack. All lags are relative samples, independent of the prompt clock.
    start_ms = onset_ms - 8
    end_ms = onset_ms + SEARCH_AFTER_ONSET_MS
    pcm_ms = PCM_RATE // 1000
    pcm = samples[start_ms * pcm_ms:end_ms * pcm_ms].astype(float) / 32768
    raw = np.convolve(pcm, FIR, mode="same")[::PCM_RATE // RATE]
    # Pre-emphasis reduces the influence of low-frequency pitch periodicity.
    wave = np.diff(raw, prepend=0) + .15 * np.r_[0, raw[:-1]]
    noise_amplitude = 10 ** (noise_dbfs / 20)
    levels = block_rms(raw)
    floor = max(noise_amplitude * 3.16, float(levels[:18].max()) * .04)
    all_groups = quiet_groups(levels, floor)
    if not all_groups:
        return [], False
    first_end = all_groups[0][1] * HOP
    anchors = np.arange(0, min(first_end, MAX_VOICE * MS) - ANCHOR + 1, 16 * MS)
    if len(anchors) < 4:
        return [], False
    correlation, gains, usable = waveform_matches(wave, anchors, noise_amplitude * .5)
    # Peaks require support in three windows with consistent polarity. Keep
    # a bounded shortlist for spectral checks, suppressing nearby sidelobes.
    ordered = np.sort(correlation, axis=0)
    scores = np.maximum(ordered[-3:].mean(axis=0), -ordered[:3].mean(axis=0))
    reports = []
    for _ in range(24):
        index = int(np.argmax(scores))
        if scores[index] < .22:
            break
        report = assess(raw, wave, anchors, correlation, gains, usable, index, floor, all_groups)
        if report is not None:
            reports.append(report)
        radius = 6 * MS
        scores[max(0, index - radius):index + radius + 1] = 0
    separated_candidates(raw, wave, all_groups, reports)
    rank = {"high": 2, "moderate": 1, "low": 0}
    reports.sort(key=lambda item: (rank[item["confidence"]], item["waveform"]), reverse=True)
    clean = (len(all_groups) == 1 and 70 * MS <= first_end <= MAX_VOICE * MS)
    plausible = [item for item in reports if item["confidence"] != "low"]
    low = [item for item in reports if item["confidence"] == "low"]
    return plausible + low[:3], clean


def classify(reports, control_ranges):
    for report in reports:
        low, high = report["lag_range_ms"]
        report["matches_control"] = any(start <= high + 12 and end >= low - 12
                                        for start, end in control_ranges)
    plausible = [item for item in reports if item["confidence"] != "low"]
    beyond_control = [item for item in plausible if not item["matches_control"]]
    if not beyond_control:
        return None, "candidate_matches_control" if plausible else "no_supported_candidate"
    if len(beyond_control) > 1:
        return None, "competing_candidates"
    candidate = beyond_control[0]
    if candidate["confidence"] != "high":
        return None, "insufficient_candidate_confidence"
    return candidate, None


def summarize(rows, stage, failure):
    measurements = [row for row in rows if row["status"] == "supported"]
    summary = {"status": "inconclusive", "attempts": len(rows), "supported": len(measurements),
               "median_witness_ms": None}
    if failure:
        return {**summary, "reason": failure}
    if stage == "control":
        if len(rows) == 3 and all(row["status"] == "control_examined" for row in rows):
            return {**summary, "status": "voice_control_ready"}
        return {**summary, "reason": "control_not_validated"}
    required = 3 if stage == "diagnostic" else 8
    expected = 3 if stage == "diagnostic" else 10
    if len(rows) != expected or len(measurements) < required:
        return {**summary, "reason": "insufficient_supported_trials"}
    values = [row["witness_interval_ms"] for row in measurements]
    center = median(values)
    mad = median(abs(value - center) for value in values)
    if mad > 8 or max(values) - min(values) > MAX_DIAGNOSTIC_SPREAD:
        return {**summary, "reason": "inconsistent_delays"}
    if stage == "diagnostic":
        return {**summary, "status": "voice_diagnostic_ready"}
    return {
        **summary, "status": "voice_witness_intervals",
        "median_witness_ms": center, "mad_ms": mad,
        "min_max_ms": [min(values), max(values)],
        "median_lag_range_ms": [median(row["lag_range_ms"][i] for row in measurements) for i in (0, 1)],
    }


def read_reference(path, stage, source, control_digest=None, *, require_ready=True):
    # Only bounded, versioned reports are accepted. Recompute readiness from
    # the written trials and bind the diagnostic to the exact control used.
    with Path(path).open("rb") as handle:
        payload = handle.read(MAX_REFERENCE_BYTES + 1)
    if len(payload) > MAX_REFERENCE_BYTES:
        raise ValueError("rapport vocal trop volumineux")
    try:
        report = json.loads(payload)
        rows = report["trials"]
        metadata = report["voice"]
        threshold = report["threshold_dbfs"]
        floor = report["ambient"]["p95_dbfs"]
        if (report["mode"] != "voice" or report["source"] != source or report["rate"] != PCM_RATE
                or metadata != {"version": REFERENCE_VERSION, "stage": stage,
                                "control_sha256": control_digest}
                or report["error"] is not None or report["ambient"]["status"] != "ok"
                or not -60 <= threshold <= -12 or not -120 <= floor <= 0
                or report["ambient"]["threshold_dbfs"] != threshold
                or len(rows) != 3):
            raise ValueError("rapport vocal incompatible ou incomplet")
        ready = summarize(rows, stage, None)["status"] == f"voice_{stage}_ready"
        if require_ready and not ready:
            raise ValueError("rapport vocal inconclusive")
        ranges = []
        if stage == "control":
            for row in rows:
                for item in row["candidates"]:
                    delay = item["delay_ms"]
                    low, high = item["lag_range_ms"]
                    if (not MIN_DELAY <= delay <= MAX_DELAY or not delay - 24 <= low <= delay <= high <= delay + 24
                            or item["confidence"] not in ("low", "moderate", "high")):
                        raise ValueError("délai hors plage dans le contrôle")
                    if item["confidence"] in ("moderate", "high"):
                        ranges.append((low, high))
        elif any(not MIN_DELAY <= row["witness_interval_ms"] <= MAX_DELAY for row in rows):
            raise ValueError("délai hors plage dans le diagnostic")
    except (KeyError, TypeError, ArithmeticError, RecursionError) as exc:
        raise ValueError("format de rapport vocal invalide") from exc
    return threshold, floor, ranges, sha256(payload).hexdigest(), ready
