#!/usr/bin/env python3
"""Offline paired-render AEC experiment. Opens no microphone, socket or player."""
import argparse
import ctypes as ct
import ctypes.util
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import shlex
import subprocess
import tempfile
import time

import numpy as np
from scipy.signal import butter, correlate, sosfilt

RATE = 48000
FRAME = RATE // 100
I16 = np.ctypeslib.ndpointer(dtype=np.int16, ndim=1, flags=("C_CONTIGUOUS", "ALIGNED"))
ROOM = ((0, .53), (13.7, -.20), (37.4, .12), (71.2, .07))
SPEEX_ECHO_SET_SAMPLING_RATE = 24


def build_webrtc(destination):
    source = Path(__file__).resolve().parents[1] / "native/src/_kara_aec_webrtc.cc"
    flags = subprocess.check_output(
        ["pkg-config", "--cflags", "--libs", "webrtc-audio-processing-1"],
        text=True, timeout=10)
    subprocess.run(["c++", "-std=c++17", "-O2", "-shared", "-fPIC", str(source),
                    "-o", str(destination), *shlex.split(flags)], check=True, timeout=60)


class WebRtc:
    def __init__(self, library, render_channels=1):
        self.lib = ct.CDLL(library)
        self.lib.kara_aec_create.argtypes = [ct.c_int]
        self.lib.kara_aec_create.restype = ct.c_void_p
        self.lib.kara_aec_destroy.argtypes = [ct.c_void_p]
        self.lib.kara_aec_destroy.restype = None
        self.lib.kara_aec_step.argtypes = [ct.c_void_p, I16, ct.c_size_t,
                                          I16, ct.c_size_t, I16, ct.c_size_t, ct.c_int]
        self.lib.kara_aec_step.restype = ct.c_int
        self.channels = render_channels
        self.state = self.lib.kara_aec_create(render_channels)
        if not self.state:
            raise RuntimeError("WebRTC AEC3 initialization failed")

    def close(self):
        if self.state:
            self.lib.kara_aec_destroy(self.state)
            self.state = None

    def step(self, render, capture, output, delay_ms):
        error = self.lib.kara_aec_step(self.state, render, render.size, capture, capture.size,
                                       output, output.size, delay_ms)
        if error:
            raise RuntimeError(f"WebRTC returned {error} for a 10 ms frame")


class Speex:
    def __init__(self, render_channels=1, tail_ms=200):
        library = ctypes.util.find_library("speexdsp")
        if not library:
            raise RuntimeError("SpeexDSP library unavailable")
        self.lib = ct.CDLL(library)
        self.lib.speex_echo_state_init_mc.argtypes = [ct.c_int] * 4
        self.lib.speex_echo_state_init_mc.restype = ct.c_void_p
        self.lib.speex_echo_ctl.argtypes = [ct.c_void_p, ct.c_int, ct.c_void_p]
        self.lib.speex_echo_ctl.restype = ct.c_int
        self.lib.speex_echo_cancellation.argtypes = [ct.c_void_p, I16, I16, I16]
        self.lib.speex_echo_cancellation.restype = None
        self.lib.speex_echo_state_destroy.argtypes = [ct.c_void_p]
        self.lib.speex_echo_state_destroy.restype = None
        self.channels = render_channels
        if render_channels not in (1, 2) or not 100 <= tail_ms <= 400:
            raise ValueError("unsupported SpeexDSP configuration")
        # The adaptive tail includes room response and residual alignment
        # error. No Speex preprocessor, VAD or gain control is used.
        self.state = self.lib.speex_echo_state_init_mc(
            FRAME, RATE * tail_ms // 1000, 1, render_channels)
        if not self.state:
            raise RuntimeError("SpeexDSP initialization failed")
        sample_rate = ct.c_int(RATE)
        if self.lib.speex_echo_ctl(self.state, SPEEX_ECHO_SET_SAMPLING_RATE,
                                   ct.byref(sample_rate)):
            self.close()
            raise RuntimeError("SpeexDSP rejected the sample rate")

    def close(self):
        if self.state:
            self.lib.speex_echo_state_destroy(self.state)
            self.state = None

    def step(self, render, capture, output, delay_ms):
        if (not self.state or render.size != FRAME * self.channels
                or capture.size != FRAME or output.size != FRAME):
            raise ValueError("invalid SpeexDSP frame")
        self.lib.speex_echo_cancellation(self.state, capture, render, output)


def pcm(signal):
    return np.rint(np.clip(signal, -1, 32767 / 32768) * 32768).astype(np.int16)


def float_pcm(signal):
    return signal.astype(np.float64) / 32768


def power(signal):
    return max(float(np.mean(signal ** 2)), 1e-15)


def db(ratio):
    return float(10 * np.log10(max(ratio, 1e-15)))


def normalize(signal, rms):
    return signal * (rms / np.sqrt(power(signal)))


def noise(seed, size, lo=140, hi=6100):
    return sosfilt(butter(3, (lo, hi), btype="bandpass", fs=RATE, output="sos"),
                   np.random.default_rng(seed).standard_normal(size))


def envelope(t, start, end, fade_in, fade_out):
    return np.clip(np.minimum((t - start) / fade_in, (end - t) / fade_out), 0, 1)


def vocal(t, pitch, syllables, seed):
    # Harmonics, independent breath and amplitude envelopes exercise transients
    # and vowels without loading a recorded utterance or reusing a render seed.
    contour = pitch + 6 * np.sin(2 * np.pi * 5.1 * t) + 2 * np.sin(2 * np.pi * .7 * t)
    phase = np.cumsum(contour) * (2 * np.pi / RATE)
    vowel = sum(np.sin(h * phase + .36 * h) / h for h in range(1, 13))
    energy = sum(amplitude * envelope(t, start, end, .023, .12)
                 for start, end, amplitude in syllables)
    return energy * (.74 * vowel + .08 * noise(seed, t.size, lo=900))


def at_ms(milliseconds):
    return np.asarray(milliseconds) * (RATE / 1000)


def area(start, end):
    return slice(round(start * RATE), round(end * RATE))


@dataclass(frozen=True)
class Case:
    name: str
    delay_ms: float
    coarse_ms: float
    drift_ppm: float = 0
    step_ms: float = 0
    oracle: bool = False
    stereo: bool = False
    voice_only_reference: bool = False


CASES = (
    Case("fixed", 130, 105),
    Case("drift-180ppm", 130, 105, drift_ppm=180),
    Case("drift-oracle", 130, 105, drift_ppm=180, oracle=True),
    Case("variable-360ms", 360, 330, drift_ppm=-180, step_ms=27),
    Case("variable-oracle", 360, 330, drift_ppm=-180, step_ms=27, oracle=True),
    Case("stereo-mix", 130, 105, stereo=True),
)
VOICE_ONLY_REFERENCE = Case("kara-only", 130, 105, voice_only_reference=True)


def open_loop(case):
    idx = np.arange(RATE * 12, dtype=np.float64)
    t = idx / RATE
    # A voiced playback and an independent band-limited accompaniment.
    far = vocal(t, 193, [(i, i + .56, .15) for i in np.arange(0, 12, .64)], 44)
    music = normalize(noise(20, idx.size, 115, 4800), .034) * (.5 + .5 * np.sin(2 * np.pi * 1.8 * t)**2)
    left = far + music
    right = .34 * far + normalize(noise(21, idx.size, 210, 4500), .062)
    render = np.column_stack((left, right)) if case.stereo else left[:, None]
    if case.voice_only_reference:
        render = far[:, None]
    dry = vocal(t, 232, [(5.0, 5.33, .038), (5.48, 5.95, .115),
                         (6.1, 6.85, .078), (7.25, 10.7, .15)], 58)
    mic_time = idx * (1 + case.drift_ppm * 1e-6)
    delay = float(case.delay_ms) + np.where(t >= 5, case.step_ms, 0.0)
    if case.step_ms:
        delay += 2.5 * np.sin(2 * np.pi * .73 * t)
    echo = sum(weight * np.interp(mic_time - at_ms(delay + tap), idx, left, left=0, right=0)
               for tap, weight in ROOM)
    if case.stereo:
        echo += .39 * np.interp(mic_time - at_ms(delay + 4.8), idx, right, left=0, right=0)
        echo -= .17 * np.interp(mic_time - at_ms(delay + 18), idx, right, left=0, right=0)
    # Render is sampled on the capture grid using either a fixed coarse
    # offset or the model's true clock and bulk delay (upper bound).
    ref_time = mic_time - at_ms(delay - 25) if case.oracle else idx - at_ms(case.coarse_ms)
    reference = np.column_stack([np.interp(ref_time, idx, item, left=0, right=0)
                                 for item in render.T])
    return dry, echo, reference


def process(engine, render, capture, delay_ms):
    reference = pcm(render).reshape(-1, render.shape[1] * FRAME)
    source = pcm(capture).reshape(-1, FRAME)
    output = np.empty_like(source)
    cpu = []
    wall = []
    for far, near, out in zip(reference, source, output, strict=True):
        cpu_start = time.process_time_ns()
        wall_start = time.perf_counter_ns()
        engine.step(far, near, out, delay_ms)
        wall.append((time.perf_counter_ns() - wall_start) / 1000)
        cpu.append((time.process_time_ns() - cpu_start) / 1000)
    return float_pcm(output.ravel()), cpu, wall


def with_engine(factory, channels, callback, *args):
    engine = factory(channels)
    try:
        return callback(engine, *args)
    finally:
        engine.close()


def find_delay(factory):
    probe = normalize(noise(31, RATE * 2), .09)
    measured, _, _ = with_engine(factory, 1, process, np.zeros((probe.size, 1)), probe, 0)
    correlation = correlate(measured, probe, method="fft")
    mid = probe.size - 1
    return int(np.argmax(np.abs(correlation[mid:mid + FRAME * 4])))


def aligned(signal, delay):
    return np.pad(signal[delay:], (0, delay))


def voice_metrics(clean, output):
    scale = float(np.dot(output, clean) / np.dot(clean, clean))
    projected = scale * clean
    return {"voice_gain_db": db(max(scale, 0) ** 2),
            "voice_si_sdr_db": db(power(projected) / power(output - projected))}


def inspect(case, factory, delay):
    dry, echo, render = open_loop(case)
    capture = dry + echo
    result, cpu, wall = with_engine(factory, render.shape[1], process, render, capture,
                                    25 if case.oracle else round(case.delay_ms - case.coarse_ms))
    result = aligned(result, delay)
    quiet = area(3.1, 4.6)
    late_quiet = area(10.95, 11.75)
    voice = {name: voice_metrics(dry[window], result[window]) for name, window in
             [("weak", area(5.04, 5.28)), ("speech", area(5.47, 6.85)),
              ("singing", area(7.31, 10.6)), ("tail", area(10.58, 10.69))]}
    baseline = {name: voice_metrics(dry[window], capture[window]) for name, window in
                [("speech", area(5.47, 6.85)), ("singing", area(7.31, 10.6))]}
    return {
        "echo_reduction_db": db(power(capture[quiet]) / power(result[quiet])),
        "late_echo_reduction_db": db(power(capture[late_quiet]) / power(result[late_quiet])),
        "voice": voice, "baseline": baseline,
        "cpu_p95_us": float(np.percentile(cpu, 95)),
        "wall_p95_us": float(np.percentile(wall, 95)),
        "wall_over_10ms": sum(value >= 10000 for value in wall),
        "frames_processed": len(cpu),
    }


def inspect_dry(factory, delay):
    t = np.arange(RATE * 4) / RATE
    dry = vocal(t, 232, [(0.25, 0.6, .038), (.8, 3.65, .15)], 58)
    measured, _, _ = with_engine(factory, 1, process, np.zeros((dry.size, 1)), dry, 0)
    singing = area(.9, 3.55)
    return voice_metrics(dry[singing], aligned(measured, delay)[singing])


def self_return(engine, dry, delay_ms=155):
    # Causal closed loop: the reference is past PCM of the processed vocal
    # return. There is no independent render track to make the task easier.
    n = np.arange(dry.size, dtype=np.float64)
    playback = np.zeros_like(dry)
    effect = np.zeros_like(dry)
    out = np.empty(FRAME, dtype=np.int16)
    for start in range(0, n.size, FRAME):
        end = start + FRAME
        timeline = n[start:end]
        echo = sum(weight * np.interp(timeline - at_ms(delay_ms + tap), n, playback, left=0)
                   for tap, weight in ROOM)
        capture = pcm(dry[start:end] + echo)
        reference = np.interp(timeline - at_ms(delay_ms - 25), n, playback, left=0)
        if engine is None:
            out[:] = capture
        else:
            engine.step(pcm(reference), capture, out, 25)
        effect[start:end] = float_pcm(out)
        playback[start:end] = .75 * .82 * np.tanh(effect[start:end] * 2)
    return effect, playback


def inspect_self_return(factory, delay):
    t = np.arange(RATE * 7) / RATE
    dry = vocal(t, 216, [(0.4, .78, .16), (.95, 1.34, .10), (1.5, 1.85, .12),
                         (3.05, 5.5, .17)], 99)
    raw, raw_play = self_return(None, dry)
    output, play = with_engine(factory, 1, self_return, dry)
    output, play = aligned(output, delay), aligned(play, delay)
    tail = area(1.94, 2.85)
    voice = area(3.13, 5.4)
    return {
        "post_phrase_return_reduction_db": db(power(raw_play[tail]) / power(play[tail])),
        "singing": voice_metrics(dry[voice], output[voice]),
        "baseline_singing": voice_metrics(dry[voice], raw[voice]),
    }


def qualification(report):
    """Conservative acceptance thresholds for integration, not hardware claims."""
    marks = {}
    for name, case in report["cases"].items():
        voice = case["voice"]
        marks[name] = (
            case["echo_reduction_db"] >= 10
            and case["late_echo_reduction_db"] >= 10
            and all(-1.5 <= v["voice_gain_db"] <= 1.5 and v["voice_si_sdr_db"] >= 15
                    for v in voice.values())
            and case["wall_over_10ms"] == 0
        )
    closed = report["self_return"]
    marks["self_return"] = (
        closed["post_phrase_return_reduction_db"] >= 10
        and -1.5 <= closed["singing"]["voice_gain_db"] <= 1.5
        and closed["singing"]["voice_si_sdr_db"] >= 15
    )
    return marks


def evaluate(factory):
    delay = find_delay(factory)
    report = {"processing_delay_samples": delay, "processing_delay_ms": 1000 * delay / RATE,
              "cases": {case.name: inspect(case, factory, delay) for case in CASES},
              "self_return": inspect_self_return(factory, delay),
              "controls": {"near_only": inspect_dry(factory, delay),
                           "kara_only": inspect(VOICE_ONLY_REFERENCE, factory, delay)}}
    report["qualifies"] = qualification(report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("both", "webrtc", "speex"), default="both")
    parser.add_argument("--speex-tail-ms", type=int, choices=(120, 200), default=200)
    args = parser.parse_args()
    report = {"sample_rate": RATE, "frame_samples": FRAME,
              "fixtures": [asdict(case) for case in CASES], "engines": {}}
    with tempfile.TemporaryDirectory(prefix="kara-aec-") as temp:
        if args.engine in ("both", "webrtc"):
            artifact = Path(temp) / "kara-aec.so"
            build_webrtc(artifact)
            report["engines"]["webrtc"] = evaluate(lambda channels: WebRtc(artifact, channels))
        if args.engine in ("both", "speex"):
            report["engines"]["speex"] = evaluate(
                lambda channels: Speex(channels, args.speex_tail_ms))
            report["engines"]["speex"]["tail_ms"] = args.speex_tail_ms
    print(json.dumps(report, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
