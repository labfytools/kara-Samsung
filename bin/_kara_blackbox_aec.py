"""Run the existing offline AEC engines on a bounded V3 interval."""
from math import ceil, floor
from pathlib import Path
from tempfile import TemporaryDirectory

import numpy as np
from scipy.signal import correlate

from _kara_aec_prototype import FRAME, RATE, Speex, WebRtc, build_webrtc, find_delay, process, with_engine
from _kara_blackbox import events, private_file, wave_layout
from _kara_blackbox_lab import MIN_AEC_NS, SECOND

MAX_SECONDS = 12


def intersecting_rows(folder, cycle, wanted):
    return {row["id"]: row for row in events(folder / "events.jsonl")
            if row.get("cycle") == cycle and row["id"] in wanted}


def typed_block(folder, data, offset, count):
    path = folder / data["file"]
    rate, channels, frames, width, header = wave_layout(path)
    first = data["sample"] + offset
    if not 0 <= first < first + count <= frames or channels != data["channels"] or rate != data["rate"]:
        raise ValueError("Bloc AEC hors plage.")
    with open(path, "rb", opener=private_file) as stream:
        stream.seek(header + first * channels * width)
        raw = stream.read(count * channels * width)
    if len(raw) != count * channels * width:
        raise ValueError("Bloc AEC tronqué.")
    samples = np.frombuffer(raw, dtype="<f4" if width == 4 else "<i2").reshape(count, channels)
    return samples if width == 4 else samples.astype(np.float32) / 32768


def reconstruct(folder, cycle, track, ids, frame_start, step, count, index):
    # Read only enough samples for the requested grid. A discontinuity inside
    # this range aborts the experiment. No held sample or zero fills a hole.
    first = floor(frame_start)
    last = ceil(frame_start + (count - 1) * step) + 1
    chunks = []
    cursor = first
    for row in events(folder / "events.jsonl"):
        if row["id"] > ids[1]:
            break
        if (row["id"] < ids[0] or row.get("cycle") != cycle or row["event"] != "pcm"
                or row["data"]["track"] != track):
            continue
        data = row["data"]
        start = index(data)
        low, high = max(start, first), min(start + data["frames"], last)
        if high <= low:
            continue
        if low != cursor:
            raise ValueError("Discontinuité AEC dans l’intervalle sélectionné.")
        chunks.append(typed_block(folder, data, low - start, high - low))
        cursor = high
        if cursor == last:
            break
    if cursor != last:
        raise ValueError("L’intervalle AEC dépasse les échantillons disponibles.")
    source = np.concatenate(chunks)
    if not np.isfinite(source).all():
        raise ValueError("Signal PCM non fini.")
    local_grid = (frame_start - first) + np.arange(count, dtype=np.float64) * step
    input_grid = np.arange(source.shape[0], dtype=np.int64)
    return np.column_stack([np.interp(local_grid, input_grid, channel) for channel in source.T]).astype(np.float32)


def input_pair(folder, report, cycle, reference, seconds=MAX_SECONDS):
    pair = report["cycles"][cycle]["overlap"].get(reference)
    if not pair or pair["duration_ns"] < MIN_AEC_NS:
        raise ValueError("Aucune fenêtre logicielle commune continue d’au moins quatre secondes.")
    ids = set(pair["mobile_anchors"] + pair["reference_events"][:1])
    rows = intersecting_rows(folder, cycle, ids)
    a, b = [rows[key]["data"] for key in pair["mobile_anchors"]]

    def mid(anchor):
        return (anchor["renderer_frame_min_ns"] + anchor["renderer_frame_max_ns"]) / 2

    first_host, last_host = mid(a), mid(b)
    if last_host <= first_host:
        raise ValueError("Les ancres microphone ne sont pas monotones.")
    frames_per_ns = (b["context_frame"] - a["context_frame"]) / (last_host - first_host)
    nominal = a["rate"] / SECOND
    if not .98 * nominal <= frames_per_ns <= 1.02 * nominal:
        raise ValueError("Cadence estimée incompatible avec ce renderer.")
    first = pair["begin_ns"]
    count = min(seconds * RATE, pair["duration_ns"] * RATE // SECOND)
    count -= count % FRAME
    mic = reconstruct(folder, cycle, "wifi", pair["mic_events"],
                      a["context_frame"] + (first - first_host) * frames_per_ns,
                      frames_per_ns * SECOND / RATE, count,
                      lambda data: data["packet_timing"]["frame_end"] - data["frames"]).ravel()
    start = rows[pair["reference_events"][0]]["data"]
    rate = start["rate"]
    if reference == "monitor":
        origin = start["clock"]["monotonic_ns"]
        offset = start["clock"]["frame"]

        def index(data):
            return data["clock"]["frame"]
    else:
        clock = start["playback_timing"]
        origin = (clock["host_before_ns"] + clock["host_after_ns"]) / 2 + clock["running_ns"] + (
            clock["pipeline_base_ns"] - clock["pipeline_clock_ns"])
        offset = 0

        def index(data):
            return round((data["playback_timing"]["running_ns"] - clock["running_ns"]) * rate / SECOND)
    render = reconstruct(folder, cycle, reference, pair["reference_events"],
                         offset + (first - origin) * rate / SECOND, rate / RATE, count, index)
    return render, mic


def rms(signal):
    return float(np.sqrt(np.mean(signal.astype(np.float64) ** 2)))


def compare(render, mic, candidate_delay=None):
    ref = render.mean(axis=1, dtype=np.float64)
    if rms(ref) < .0001 or rms(mic) < .0001:
        raise ValueError("Niveau insuffisant pour comparer le couplage acoustique.")
    delay = candidate_delay
    if delay is None:
        cross = correlate(mic, ref, mode="full", method="fft")
        offset = int(np.argmax(np.abs(cross[len(ref) - 1:len(ref) - 1 + RATE // 2])))
        delay = round(offset * 1000 / RATE)
    lag = round(delay * RATE / 1000)

    def stats(signal):
        value = signal[lag:]
        base = ref[:len(value)]
        scale = np.linalg.norm(value) * np.linalg.norm(base)
        return {"rms": rms(signal), "reference_correlation": float(np.dot(value, base) / scale) if scale else None}

    variants = {"unchanged": stats(mic)}
    with TemporaryDirectory(prefix="kara-blackbox-aec-") as draft:
        library = Path(draft) / "webrtc.so"
        build_webrtc(library)
        for name, factory in (("aec3", lambda n: WebRtc(library, n)), ("speex", Speex)):
            signal, _, times = with_engine(factory, render.shape[1], process, render, mic, delay)
            algorithm_delay = find_delay(factory)
            variants[name] = {**stats(signal[algorithm_delay:]),
                              "algorithm_delay_ms": algorithm_delay * 1000 / RATE,
                              "frame_p95_us": float(np.percentile(times, 95))}
    return {"frames": len(mic), "rate": RATE, "delay_candidate_ms": delay,
            "variants": variants, "physical_alignment_bound_ns": None,
            "ground_truth_available": False, "method": "software_clock_linear_alignment"}
