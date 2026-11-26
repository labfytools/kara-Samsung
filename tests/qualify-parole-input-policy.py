#!/usr/bin/env python3
"""Choisit une entrée acoustique à partir de reçus appariés, sans lire la référence de test."""
from argparse import ArgumentParser
from collections import defaultdict
import json
from pathlib import Path
import runpy
from statistics import median

FIRST_SECONDS = 5
CONFIDENCE = .62
BASS_SHARE = .65


def read(path):
    return json.loads(path.read_bytes())


def bass_share(pcm):
    import numpy as np

    # Measure low-band energy in the original mix. This is available before
    # Demucs and includes instruments and vocal fundamentals alike.
    samples = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    width = 640
    frames = samples[:len(samples) // width * width].reshape(-1, width)
    if not len(frames):
        return 0.
    power = (np.abs(np.fft.rfft(frames * np.hanning(width), axis=1)) ** 2).sum(axis=0)
    hz = np.fft.rfftfreq(width, 1 / 16_000)
    total = power[hz >= 50].sum()
    return float(power[(hz >= 50) & (hz <= 350)].sum() / total) if total else 0.


def entries(workspace, clip=None):
    items = []
    manifest = read(workspace / "manifest.json")
    for case in manifest["cases"]:
        stage = workspace / case["id"]
        receipt = stage / "turbo.json"
        # Each variant is resumable on its own. A guard may still interrupt
        # the campaign after both observations and before final settlement.
        if not receipt.is_file() or not read(receipt)["events"][-1].get("complete"):
            continue
        folder = stage / "observations"
        if not all((folder / f"{name}.json").is_file() for name in ("mix", "vocals")):
            continue
        mix, vocals = (read(folder / f"{name}.json") for name in ("mix", "vocals"))
        seconds = min(FIRST_SECONDS, case["end"] - case["start"])
        early = [w[3] for w in mix["words"] if case["start"] <= w[1] < case["start"] + seconds]
        density = sum(c >= CONFIDENCE for c in early) / seconds
        probability = median(early) if early else 0.
        low = bass_share(clip(case, seconds)) if clip else None
        items.append(dict(case=case["id"], genre=case["genre"], density=density, confidence=probability,
                          bass_share=low,
                          scores={"mix": mix["score"], "vocals": vocals["score"]},
                          count_mix=len(mix["words"]), count_vocals=len(vocals["words"]),
                          confidence_mix=median(w[3] for w in mix["words"]) if mix["words"] else 0.,
                          confidence_vocals=median(w[3] for w in vocals["words"]) if vocals["words"] else 0.))
    return manifest, items


def separate(row, limits):
    density, confidence = limits
    return row["density"] < density and row["confidence"] < confidence


def fit(rows):
    # Search a small explicit grid on the historical cohort. Charging one
    # error per separation avoids using Demucs for negligible apparent gain.
    trials = [(density, confidence) for density in (0., .4, .8, 1.2, 1.6)
              for confidence in (.30, .45, .55, .65, .75)]

    def loss(limits):
        use = [separate(row, limits) for row in rows]
        errors = sum(row["scores"]["vocals" if voice else "mix"]["errors"] for row, voice in zip(rows, use))
        return errors + sum(use), sum(use), limits

    return min(trials, key=loss)


def trusted_voice(row):
    return (row["count_vocals"] >= .8 * row["count_mix"]
            and row["confidence_vocals"] >= row["confidence_mix"] + .05)


def compare(rows, limits):
    scores = defaultdict(lambda: {"paired": 0, "expected": 0, "errors": 0, "demucs": 0,
                                  "voice_selected": 0, "substitutions": 0, "insertions": 0, "deletions": 0,
                                  "characters": 0, "character_errors": 0})
    for row in rows:
        gate = separate(row, limits)
        reggae = row["genre"] == "reggae"
        choices = [("mix", False, False), ("vocals", True, True),
                   ("genre", reggae, reggae), ("first_5s", gate, gate),
                   ("prudente", gate, gate and trusted_voice(row)), ("stem_disponible", False, trusted_voice(row))]
        if row["bass_share"] is not None:
            acoustics = row["bass_share"] >= BASS_SHARE and row["confidence"] < CONFIDENCE
            choices.append(("basses_et_confiance", acoustics, acoustics))
        for policy, run_stem, use_voice in choices:
            values = row["scores"]["vocals" if use_voice else "mix"]
            for genre in ("total", row["genre"]):
                group = scores[genre, policy]
                group["paired"] += 1
                group["demucs"] += run_stem
                group["voice_selected"] += use_voice
                for count in ("expected", "errors", "substitutions", "insertions", "deletions",
                              "characters", "character_errors"):
                    group[count] += values[count]
    result = defaultdict(dict)
    for (genre, name), counts in scores.items():
        result[genre][name] = dict(**counts, wer=counts["errors"] / counts["expected"],
                                   cer=counts["character_errors"] / counts["characters"])
    return result


def acoustic_summary(rows):
    values = defaultdict(list)
    for row in rows:
        values[row["genre"]].append(row["bass_share"])
    return {genre: dict(count=len(shares), median=median(shares), lowest=min(shares), highest=max(shares))
            for genre, shares in values.items()}


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("history", type=Path)
    parser.add_argument("--holdout", type=Path)
    parser.add_argument("--acoustic", action="store_true", help="Décoder cinq secondes pour la part des basses")
    args = parser.parse_args()
    clip = None
    if args.acoustic:
        tools = Path(__file__).resolve().with_name("qualify-parole-separation.py")
        decoder = runpy.run_path(str(tools))

        def clip(case, seconds):
            source = Path(case["path"])
            if decoder["digest"](source) != case["audio_sha256"]:
                raise ValueError("La piste a changé depuis sa qualification")
            return decoder["pcm"](source, case["start"], case["start"] + seconds)

    plan, history = entries(args.history, clip)
    if len(history) != len(plan["cases"]) or not history:
        raise ValueError("Il faut le témoin historique apparié complet")
    limits = fit(history)
    result = dict(limits=limits, historical_descriptive=compare(history, limits))
    if clip:
        result["low_band_history"] = acoustic_summary(history)
    if args.holdout:
        holdout, testing = entries(args.holdout, clip)
        if {r["id"] for r in plan["cases"]} & {r["id"] for r in holdout["cases"]}:
            raise ValueError("Les deux manifestes partagent des enregistrements")
        if plan["weights"] != holdout["weights"]:
            raise ValueError("Le banc de sélection exige les mêmes poids")
        result.update(held_out=compare(testing, limits), planned=len(holdout["cases"]))
        if clip:
            result["low_band_held_out"] = acoustic_summary(testing)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))


if __name__ == "__main__":
    main()
