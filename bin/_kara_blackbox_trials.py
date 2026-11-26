"""Local, versioned ground truth and deterministic incident overlays."""
from fcntl import LOCK_EX, flock
from math import isfinite
import json
import os
from random import Random
from uuid import uuid4

from _kara_blackbox import MARKERS, clean, events, now, private_file

LABELS = (*MARKERS, "word-correct", "word-omitted", "phrase-late")
SECOND = 1_000_000_000
LIMITS = {
    "drop_every": (0, 10000), "jitter_ms": (0, 1000), "delay_ms": (0, 10000),
    "delay_ramp_ms": (-1000, 1000), "drift_ppm": (-10000, 10000),
    "gain": (0, 16), "clip_peak": (.001, 1), "echo_gain": (0, 1),
    "echo_delay_ms": (0, 1000), "room_tail": (0, .9),
}


def annotate(root, path, session, cycle, since, until, name, note, previous):
    if name not in LABELS or not 0 <= len(note) <= 240:
        raise ValueError("Type d’annotation ou commentaire invalide.")
    if not since < until:
        raise ValueError("L’intervalle annoté doit être croissant.")
    if path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Enregistre les annotations dans un dossier privé distinct des captures.")
    with open(path, "a+", encoding="utf-8", opener=private_file) as stream:
        if os.fstat(stream.fileno()).st_mode & 0o077:
            raise ValueError("Le journal d’annotations doit être privé.")
        flock(stream, LOCK_EX)
        revision = 1
        if previous:
            stream.seek(0)
            while raw := stream.readline(65537):
                if len(raw) > 65536 or not raw.endswith("\n"):
                    raise ValueError("Journal d’annotations tronqué.")
                prior = json.loads(raw)
                if prior["id"] == previous and prior["session"] == session and prior["cycle"] == cycle:
                    revision = prior["revision"] + 1
            if revision == 1:
                raise ValueError("Version à remplacer absente de ce cycle.")
        row = {"id": uuid4().hex, "revision": revision, "supersedes": previous, "session": session,
               "cycle": cycle, "begin_ns": since, "end_ns": until, "label": name,
               "note": clean(note.strip()), "source": "human", "created": now()}
        stream.write(json.dumps(row, ensure_ascii=False, allow_nan=False, separators=(",", ":")) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return row


def simulation_options(options):
    if not isinstance(options, dict) or options.keys() - {*LIMITS, "cut", "reconnect"}:
        raise ValueError("Paramètres de simulation inconnus.")
    result = {"gain": 1, "clip_peak": 1, **options}
    for key, (low, high) in LIMITS.items():
        value = result.get(key, 0)
        if type(value) not in (int, float) or not isfinite(value) or not low <= value <= high:
            raise ValueError(f"Valeur de simulation invalide pour {key}.")
    if type(result.get("drop_every", 0)) is not int:
        raise ValueError("drop_every doit être entier.")
    if "cut" in result:
        gap = result["cut"]
        if (not isinstance(gap, list) or len(gap) != 2 or any(type(x) not in (int, float) for x in gap)
                or not 0 <= gap[0] < gap[1] <= 86400):
            raise ValueError("La coupure [début, fin] est exprimée en secondes relatives.")
    if "reconnect" in result and (type(result["reconnect"]) not in (int, float) or not 0 <= result["reconnect"] <= 86400):
        raise ValueError("La reconnexion est exprimée en secondes relatives.")
    return result


def packet_simulation(folder, cycle, options, seed, begin, end, limit):
    random = Random(seed)
    origin = count = delivered = 0
    new_epoch = None
    for row in events(folder / "events.jsonl"):
        stamp = row["monotonic_ns"]
        if end is not None and stamp > end:
            break
        data = row.get("data", {})
        if row.get("cycle") != cycle or row["event"] != "pcm" or data.get("track") != "wifi" or (
                begin is not None and stamp < begin):
            continue
        if count >= limit:
            break
        count += 1
        if count == 1:
            origin = stamp
        elapsed = (stamp - origin) / SECOND
        every = options.get("drop_every", 0)
        gap = options.get("cut")
        lost = bool(every and count % every == 0 or gap and gap[0] <= elapsed < gap[1])
        reconnect = "reconnect" in options and elapsed >= options["reconnect"]
        if reconnect and new_epoch is None:
            new_epoch = count
        delay = options.get("delay_ms", 0) + options.get("delay_ramp_ms", 0) * min(1., elapsed / 10)
        delay = max(0., delay + random.uniform(-1, 1) * options.get("jitter_ms", 0))
        delivered += not lost
        yield {"event_id": row["id"], "source_sequence": data["sequence"],
               "simulated_sequence": count - (new_epoch if reconnect else 1),
               "delivered": not lost, "delay_ms": round(delay, 6),
               "receive_ns": None if lost else stamp + round(delay * 1_000_000),
               "source_clock_ns": round((elapsed - (options["reconnect"] if reconnect else 0))
                                        * SECOND * (1 + options.get("drift_ppm", 0) / 1e6)),
               "connection_after_reconnect": reconnect}
    if not count:
        raise ValueError("Aucun paquet microphone V3 dans l’intervalle.")
    yield {"packets": count, "delivered": delivered, "lost": count - delivered}


def simulate_pcm(microphone, render, options, rate):
    import numpy as np

    signal = microphone * options["gain"]
    if echo := options.get("echo_gain"):
        reference = render.mean(axis=1)
        lag = round(options.get("echo_delay_ms", 0) * rate / 1000)
        reflection = options.get("room_tail", 0)
        for index in range(4):
            start = lag + index * round(.023 * rate)
            if start < len(signal):
                signal[start:] += echo * reflection ** index * reference[:len(signal) - start]
    return np.clip(signal, -options["clip_peak"], options["clip_peak"])
