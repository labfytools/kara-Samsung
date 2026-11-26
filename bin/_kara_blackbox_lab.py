"""Streaming analysis of the existing BlackBox journal."""
from collections import Counter
from dataclasses import dataclass, field

from _kara_blackbox import events

SECOND = 1_000_000_000
MAX_RANGES = 256
MIN_AEC_NS = 4 * SECOND
TRACKS = ("wifi", "monitor", "playback")
OBSERVATIONS = {
    "clipping": "crête saturée", "xrun": "sous-alimentation audio signalée",
    "capture-gap": "discontinuité signalée", "overflow": "file audio saturée",
    "tv-timeout": "client TV sans réponse", "transport-end": "connexion microphone terminée",
    "disconnect": "connexion fermée", "write-error": "écriture interrompue",
    "error": "erreur du composant",
    "tv-software-delay": "reçu logiciel TV tardif",
    "memory-pressure": "attente sous pression mémoire",
}


def frame_ns(frames, rate):
    return (frames * SECOND + rate // 2) // rate


@dataclass
class Span:
    identity: tuple
    first: int
    end: int
    earliest: int | None
    latest: int | None
    first_event: int
    last_event: int


@dataclass
class Track:
    ranges: list = field(default_factory=list)
    frames: int = 0
    breaks: int = 0

    def submit(self, identity, first, end, t0, t1, event, tolerance=0):
        previous = self.ranges[-1] if self.ranges else None
        if previous and previous.identity == identity and abs(previous.end - first) <= tolerance:
            previous.end, previous.latest, previous.last_event = end, t1, event
            return
        if previous:
            self.breaks += 1
        self.ranges.append(Span(identity, first, end, t0, t1, event, event))
        if len(self.ranges) > MAX_RANGES:
            # Keep the most recent ranges and disclose the truncation. We
            # never bridge the discarded evidence to claim continuity.
            del self.ranges[0]


@dataclass
class Cycle:
    tracks: dict = field(default_factory=lambda: {name: Track() for name in TRACKS})
    anchors: dict = field(default_factory=dict)
    first_anchor: dict = field(default_factory=dict)
    gaps: int = 0
    scored: int = 0
    score: dict | None = None
    turbo_windows: int = 0
    turbo_frames: dict = field(default_factory=dict)
    missing_turbo: int = 0
    recorded_turbo: int = 0

    def add_anchor(self, row):
        data = row["data"]
        key = data.get("connection"), data["rate"]
        anchor = (data["context_frame"], data["renderer_frame_min_ns"],
                  data["renderer_frame_max_ns"], row["id"])
        self.first_anchor.setdefault(key, anchor)
        history = self.anchors.setdefault(key, [])
        history.append(anchor)
        if len(history) > MAX_RANGES:
            del history[0]

    def add_pcm(self, row):
        data = row["data"]
        name, frames, rate = data["track"], data["frames"], data["rate"]
        if name == "turbo":
            call = data["decode"]
            key = call["window"], call["call"]
            self.turbo_frames[key] = self.turbo_frames.get(key, 0) + frames
        if name in self.tracks:
            self.tracks[name].frames += frames
        if name == "monitor":
            clock = data["clock"]
            t0 = clock["monotonic_ns"]
            first = clock["frame"]
            group = (name, clock["driver"], clock["epoch"], clock["xrun"], rate)
            self.tracks[name].submit(group, first, first + frames, t0, t0 + frame_ns(frames, rate), row["id"])
        elif name == "wifi":
            end = data.get("packet_timing", {}).get("frame_end")
            connection = data.get("connection")
            if connection and type(end) is int and end >= frames:
                self.tracks[name].submit((name, connection, rate), end - frames, end, None, None, row["id"])
        elif name == "playback":
            timing = data["playback_timing"]
            if {"running_ns", "pipeline_base_ns", "pipeline_clock_ns", "host_before_ns", "host_after_ns"} <= timing.keys():
                t0 = timing["running_ns"] + timing["pipeline_base_ns"] - timing["pipeline_clock_ns"]
                first = timing["running_ns"]
                duration = frame_ns(frames, rate)
                group = (name, timing["pipeline_base_ns"], rate, data["channels"], data["sample_format"])
                self.tracks[name].submit(group, first, first + duration, t0 + timing["host_after_ns"],
                                         t0 + duration + timing["host_before_ns"], row["id"], tolerance=1)

    def score_decision(self, data):
        self.scored += 1
        for call in (data.get("decoder") or {}).get("decodes", ()):
            if not call.get("input_sha256"):
                continue
            present = self.turbo_frames.pop((data.get("window"), call["call"]), 0)
            if present == call["input_frames"]:
                self.recorded_turbo += 1
            else:
                self.missing_turbo += 1

    def describe(self):
        pairs = {}
        drift = []
        for mic in self.tracks["wifi"].ranges:
            key = mic.identity[1:]
            anchors = sorted({anchor for anchor in
                              [self.first_anchor.get(key), *self.anchors.get(key, [])]
                              if anchor and mic.first <= anchor[0] <= mic.end})
            if len(anchors) < 2 or anchors[0][0] == anchors[-1][0]:
                continue
            first, last = anchors[0], anchors[-1]
            expected = frame_ns(last[0] - first[0], key[1])
            low = last[1] - first[2]
            high = last[2] - first[1]
            if expected and high > 0:
                drift.append([round((low / expected - 1) * 1_000_000),
                              round((high / expected - 1) * 1_000_000)])
            for reference in ("monitor", "playback"):
                for span in self.tracks[reference].ranges:
                    begin, end = max(first[2], span.earliest), min(last[1], span.latest)
                    if end <= begin or end - begin <= pairs.get(reference, {}).get("duration_ns", 0):
                        continue
                    pairs[reference] = {"begin_ns": begin, "end_ns": end,
                                        "duration_ns": end - begin, "mobile_anchors": [first[3], last[3]],
                                        "mic_events": [mic.first_event, mic.last_event],
                                        "reference_events": [span.first_event, span.last_event]}
        return {"observed_tracks": {name: {"frames": track.frames, "breaks": track.breaks,
                                           "retained_ranges": len(track.ranges)}
                                    for name, track in self.tracks.items()},
                "overlap": pairs, "aec_state": (
                    "software_overlap" if any(row["duration_ns"] >= MIN_AEC_NS for row in pairs.values())
                    else "insufficient_overlap" if pairs else "missing_timing_or_tracks"),
                "physical_alignment_bound_ns": None, "renderer_drift_ppm_intervals": drift,
                "rx_gaps_reported": self.gaps, "turbo_windows": self.turbo_windows,
                "turbo_capture": {"calls_with_all_frames": self.recorded_turbo,
                                  "missing_calls": self.missing_turbo, "unconfirmed_calls": len(self.turbo_frames)},
                "score_decisions": self.scored, "score": self.score}


def analyze(folder):
    cycles, occurrences, transport = {}, Counter(), Counter()
    clients, metrics = {}, {}
    evidence = []
    first, last = None, None
    for row in events(folder / "events.jsonl"):
        stamp = row["monotonic_ns"]
        first = stamp if first is None else first
        last = stamp
        event, data = row["event"], row.get("data", {})
        key = row.get("cycle")
        if key and key not in cycles:
            cycles[key] = Cycle()
        cycle = cycles.get(key)
        if cycle and row["component"] == "audio" and event == "pcm":
            cycle.add_pcm(row)
        elif cycle and row["component"] == "clock" and event == "mobile-anchor":
            cycle.add_anchor(row)
        elif cycle and event == "window" and row["component"] == "turbo":
            cycle.turbo_windows += 1
        elif cycle and event == "line-decision" and row["component"] == "scoring":
            cycle.score_decision(data)
        elif cycle and event == "turn-end" and row["component"] == "party":
            cycle.score = data.get("summary")
        if event == "browser-capture" and data.get("connection"):
            client = clients.setdefault(data["connection"], {})
            for key in ("sent", "dropped", "buffered_bytes_max"):
                value = data.get("client", {}).get(key)
                if type(value) is int and value >= 0:
                    client[key] = max(client.get(key, 0), value)
        if row["component"] == "system" and event == "sample":
            for key in ("cpu_temp_c", "gpu_temp_c", "gpu_percent"):
                values = data.get(key)
                if values:
                    metrics[key + "_peak"] = max(metrics.get(key + "_peak", 0), max(values))
            memory = data.get("memory_kib", {}).get("MemAvailable")
            if type(memory) is int:
                metrics["memory_kib_min"] = min(memory, metrics.get("memory_kib_min", memory))
            psi = data.get("memory_pressure", {}).get("some", 0)
            metrics["memory_psi_some_avg10_peak"] = max(psi, metrics.get("memory_psi_some_avg10_peak", 0))
            if psi >= 1:
                event = "memory-pressure"
        elif event == "rendered" and data.get("software_observation_ns", 0) > SECOND:
            event = "tv-software-delay"
        if event == "transport-end" and row["component"] == "audio":
            transport.update({key: data[key] for key in ("received", "missing", "duplicates", "reordered", "invalid")})
            if cycle:
                cycle.gaps += data["missing"]
        elif event in OBSERVATIONS:
            occurrences[event] += 1
            if len(evidence) < 1000:
                evidence.append({"event_id": row["id"], "cycle": row.get("cycle"), "observed": event,
                                 "description": OBSERVATIONS[event], "elapsed_ns": stamp - first})
    return {"version": 1, "duration_ns": last - first if last is not None else 0,
            "cycles": {key: cycle.describe() for key, cycle in cycles.items()},
            "transport": dict(transport), "browser_counters": {
                "sent_reported": sum(item.get("sent", 0) for item in clients.values()),
                "dropped_reported": sum(item.get("dropped", 0) for item in clients.values()),
                "max_buffered_bytes": max((item.get("buffered_bytes_max", 0) for item in clients.values()), default=0),
            }, "resources": metrics, "counts": dict(occurrences), "evidence": evidence,
            "physical_alignment_bound_ns": None}


def redacted(report):
    # An export is an allowlist of aggregate numbers. Session identifiers,
    # host clocks, lyrics, transcripts, configuration and evidence stay local.
    return {"version": report["version"], "duration_ns": report["duration_ns"],
            "cycles": len(report["cycles"]),
            "aec_states": dict(Counter(item["aec_state"] for item in report["cycles"].values())),
            "score_decisions": sum(item["score_decisions"] for item in report["cycles"].values()),
            "turbo_windows": sum(item["turbo_windows"] for item in report["cycles"].values()),
            "transport": report["transport"], "counts": report["counts"],
            "physical_alignment_bound_ns": None}
