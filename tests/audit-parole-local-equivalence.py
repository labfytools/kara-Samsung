#!/usr/bin/env python3
"""Échantillonne les pistes sans référence et vérifie les rapprochements localement."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from contextlib import closing
from hashlib import sha256
import json
from pathlib import Path
import runpy
import sqlite3
from subprocess import SubprocessError
import sys
from time import monotonic

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_audio_identity import pcm_digest
from _kara_prepare import file_token
from _kara_turbo import digest
from _parole_fingerprint import read as fingerprint, similarity
from _parole_index import document, key
from _parole_local_reference import candidate

FORMATS = {"lyrics": ("Strawberry", "LRC"), "lrc": ("local", "externe", "généré")}
SAMPLE_PER_GROUP = 6
RATE = 16_000
RADIUS = 2.5
WINDOW = 4.


def pairs(db):
    by_credit = defaultdict(list)
    for row in db.execute("SELECT id,artist,title,duration,edition,lyrics,lrc FROM songs WHERE duration>0 ORDER BY id"):
        by_credit[key(row["artist"]), key(row["title"])].append(row)
    counts, targets, samples = Counter(), defaultdict(set), defaultdict(list)
    for editions in by_credit.values():
        best = {}
        for recipient in editions:
            for format_, states in FORMATS.items():
                if recipient[format_] != "absent":
                    continue
                for donor in editions:
                    if donor["edition"] == recipient["edition"] or donor[format_] not in states:
                        continue
                    delta = abs(donor["duration"] - recipient["duration"])
                    # Catalogue names and the duration admit a comparison.
                    # They never establish the recording or its clock.
                    cohort = "proche" if delta <= 2 else "écart"
                    bucket = format_, cohort
                    counts[bucket] += 1
                    targets[bucket].add(recipient["id"])
                    origin = f"omega-ii-local-v1:{donor['id']}:{recipient['id']}".encode()
                    rank = sha256(origin).digest()
                    pair = rank, donor["id"], recipient["id"], delta
                    if bucket not in best or pair < best[bucket]:
                        best[bucket] = pair
        for bucket, candidate_pair in best.items():
            # Keep one ranked pair per credit and six per cohort. Large
            # compilations never retain the whole cartesian product.
            current_sample = samples[bucket]
            current_sample.append(candidate_pair)
            current_sample.sort()
            del current_sample[SAMPLE_PER_GROUP:]
    return counts, targets, samples


def current(root, row):
    path = Path(row["path"])
    if (not path.is_relative_to(root) or path.resolve(strict=True) != path or not path.is_file()
            or document(file_token(path)) != row["token"]):
        raise ValueError("Piste sortie de l’instantané")
    return path


def estimate_shift(reference, search, radius):
    import numpy as np
    from scipy.signal import correlate

    if not len(reference) or len(search) < len(reference):
        return None
    # Normalize on each candidate interval so amplitude differences and
    # the search-window silence do not favor a larger denominator.
    energy = np.r_[0., np.cumsum(search * search)]
    energies = energy[len(reference):] - energy[:-len(reference)]
    norm = np.sqrt(np.maximum(energies, 0) * np.dot(reference, reference))
    cross = np.abs(correlate(search, reference, mode="valid", method="fft"))
    score = np.divide(cross, norm, out=np.zeros_like(cross), where=norm > .001)
    index = int(score.argmax())
    peak = float(min(1., score[index]))
    # A repeated motif giving an almost equal peak away from the candidate
    # is evidence of ambiguity, even if both correlations are high.
    margin = RATE // 20
    score[max(0, index - margin):index + margin + 1] = 0
    runner_up = float(min(1., score.max()))
    return {"offset_ms": (index - radius) * 1000 / RATE, "correlation": peak,
            "runner_up": runner_up, "usable": peak >= .85 and peak - runner_up >= .07}


def temporal_alignment(clip, source, target, duration):
    import numpy as np
    from scipy.signal import butter, sosfilt

    if duration < 25:
        return None
    filters = butter(2, (120, 3500), fs=RATE, btype="bandpass", output="sos")

    def signal(path, start, end):
        raw = np.frombuffer(clip(path, start, end), dtype="<i2").astype(np.float64) / 32768
        return sosfilt(filters, raw)

    windows = []
    for proportion in (.2, .5, .8):
        first = round((proportion * duration - WINDOW / 2) * RATE) / RATE
        reference = signal(source, first, first + WINDOW)
        search = signal(target, first - RADIUS, first + WINDOW + RADIUS)
        found = estimate_shift(reference, search, round(RADIUS * RATE))
        if found:
            windows.append(dict(found, position=proportion * duration))
    if len(windows) != 3 or not all(result["usable"] for result in windows):
        return {"windows": windows, "coherent": False}
    scales = [item["offset_ms"] for item in windows]
    spread = max(scales) - min(scales)
    return {"windows": windows, "coherent": spread <= 50, "spread_ms": spread,
            "within_50_ms": max(map(abs, scales)) <= 50}


def compare(root, donor, recipient, format_, receipts, tool, clip=None):
    start = monotonic()
    source, target = current(root, donor), current(root, recipient)
    same_file = donor["bytes"] == recipient["bytes"] and digest(source) == digest(target)
    same_pcm = pcm_digest(source) == pcm_digest(target)
    source_fingerprint, target_fingerprint = fingerprint(source, tool=tool), fingerprint(target, tool=tool)
    acoustic = similarity(source_fingerprint, target_fingerprint) if source_fingerprint and target_fingerprint else None
    approved = candidate(root, donor, receipts, synced=format_ == "lrc")
    usable = bool(same_pcm and approved and approved.current())
    timing = temporal_alignment(clip, source, target, min(donor["duration"], recipient["duration"])) if clip else None
    # Detect a version change during the read. This audit acquires no media
    # mutation lease and publishes no files, cache entries or editions.
    current(root, donor)
    current(root, recipient)
    return {"same_file": same_file, "same_pcm": same_pcm, "chromaprint": acoustic,
            "approved_reference": bool(approved), "eligible_pair": usable, "timing": timing,
            "seconds": monotonic() - start}


def main():
    cli = ArgumentParser(description=__doc__)
    cli.add_argument("index_snapshot", type=Path)
    cli.add_argument("library", type=Path)
    cli.add_argument("fpcalc", type=Path)
    cli.add_argument("--offsets", action="store_true", help="Comparer trois fenêtres PCM sans modifier les références")
    args = cli.parse_args()
    root = args.library.resolve(strict=True)
    index = args.index_snapshot.resolve(strict=True)
    tool = str(args.fpcalc.resolve(strict=True))
    clip = runpy.run_path(str(HERE.with_name("qualify-parole-separation.py")))["pcm"] if args.offsets else None
    if index.is_relative_to(root):
        cli.error("Utiliser une copie SQLite hors bibliothèque")
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        counts, targets, samples = pairs(db)
        receipts = {subject: json.loads(value) for subject, value in db.execute("""
            SELECT subject,value FROM reviews WHERE kind='transcription_validation' AND state='validated'
        """)}
        keys = {identity for sample in samples.values() for _, donor, recipient, _ in sample
                for identity in (donor, recipient)}
        rows = {identity: db.execute("SELECT * FROM songs WHERE id=?", (identity,)).fetchone() for identity in keys}
    results = {}
    for (format_, cohort), found in sorted(samples.items()):
        matches, totals = [], Counter()
        for _, donor, recipient, delta in found:
            try:
                result = compare(root, rows[donor], rows[recipient], format_, receipts, tool, clip)
            except (OSError, ValueError, SubprocessError):
                totals["changed_or_unreadable"] += 1
                continue
            totals.update(same_file=int(result["same_file"]), same_pcm=int(result["same_pcm"]),
                          acoustically_close=int(result["chromaprint"] is not None and result["chromaprint"] >= .93),
                          approved_reference=int(result["approved_reference"]), eligible_pair=int(result["eligible_pair"]))
            matches.append(dict(result, delta_seconds=delta))
        totals["fingerprint_comparable"] = sum(result["chromaprint"] is not None for result in matches)
        results[f"{format_}:{cohort}"] = dict(pairs=counts[format_, cohort], recipients=len(targets[format_, cohort]),
                                            sampled=len(found), totals=dict(totals), outcomes=matches)
    print(json.dumps(results, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
