"""Consent-scoped PipeWire taps for a BlackBox turn."""
from pathlib import Path
import re
from subprocess import SubprocessError
from tempfile import TemporaryDirectory
from threading import Event, Lock, Thread
from time import monotonic_ns

import numpy as np

from _kara_blackbox import GRAPH_TRACKS, digest
from _kara_paths import CACHE
from _kara_pipewire_latency import CaptureError, NATIVE, RATE, build_helper, open_graph
from _kara_voice_latency_witness import DEFAULT_SOURCE


_CACHE_LOCK = Lock()


def cached_helper():
    # Build the existing V5.1 tap helper outside GTK and the audio thread.
    # Publication uses a complete executable keyed by the native source.
    cache = CACHE / "kara/native"
    cache.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = cache / ("pipewire-witness-" + digest(NATIVE)[:24])
    with _CACHE_LOCK:
        if not target.is_file():
            with TemporaryDirectory(dir=cache, prefix="witness-") as temp:
                pending = Path(temp) / "witness"
                build_helper(pending)
                pending.replace(target)
    return target


class AcousticTurn:
    def __init__(self, recorder, session, context, sink, *, helper=None,
                 witness=DEFAULT_SOURCE, channel="FL"):
        self.recorder = recorder
        self.session = session
        self.context = dict(context)
        self.sink = sink
        self.witness = witness
        self.channel = channel
        self.helper = helper
        self.epoch = monotonic_ns()
        self.stop_flag = Event()
        self.gate = Lock()
        self.threads = []
        self.signature = session, self.context["turn"], sink

    def start(self):
        for track in GRAPH_TRACKS:
            self.report(track, "requested")
            thread = Thread(target=self.read, args=(track,), daemon=True, name=f"kara-acoustic-{track}")
            self.threads.append(thread)
            thread.start()

    def cancelled(self):
        return self.stop_flag.is_set() or self.recorder.voice_session() != self.session

    def stop(self):
        # Once stop returns, no worker can enqueue PCM for this aperture.
        # The native clients observe cancellation within one select interval.
        with self.gate:
            self.stop_flag.set()

    def alive(self):
        return any(thread.is_alive() for thread in self.threads)

    def report(self, track, state, **data):
        self.recorder.emit("acoustic", "tap", **self.context, recording_session=self.session,
                           track=track, state=state, epoch=self.epoch, **data)

    def read(self, track):
        reason = "capture_ended"
        stored, missing = 0, 0
        if track == "monitor" and not self.sink:
            self.report(track, "unavailable", reason="output_unidentified")
            return
        try:
            helper = self.helper or cached_helper()
            if self.cancelled():
                return
            with open_graph(helper, self.witness, self.sink or "-", self.channel,
                            track=track, cancelled=self.cancelled) as graph:
                while not self.cancelled():
                    graph.read_cycle()
                    channels = 2 if track == "monitor" else 1
                    relevant = 6 if track == "monitor" else 1
                    if graph.present & relevant != relevant:
                        missing += graph.cycle["quantum"]
                        continue
                    # Interleave the stereo planes without requantization.
                    samples = graph.pending[1:].T if track == "monitor" else graph.pending[0]
                    block = np.asarray(samples, dtype="<f4").tobytes(order="C")
                    with self.gate:
                        if self.cancelled():
                            break
                        if not stored:
                            self.report(track, "running", first_frame=graph.cycle["frame"],
                                        driver=graph.cycle["driver"])
                        self.recorder.pcm(track, RATE, channels, block, **self.context,
                                          clock={**graph.cycle, "epoch": self.epoch},
                                          recording_session=self.session)
                        stored += graph.cycle["quantum"]
        except CaptureError as error:
            symbolic = str(error)
            if symbolic != "capture_cancelled":
                reason = symbolic if re.fullmatch(r"[a-z_]{1,64}", symbolic) else "pipewire_capture_failed"
        except (OSError, ValueError, SubprocessError):
            reason = "pipewire_capture_failed"
        if reason == "capture_ended" and stored:
            self.report(track, "stopped", submitted_frames=stored, missing_frames=missing)
        else:
            self.report(track, "unavailable", reason=reason if reason != "capture_ended" else "no_capture_pcm",
                        submitted_frames=stored, missing_frames=missing)


class AcousticCapture:
    """Owned by the playing turn on Parole's main loop."""
    def __init__(self, recorder):
        self.recorder = recorder
        self.task = None

    def update(self, context=None, sink=None):
        session = self.recorder.voice_session() if context else None
        signature = (session, context["turn"], sink) if session else None
        if self.task:
            if self.task.signature == signature and not self.task.stop_flag.is_set():
                return
            self.task.stop()
            if self.task.alive():
                return
            self.task = None
        if signature:
            self.task = AcousticTurn(self.recorder, session, context, sink)
            self.task.start()
