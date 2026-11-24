#!/usr/bin/env python3
"""Silent PCM qualification of the prepared player recognizer and scorer."""
from argparse import ArgumentParser
from contextlib import closing
from dataclasses import asdict
import json
from math import isfinite
import os
from pathlib import Path
import subprocess
import sys
from time import monotonic

import numpy as np

BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from _kara_game import GameScore
from _kara_paths import DATA, music_library
from _kara_prepare import PreparedCache
from _kara_scoring import (AudioWindow, SCORE_TOLERANCE, prepared_window, score_breakdown,
                           score_reference_line)
from _kara_stems import cached_stems
from _kara_turbo import MODEL, RATE, TurboDecoder, build_decoder, digest
from _kara_voice import PlayerDecoding, transcribe_player, transcribe_prepared_job
from _kara_words import words

FIXTURES = Path(__file__).with_name("fixtures") / "chant-cases.json"
PROFILES = {
    "ligne-glouton": PlayerDecoding(),
    "ligne-faisceau": PlayerDecoding(beam=True),
    "court-3s": PlayerDecoding(3, 0),
    "chevauche-3s": PlayerDecoding(3, .75),
    "chevauche-3s-faisceau": PlayerDecoding(3, .75, True),
    "chevauche-6s": PlayerDecoding(6, 1.5),
}
DELAYS = (.25, .5, .75, 1., 1.5, 2.)
FILTERED_REPLAYS = {
    "original-filtre-seul": ("lowpass=f=950:p=2", .18),
    "instrumental-filtre-seul": ("lowpass=f=950:p=2", .18),
    "original-filtre-bis-seul": ("highpass=f=220,lowpass=f=1250:p=2", .31),
    "original-filtre-median-seul": ("highpass=f=340,lowpass=f=1550", .37),
}


def sample_count(seconds):
    return round(seconds * RATE)


def ffmpeg(arguments, pcm=None):
    # Pipes and regular local files are the only inputs and outputs. No
    # playback, device backend, network protocol or microphone is opened.
    return subprocess.run(["ffmpeg", "-hide_banner", "-nostdin", "-v", "error",
                           "-protocol_whitelist", "file,pipe", *arguments, "-f", "f32le", "pipe:1"],
                          input=pcm if pcm is not None else b"", stdout=subprocess.PIPE,
                          check=True, timeout=25).stdout


def load_pcm(path, start, stop):
    if not (0 <= start < stop <= start + 35) or not path.is_file():
        raise ValueError("Fichier local ou durée de l'extrait invalide.")
    raw = ffmpeg(["-ss", str(start), "-i", str(path.resolve(strict=True)), "-map", "0:a:0",
                  "-vn", "-sn", "-dn", "-t", str(stop - start), "-ac", "1", "-ar", str(RATE)])
    expected = sample_count(stop) - sample_count(start)
    return sized(np.frombuffer(raw, dtype="<f4"), expected)


def sized(audio, count):
    return np.pad(audio[:count], (0, max(0, count - len(audio))))


def apply_filter(audio, filters):
    return np.frombuffer(ffmpeg(["-f", "f32le", "-ac", "1", "-ar", str(RATE),
                                 "-i", "pipe:0", "-af", filters], audio.astype("<f4").tobytes()), dtype="<f4")


def stretch(audio, tempo=1., pitch=0):
    # Pad the filter tail before fitting to the sample count. Pitch-only
    # variants share the exact crop origin and duration; formants are preserved.
    result = apply_filter(np.pad(audio, (0, RATE // 4)),
                          f"rubberband=tempo={tempo:.9f}:pitch={2 ** (pitch / 12):.9f}"
                          ":formant=preserved:pitchq=quality")
    return sized(result, round(len(audio) / tempo))


def waveform_variants(voice, case):
    yield "voix", voice, 0.
    for shift in (-4, -2, -1, 1, 2, 4):
        yield f"hauteur{shift:+d}", stretch(voice, pitch=shift), 0.
    x = np.linspace(0., 1., len(voice), dtype=np.float32)
    chord = np.interp(x, [0, .31, .38, .62, .69, .92, 1.], [0, 0, 1, 1, 2, 2, 1])
    modified = np.zeros_like(voice)
    for index, pitch in enumerate((-2, 2, -1)):
        modified += np.maximum(0, 1 - np.abs(chord - index)) * stretch(voice, pitch=pitch)
    yield "hauteur-variable", modified, 0.
    yield "rythme+6%", stretch(voice, tempo=1.06), 0.
    yield "rythme-6%", stretch(voice, tempo=.94), 0.
    first, last = (sample_count(point - case["clip"][0]) for point in case["hold"])
    yield "note-prolongee", np.concatenate((voice[:first], stretch(voice[first:last], tempo=.42), voice[last:])), 0.
    envelope = np.interp(x, [0, .25, .5, .75, 1], [.09, .8, .12, 1, .16]).astype(np.float32)
    yield "volume-variable", voice * envelope, 0.
    yield "volume-24dB", voice * 10 ** (-24 / 20), 0.


def timeline(audio, origin, start, stop):
    # All crops address the unchanged musical clock. A shifted take adds
    # leading zeros or loses its tail if it runs outside the real job window.
    first, last = sample_count(start) - sample_count(origin), sample_count(stop) - sample_count(origin)
    result = np.zeros(last - first, dtype=np.float32)
    low, high = max(0, first), min(len(audio), last)
    if low < high:
        result[low - first:high - first] = audio[low:high]
    return result


def rms(audio):
    return float(np.sqrt(np.mean(audio * audio)))


def quantized(audio):
    # The input has the same finite signed-16-bit resolution as the game mic.
    return np.clip(np.round(audio * 32768), -32768, 32767).astype("<i2").astype(np.float32) / 32768


def lexical_errors(expected, heard):
    # Pure lexical Levenshtein, with deterministic S/D/I ties. The timing
    # comparator is separate and uses the prepared song times.
    row = [(j, 0, 0, j) for j in range(len(heard) + 1)]
    for i, target in enumerate(expected, 1):
        prior, row = row, [(i, 0, i, 0)]
        for j, token in enumerate(heard, 1):
            cost, s, d, ins = prior[j - 1]
            different = target != token
            above, left = prior[j], row[-1]
            row.append(min((cost + different, s + different, d, ins),
                           (above[0] + 1, above[1], above[2] + 1, above[3]),
                           (left[0] + 1, left[1], left[2], left[3] + 1)))
    cost, s, d, ins = row[-1]
    return {"expected": len(expected), "errors": cost, "substitutions": s, "deletions": d,
            "insertions": ins, "wer": cost / len(expected) if expected else None}


def windows(reference, indices, post_roll):
    times = [line["display_start"] for line in reference["lines"]]
    for index in indices:
        line = reference["lines"][index]
        end = line["end"]
        if end is None:
            end = GameScore.line_end(times, reference["duration"], index)
        window = prepared_window(index, times[index], end, reference["duration"], 1.,
                                 tuple(word["start"] for word in line["words"]), SCORE_TOLERANCE,
                                 post_roll=post_roll)
        if window is None:
            raise ValueError(f"Ligne {index + 1} sans fenêtre de notation")
        yield window


def emit(report, **row):
    print(json.dumps(row, ensure_ascii=False, allow_nan=False), file=report, flush=True)
    if row["kind"] != "metadata":
        short = {key: row[key] for key in ("kind", "case", "variant", "elapsed_s") if key in row}
        short.update({key: row["result"][key] for key in ("correct", "verified") if key in row.get("result", {})})
        print(json.dumps(short, ensure_ascii=False), flush=True)


def scored_pass(report, decoder, case, reference, game, jobs, variant, signal, *, decoding, kind, shift=0.):
    game.line_results.clear()
    details = []
    began = monotonic()
    for window, job, backing, original in jobs:
        voice = timeline(signal, case["clip"][0] + shift, window.start, window.stop)
        audio = voice
        if kind == "leak":
            if variant == "silence":
                audio = np.zeros_like(voice)
            elif variant == "instrumental-seul":
                audio = .25 * backing
            elif variant == "original-seul":
                audio = .25 * original
            elif variant in FILTERED_REPLAYS:
                music = original if variant.startswith("original") else backing
                filters, delay = FILTERED_REPLAYS[variant]
                audio = .25 * apply_filter(music, filters)
                audio = timeline(audio, window.start + delay, window.start, window.stop)
            elif variant != "voix":
                source, level = variant.split("/")
                music = original if source.startswith("mix-original") else backing
                scale = rms(voice) * 10 ** (int(level) / 20) / max(rms(music), 1e-8)
                audio = voice + music * scale
        # A common gain prevents clipping without altering the requested
        # stem-to-music signal ratio. Low-level cases keep their own RMS.
        audio = quantized(audio / max(1., float(np.max(np.abs(audio))) / .98))
        diagnostics = {}
        start = monotonic()
        heard, source = transcribe_prepared_job(decoder, audio, job, decoding=decoding, diagnostics=diagnostics)
        elapsed = monotonic() - start
        game.match_reference(window, {"source": source, "heard": heard})
        details.append({"line": window.line + 1, "start": window.start, "stop": window.stop,
                        "source": source, "elapsed_s": elapsed, "rms": rms(audio), "heard": heard,
                        "words": game.line_results[window.line], "diagnostics": diagnostics})
    # Only the predeclared excerpt is evaluated here. Full-song coverage is
    # measured separately, and production adjudication uses the full reference.
    selected = [reference["lines"][window.line] for window, *_ in jobs]
    summary = score_breakdown({"lines": selected},
                              {j: game.line_results[window.line] for j, (window, *_) in enumerate(jobs)})
    lexical = None
    if kind in ("strategy-live", "context"):
        # Assign tokens by a fixed clock partition for WER, independently of
        # the game comparator's lexical ownership of overlapping windows.
        limits = [case["clip"][0], *(line["display_start"] for line in selected[1:]), case["clip"][1]]
        expected = [word["normalized"] for line in selected for word in line["words"]]
        heard = [word["word"] for row, low, high in zip(details, limits, limits[1:])
                 for word in row["heard"] if low <= row["start"] + word["start"] < high]
        lexical = lexical_errors(expected, heard)
    emit(report, kind=kind, case=case["id"], variant=variant, elapsed_s=monotonic() - began,
         result=summary, lines=details, injected_shift_s=shift, signal_samples=len(signal),
         **({"lexical": lexical} if lexical is not None else {}))
    return summary


def strategy_pass(report, decoder, case, reference, signal, named_profiles):
    expected = [w["normalized"] for i in case["lines"] for w in reference["lines"][i - 1]["words"]]
    start, stop = case["clip"]
    # Same excerpt and padding for all profiles. The lyric sequence is
    # compared after inference, and the DTW points remain on the song clock.
    audio = timeline(signal, start, start - .6, stop + .6)
    for name, profile in named_profiles.items():
        began = monotonic()
        diagnostics = {}
        heard, source = transcribe_player(decoder, quantized(audio), reference["language"],
                                          decoding=profile, diagnostics=diagnostics)
        observed = [{**w, "start": w["start"] + start - .6,
                     "end": w["end"] + start - .6 if w["end"] is not None else None} for w in heard]
        synthetic_line = {"words": [{**w, "id": j} for j, w in enumerate(
            w for i in case["lines"] for w in reference["lines"][i - 1]["words"])]}
        aligned = score_reference_line(synthetic_line, observed, source=source)
        emit(report, kind="strategy", case=case["id"], variant=name, elapsed_s=monotonic() - began,
             lexical=lexical_errors(expected, [w["word"] for w in observed]), heard=observed,
             onsets=[w["delay"] for w in aligned if w["status"] in ("correct", "early", "late")],
             source=source, diagnostics=diagnostics)


def passage(report, decoder, case, reference, path, stems, group, profiles, post_roll):
    first, last = case["clip"]
    voice = load_pcm(stems[1], first, last).copy()
    # A short taper avoids clicks from the fixture boundary itself.
    fade = np.linspace(0., 1., RATE // 100)
    voice[:len(fade)] *= fade
    voice[-len(fade):] *= fade[::-1]
    game = GameScore(lambda: None)
    game.set_round(reference=reference)
    game.tolerance = SCORE_TOLERANCE
    def make_jobs(tail):
        results = []
        for window in windows(reference, [index - 1 for index in case["lines"]], tail):
            results.append((window, {"instrumental": True, "track_start": window.start, "track_end": window.stop,
                                     "original": str(path), "backing": str(stems[0]),
                                     "language": reference["language"]},
                            load_pcm(stems[0], window.start, window.stop) if group == "leak" else None,
                            load_pcm(path, window.start, window.stop) if group == "leak" else None))
        return results

    if group == "strategy":
        strategy_pass(report, decoder, case, reference, voice, profiles)
        jobs = make_jobs(post_roll)
        for name in ("ligne-glouton", "ligne-faisceau"):
            scored_pass(report, decoder, case, reference, game, jobs, name, voice,
                        decoding=profiles[name], kind="strategy-live")
        return []
    if group in ("context", "context-holdout"):
        voices = {"voix": voice, "hauteur-2": stretch(voice, pitch=-2)}
        for tail in (0., .75, 1.5):
            for name, signal in voices.items():
                scored_pass(report, decoder, case, reference, game, make_jobs(tail), f"{name}/queue+{tail:g}s", signal,
                            decoding=next(iter(profiles.values())), kind="context")
        return []
    jobs = make_jobs(post_roll)
    failures = []
    for profile in profiles.values():
        if group == "voice":
            variants = waveform_variants(voice, case)
        elif group == "timing":
            # The fixed transposition decorrelates the player's waveform
            # from the artist's mix. Every delay starts from the same take.
            timing_voice = stretch(voice, pitch=2)
            variants = [("a-l-heure", timing_voice, 0.),
                        *((f"retard-{delay:g}s", timing_voice, delay) for delay in DELAYS),
                        ("retard-progressif", stretch(timing_voice, tempo=len(voice) / (len(voice) + 1.6 * RATE)), 0.)]
        else:
            variants = [(name, voice, 0.) for name in (
                "silence", "instrumental-seul", "original-seul", *FILTERED_REPLAYS, "voix",
                *(f"{source}/{level}" for source in ("mix-original", "mix-instrumental") for level in (-18, -6, 3)),
            )]
            shifted = stretch(voice, pitch=2)
            variants.extend((f"mix-original-hauteur+2/{level}", shifted, .025) for level in (-18, -6, 3))
        for name, signal, shift in variants:
            summary = scored_pass(report, decoder, case, reference, game, jobs, name, signal,
                                  decoding=profile, kind=group, shift=shift)
            if group == "leak" and (name == "silence" or name.endswith("-seul")) and summary["correct"]:
                failures.append(f"{case['id']} · {name} · {summary['correct']} faux crédits")
    return failures


def speech(report, decoder, spec, path, profiles):
    import wave
    with wave.open(str(path), "rb") as source:
        if (source.getframerate(), source.getsampwidth(), source.getnchannels()) != (RATE, 2, 1):
            raise ValueError("La fixture parlée doit être un WAVE PCM mono 16 bits / 16 kHz.")
        if not 0 < source.getnframes() <= 30 * RATE:
            raise ValueError("Durée de la fixture parlée hors limites.")
        pcm = source.readframes(source.getnframes())
    signal = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    for name, profile in profiles.items():
        began = monotonic()
        heard, source = transcribe_player(decoder, signal, spec["language"], decoding=profile)
        emit(report, kind="speech", case="jfk", variant=name, elapsed_s=monotonic() - began,
             lexical=lexical_errors(words(spec["text"]), [w["word"] for w in heard]), heard=heard, source=source)


def private_report(path):
    return open(path, "x", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o600))


def audit(report, cache, library, spec):
    for name in [*(case["song"] for case in spec["passages"]), *spec["other_references"]]:
        path = (library / name).resolve(strict=True)
        reference = cache.cached(path)
        if reference is None:
            raise ValueError(f"Référence validée requise pour {name}.")
        perfect, omissions = {}, {}
        count = 0
        for index, line in enumerate(reference["lines"]):
            heard, sparse = [], []
            for word in line["words"]:
                if word["start"] is not None:
                    item = {"word": word["normalized"], "start": word["start"], "confidence": .95}
                    heard.append(item)
                    count += 1
                    if count % 5:
                        sparse.append(item)
            perfect[index] = score_reference_line(line, heard)
            omissions[index] = score_reference_line(line, sparse)
        emit(report, kind="audit", case=reference["title"],
             audio_sha256=reference["audio"]["sha256"], source_lrc_sha256=reference["source"]["sha256"],
             manifest_sha256=digest(cache.folder(path) / "prepared.json"),
             prepared_lrc_sha256=reference["lrc_sha256"],
             result=score_breakdown(reference, perfect), one_in_five_omitted=score_breakdown(reference, omissions))


def review(report, cache, song, sidecar, first, last):
    reference = cache.cached(song.resolve(strict=True))
    with sidecar.open(encoding="utf-8") as stream:
        raw = stream.read(16_000_001)
    if len(raw) > 16_000_000:
        raise ValueError("Diagnostic trop volumineux.")
    capture = json.loads(raw)
    if reference is None or any(capture.get(field) != reference[key] for field, key in (
            ("reference_id", "song_id"), ("reference_lrc", "lrc_sha256"), ("reference_revision", "prepared_at"))):
        raise ValueError("Le diagnostic et le manifeste ne décrivent pas la même génération.")
    tolerance = capture["tolerance"]
    if not isfinite(tolerance) or not 0 <= tolerance <= 5:
        raise ValueError("Tolérance invalide dans le diagnostic.")
    first, last = 1 if first is None else first, len(reference["lines"]) if last is None else last
    if not 1 <= first <= last <= len(reference["lines"]):
        raise ValueError("Intervalle de lignes hors limites.")
    game = GameScore(lambda: None)
    game.set_round(reference=reference)
    game.tolerance = tolerance
    for line in sorted(capture["lignes"], key=lambda row: row["id"]):
        index = line["ligne"] - 1
        start, stop, rate = line["debut_morceau_s"], line["fin_morceau_s"], line["vitesse"]
        if not (0 <= index < len(reference["lines"]) and 0 <= start < stop <= reference["duration"]
                and isfinite(rate) and rate > 0):
            raise ValueError("Fenêtre de diagnostic hors limites.")
        if not first <= index + 1 <= last:
            continue
        game.match_reference(AudioWindow(index, start, stop, rate),
                             {"source": line["source"], "heard": line["mots_fenetre"]})
        emit(report, kind="diagnostic-line", case=reference["title"],
             window=line, words=game.line_results[index])
    excerpt = score_breakdown({"lines": reference["lines"][first - 1:last]},
                              {i - first + 1: result for i, result in game.line_results.items()})
    emit(report, kind="diagnostic-summary", case=reference["title"], result=excerpt,
         tolerance=tolerance, scope=[first, last])


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("suite", choices=("strategy", "context", "context-holdout", "voice", "timing",
                                          "leak", "audit", "review"))
    parser.add_argument("--output", type=Path, required=True, help="Nouveau rapport JSONL (création exclusive)")
    parser.add_argument("--library", type=Path, default=music_library())
    parser.add_argument("--fixtures", type=Path, default=FIXTURES)
    parser.add_argument("--speech", type=Path, default=DATA / "hyprwhspr/pywhispercpp-src/whisper.cpp/samples/jfk.wav")
    parser.add_argument("--device", choices=("gpu", "cpu"), default="gpu")
    parser.add_argument("--post-roll", type=float, choices=(0., .75, 1.5), default=0.)
    parser.add_argument("--profile", choices=PROFILES, default="ligne-glouton")
    parser.add_argument("--case", help="Identifiant du passage dans le fichier de fixtures")
    parser.add_argument("--song", type=Path, help="Morceau du tour enregistré (review)")
    parser.add_argument("--sidecar", type=Path, help="Diagnostic JSON du tour (review)")
    parser.add_argument("--first", type=int, help="Première ligne du diagnostic (review, base 1)")
    parser.add_argument("--last", type=int, help="Dernière ligne du diagnostic (review, incluse)")
    args = parser.parse_args()
    cache = PreparedCache()
    if args.suite == "review":
        if args.song is None or args.sidecar is None:
            parser.error("review exige --song et --sidecar")
        with private_report(args.output) as report:
            review(report, cache, args.library / args.song, args.sidecar, args.first, args.last)
        return
    spec = json.loads(args.fixtures.read_text(encoding="utf-8"))
    if args.suite == "audit":
        with private_report(args.output) as report:
            audit(report, cache, args.library, spec)
        return
    inputs = []
    for case in spec["context_holdout" if args.suite == "context-holdout" else "passages"]:
        if args.case and args.case != case["id"]:
            continue
        path = (args.library / case["song"]).resolve(strict=True)
        reference, stems = cache.cached(path), cached_stems(path)
        if reference is None or stems is None:
            parser.error(f"Référence validée et pistes Demucs en cache requises pour {case['id']}.")
        inputs.append((case, reference, path, stems))
    if not inputs:
        parser.error("Aucun passage sélectionné.")
    profiles = PROFILES if args.suite == "strategy" else {args.profile: PROFILES[args.profile]}
    binary = build_decoder()
    threads = 8 if args.device == "cpu" else 4
    with private_report(args.output) as report:
        emit(report, kind="metadata", suite=args.suite, rate=RATE, tolerance=SCORE_TOLERANCE,
             post_roll_s=args.post_roll,
             device=args.device, threads=threads, build=binary.name, model_sha256=cache.model_info()["sha256"],
             voice_engine_sha256=digest(BIN / "_kara_voice.py"), scoring_sha256=digest(BIN / "_kara_scoring.py"),
             game_sha256=digest(BIN / "_kara_game.py"), harness_sha256=digest(Path(__file__).resolve()),
             ffmpeg=subprocess.check_output(["ffmpeg", "-version"], text=True).splitlines()[0],
             profiles={name: asdict(profile) for name, profile in profiles.items()},
             inputs=[{"case": case, "audio_sha256": ref["audio"]["sha256"],
                      "source_lrc_sha256": ref["source"]["sha256"],
                      "manifest_sha256": digest(cache.folder(path) / "prepared.json"),
                      "demucs_sha256": [digest(p) for p in stems]} for case, ref, path, stems in inputs],
             speech_sha256=digest(args.speech) if args.suite == "strategy" else None)
        with closing(TurboDecoder(binary, MODEL, args.device, threads)) as decoder:
            failures = []
            for case, reference, path, stems in inputs:
                failures.extend(passage(report, decoder, case, reference, path, stems, args.suite, profiles,
                                        args.post_roll))
            if args.suite == "strategy":
                speech(report, decoder, spec["speech"], args.speech, profiles)
        if failures:
            raise SystemExit("\n".join(failures))


if __name__ == "__main__":
    main()
