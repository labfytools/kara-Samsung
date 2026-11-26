#!/usr/bin/env python3
"""Compare les entrées de Turbo sur un manifeste figé et qualifie leur coût."""

from argparse import ArgumentParser
from array import array
from collections import Counter
from contextlib import ExitStack, closing
from csv import DictReader
from dataclasses import replace
from hashlib import sha256
import json
from math import sqrt
import os
from pathlib import Path
import resource
import runpy
import signal
import sqlite3
import subprocess
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_audio_identity import pcm_digest
from _kara_import_resources import lease
from _kara_prepare import PreparedCache, Preparer, atomic
from _kara_stems import (CHECKPOINT, MODEL as STEM_MODEL, PYTHON as DEMUCS, ROOT as STEM_ROOT,
                         model_signature, read_metadata, stem_directory, stem_files)
from _kara_turbo import MODEL, OVERLAP, RATE, STRIDE, SOURCE, digest, pcm_file
from _kara_words import ARGOT, words
from _parole_acoustic import prompts, sound, substitutions
from _parole_lexicon import ranked_forms
from _parole_night import Login
from _parole_process import Interrupted, run
from _parole_qualification_receipts import restore as restore_variant, save as save_variant
from _parole_separation_metrics import process_tree, system_reclaim, word_details

HERE = Path(__file__).resolve()
VARIANTS = ("mix", "vocals", "normalized", "filtered", "adaptive", "targeted", "combined")
PAD = 7.


def write(path, value):
    atomic(path, json.dumps(value, ensure_ascii=False, allow_nan=False).encode())


def read(path):
    return json.loads(path.read_bytes())


def emit(**fields):
    print(json.dumps(fields, allow_nan=False), flush=True)


def rss():
    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss


def demucs_worker(engine, segment, destination, source):
    began = monotonic()
    emit(phase="imports")
    from demucs import separate
    for symbol, phase in (("get_model_from_args", "weights"), ("load_track", "pcm"),
                          ("apply_model", "separation"), ("save_audio", "flac")):
        def instrument(call, phase):
            def measured(*args, **kwargs):
                start = monotonic()
                emit(phase=phase)
                value = call(*args, **kwargs)
                emit(completed=phase, seconds=monotonic() - start, high_water_kib=rss())
                return value
            return measured
        setattr(separate, symbol, instrument(getattr(separate, symbol), phase))
    # In Demucs 4.0.1, jobs=0 defers each segment until overlap-add
    # consumes it. The one-thread executor for jobs=1 retains all results.
    jobs = "0" if engine == "serial" else "1"
    separate.main(["--two-stems=vocals", "-n", STEM_MODEL, "-d", "cpu", "--shifts", "0",
                   "--overlap", "0.1", "--segment", segment, "--flac", "-j", jobs,
                   "-o", destination, source])
    emit(complete=True, seconds=monotonic() - began, high_water_kib=rss())


def clip(src, start, end, *fmt):
    return ["ffmpeg", "-v", "error", "-xerror", "-nostdin", "-protocol_whitelist", "file,pipe",
            "-ss", str(start), "-i", str(src), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-t", str(end - start), *fmt]


def pcm(src, start, end):
    return subprocess.check_output(clip(src, start, end, "-ac", "1", "-ar", str(RATE),
                                        "-f", "s16le", "-"), timeout=20)


def normalized(raw):
    signal = array("h")
    signal.frombytes(raw)
    rms = sqrt(sum(x * x for x in signal) / len(signal))
    peak = max(map(abs, signal))
    if not rms or not peak:
        return raw
    limit = 10 ** (3 / 20)
    gain = min(limit, 32768 * .12 / rms, 32760 / peak)
    gain = max(1 / limit, gain)
    return array("h", (min(32767, max(-32768, round(x * gain))) for x in signal)).tobytes()


def highpass(raw):
    # Compare one mild high-pass by itself, on the same timeline and PCM.
    return subprocess.run(("ffmpeg", "-v", "error", "-xerror", "-nostdin", "-f", "s16le", "-ar", str(RATE),
                           "-ac", "1", "-i", "pipe:0", "-af", "highpass=f=75:p=2",
                           "-f", "s16le", "pipe:1"), input=raw, capture_output=True, check=True, timeout=10).stdout


def adaptive_cores(count, fetch):
    """Prefer quiet boundaries while preserving every sample and a single owner."""
    step = RATE // 10
    first = 0
    while count - first > 12 * RATE:
        start, stop = first + 6 * RATE, first + 10 * RATE
        view = memoryview(fetch(start, stop)).cast("h")
        offsets = range(0, len(view) - 2 * step + 1, step)
        valley = min(offsets, key=lambda i: (sum(x * x for x in view[i:i + 2 * step]),
                                             abs(i - 2 * RATE)))
        boundary = start + valley + step
        yield first, boundary
        first = boundary
    yield first, count


def observe(decoder, raw, origin, *, language="", beam=False, prompt=""):
    lang, segments = decoder.decode(raw, language, origin, beam=beam, prompt=prompt)
    heard = [word for segment in segments if segment.no_speech <= .65 for word in segment.words]
    return lang, heard


def windowed(decoder, count, fetch, origin, *, adaptive):
    cores = (adaptive_cores(count, fetch) if adaptive else
             ((i, min(i + STRIDE * RATE, count)) for i in range(0, count, STRIDE * RATE)))
    languages, heard = Counter(), []
    for low, high in cores:
        start, stop = max(0, low - OVERLAP * RATE), min(count, high + OVERLAP * RATE)
        lang, row = observe(decoder, fetch(start, stop), origin + start / RATE)
        owned = [w for w in row if low <= round((w.start - origin) * RATE) < high]
        languages[lang] += len(owned)
        heard.extend(owned)
    return (languages.most_common(1)[0][0] if languages else ""), heard


def consensus(baseline, confirm, support):
    final = []
    # Two reads must agree near the original onset. Preserve the number
    # of words and ownership of timestamps even when hypotheses disagree.
    for w in baseline:
        options = {a.text for a in confirm if w.confidence < .62 and abs(a.start - w.start) <= .22
                   and a.confidence >= .65 and a.text != w.text and
                   any(b.text == a.text and abs(b.start - w.start) <= .22 and b.confidence >= .65
                       for b in support)}
        final.append(replace(w, text=next(iter(options))) if len(options) == 1 else w)
    return final


def reread(decoder, fetch, count, origin, language, baseline, support):
    uncertain = [w for w in baseline if w.confidence < .62]
    if not uncertain:
        return baseline, None, []
    pivot = min(uncertain, key=lambda w: (w.confidence, w.start))
    low = max(0, round((pivot.start - origin - 2.5) * RATE))
    high = min(count, round((pivot.start - origin + 2.5) * RATE))
    offset = origin + low / RATE
    raw = fetch(low, high)
    _, confirm = observe(decoder, raw, offset, language=language, beam=True)
    return consensus(baseline, confirm, support), (low, high), confirm


def read_cases(args):
    if args.annotations:
        root = args.annotations.resolve(strict=True)
        cases = []
        for item in read(root / "corpus.json"):
            path = (root / item["song"]).resolve(strict=True)
            if not path.is_relative_to(root):
                raise ValueError("Audio annoté hors corpus")
            labels = path.parent / "words.words.txt"
            expected = [w for label in labels.read_text().splitlines() for w in words(label)]
            cases.append(dict(id=item["id"], genre=item["language"], path=str(path),
                              expected=expected, annotation=str(path.parent), start=0., end=None,
                              reference_hashes=[digest(p) for p in (
                                  labels, path.parent / "manual.csv", path.parent / "lines.csv")]))
        return cases

    previous = [row for row in map(json.loads, (args.previous / "result.jsonl").read_text().splitlines())]
    baselines = {row["id"]: row for row in previous}
    source = read(args.previous / "manifest.json")
    if len(source) != 50 or len(baselines) != 50 or len(previous) != 50:
        raise ValueError("Le comparatif exige le manifeste historique complet de 50 extraits")
    lookup = runpy.run_path(str(HERE.with_name("qualify-parole-targeted.py")))["extract"]
    root = args.library.resolve(strict=True)
    with closing(sqlite3.connect(args.index.resolve(strict=True).as_uri() + "?mode=ro", uri=True)) as db:
        for case in source:
            track = db.execute("SELECT path FROM songs WHERE id=?", (case["id"],)).fetchone()
            sample = lookup(root, track[0]) if track else None
            if (sample is None or tuple(sample[:2]) != (case["start"], case["end"])
                    or len(sample[2]) != baselines[case["id"]]["baseline"]["expected"]):
                raise ValueError("Une entrée ne correspond plus au manifeste de 50 titres")
            case.update(path=track[0], expected=sample[2], previous=baselines[case["id"]]["baseline"])
    return source


def summarize_resources(samples):
    return {"rss_group_kib": max((s.get("group_rss_kib", 0) for s in samples), default=0),
            "pss_group_kib": max((s.get("group_pss_kib", 0) for s in samples), default=0),
            "min_available_kib": min((s["memory_available_kib"] for s in samples), default=None),
            "max_psi_pct": max((s["memory_psi_some_avg10_pct"] for s in samples), default=None),
            "max_cpu_c": max((s["cpu_c"] or 0 for s in samples), default=0),
            "max_gpu_c": max((s["gpu_c"] or 0 for s in samples), default=0),
            "max_gpu_busy_pct": max((s["gpu_busy_pct"] or 0 for s in samples), default=0),
            "max_gpu_gtt_bytes": max((s["gpu_gtt_bytes"] or 0 for s in samples), default=0),
            "max_gpu_vram_bytes": max((s["gpu_vram_bytes"] or 0 for s in samples), default=0)}


def guarded(args, command, receipt, *, demucs=False):
    benchmark = runpy.run_path(str(HERE.with_name("benchmark-kara-preparation.py")))
    monitor = benchmark["Monitor"](args.workspace, args.annotations or args.library, args.profile, args.memory_psi)
    if monitor.login is None:
        monitor.login = Login()
    events = []
    parent = os.getpid()

    def stop():
        prior = len(monitor.samples)
        canceled = monitor.stop()
        if len(monitor.samples) != prior:
            stats = process_tree(parent)
            monitor.samples[-1].update(
                group_rss_kib=sum(p.get("VmRSS", 0) for p in stats),
                group_pss_kib=sum(p.get("Pss", 0) for p in stats),
                group_anon_kib=sum(p.get("RssAnon", 0) for p in stats),
                group_swap_kib=sum(p.get("VmSwap", 0) for p in stats),
                process_memory=stats, **system_reclaim(),
                phase=next((e["phase"] for e in reversed(events) if "phase" in e), "admission"))
            if monitor.login.sleeping():
                monitor.reason = "Mise en veille demandée"
                return True
        return canceled

    env = dict(os.environ, OMP_NUM_THREADS="2", MKL_NUM_THREADS="2", OPENBLAS_NUM_THREADS="1",
               TORCH_HOME=str(CHECKPOINT.parents[2])) if demucs else None
    try:
        deadline = monotonic() + 180
        while True:
            if not stop() and monitor.samples[-1]["memory_available_kib"] >= 4096 * 1024:
                break
            if monitor.reason is None:
                monitor.reason = "Pression mémoire · réserve d’admission"
            if monotonic() >= deadline or not monitor.reason.startswith(("Limite thermique", "Pression mémoire")):
                raise Interrupted(monitor.reason)
            sleep(2)
            monitor.reason = None
        code, _ = run(["nice", "-n", "12", *command], stop=stop, timeout=1800,
                      env=env, event=events.append)
        if code or not any(item.get("complete") for item in events):
            failure = next((item["failure"] for item in reversed(events) if "failure" in item), "")
            raise RuntimeError(f"Moteur arrêté ({code}) · {failure}")
    except (Interrupted, RuntimeError, TimeoutError):
        write(receipt.with_suffix(f".{time_ns()}.interrupted.json"),
              {"reason": monitor.reason, "events": events, "samples": monitor.samples})
        if monitor.reason:
            raise Interrupted(monitor.reason) from None
        raise
    phases = {sample["phase"] for sample in monitor.samples}
    report = {"events": events, "resources": summarize_resources(monitor.samples), "samples": monitor.samples,
              "resources_by_phase": {name: summarize_resources([s for s in monitor.samples if s["phase"] == name])
                                     for name in sorted(phases)}}
    return report


def prepare_stem(args, case, folder):
    receipt = folder / "demucs.json"
    if receipt.exists():
        saved = read(receipt)
        if digest(Path(saved["vocals"])) != saved["sha256"]:
            raise ValueError("Piste vocale modifiée entre deux exécutions")
        return saved
    if args.stems_from:
        reusable = args.stems_from / case["id"] / "demucs.json"
        if reusable.is_file():
            saved = read(reusable)
            if digest(Path(saved["vocals"])) != saved["sha256"]:
                raise ValueError("Piste vocale importée modifiée")
            write(receipt, {**saved, "reused_from": str(reusable)})
            return saved
    source = Path(case["path"])
    with lease(STEM_ROOT / "demucs.lock", wait=False) as lock:
        if lock is None:
            raise Interrupted("Créneau Demucs Kara occupé")
        # Legacy caches remain usable by Kara. Qualification requires PCM
        # provenance as well as the current model settings and FLAC checks.
        cached = stem_directory(source)
        try:
            meta = read_metadata(cached)
            ready = (stem_files(cached) if args.segment == 7 and meta.get("processing") == model_signature()
                     and meta.get("audio") == pcm_digest(source) else None)
        except (OSError, ValueError, subprocess.SubprocessError):
            ready = None
        if ready:
            result = dict(vocals=str(ready[1]), sha256=digest(ready[1]), offset=0., method="cache-complet")
        else:
            with TemporaryDirectory(dir=folder, prefix="demucs-") as temporary:
                scratch = Path(temporary)
                staged = source
                offset = 0.
                if case["end"] is not None:
                    offset = max(0., case["start"] - PAD)
                    staged = scratch / "input.wav"
                    subprocess.run(clip(source, offset, case["end"] + PAD, "-ar", "44100", "-ac", "2",
                                        "-c:a", "pcm_f32le", str(staged)), check=True, timeout=30)
                report = guarded(args, [str(DEMUCS), "-B", str(HERE), "--demucs-worker",
                                         args.engine, str(args.segment), str(scratch), str(staged)],
                                 receipt, demucs=True)
                stem = scratch / STEM_MODEL / staged.stem / "vocals.flac"
                final = folder / "vocals.flac"
                if stem.stat().st_size <= 4096:
                    raise ValueError("Séparation vocale vide")
                stem.replace(final)
                result = dict(vocals=str(final), sha256=digest(final), offset=offset, method=args.engine, **report)
                if args.keep_instrumental:
                    source_instrumental = stem.with_name("no_vocals.flac")
                    instrumental = source_instrumental.replace(folder / "instrumental.flac")
                    result.update(instrumental=str(instrumental), instrumental_sha256=digest(instrumental))
        write(receipt, result)
        return result


def alignment(known, heard, truth):
    """Match lyrics in order. Independent times only break equal lexical costs."""
    n = len(heard)
    previous = [(i, 0.) for i in range(n + 1)]
    moves = [bytearray([2]) * (n + 1)]
    for i, (text, start) in enumerate(zip(known, truth, strict=True), 1):
        row = [(i, 0.)]
        move = bytearray([1])
        for j, word in enumerate(heard, 1):
            cost, tie = previous[j - 1]
            match = text == word.text
            timing = min(90., abs(start - word.start)) if match and start is not None else 0.
            choices = ((cost + (not match), tie + timing, 0),
                       (previous[j][0] + 1, previous[j][1], 1),
                       (row[-1][0] + 1, row[-1][1], 2))
            cost, tie, decision = min(choices)
            row.append((cost, tie))
            move.append(decision)
        previous = row
        moves.append(move)
    matched = [None] * len(known)
    i, j = len(known), n
    while i or j:
        step = moves[i][j]
        if step == 0:
            if known[i - 1] == heard[j - 1].text:
                matched[i - 1] = heard[j - 1]
            i, j = i - 1, j - 1
        elif step == 1:
            i -= 1
        else:
            j -= 1
    return matched


def timed_score(case, heard):
    matcher = runpy.run_path(str(HERE.with_name("qualify-kara-reference-timing.py")))
    parse, summarize = matcher["onset"], matcher["summarize"]
    directory = Path(case["annotation"])
    labels = (directory / "words.words.txt").read_text().splitlines()
    with (directory / "manual.csv").open(newline="") as stream:
        row_times = list(DictReader(stream))
    starts, ends = [], []
    for label, row in zip(labels, row_times, strict=True):
        tokens = words(label)
        starts.extend([parse(row["word_start"])] if len(tokens) == 1 else [None] * len(tokens))
        ends.extend([parse(row["word_end"])] if len(tokens) == 1 else [None] * len(tokens))
    observed = alignment(case["expected"], heard, starts)
    with (directory / "lines.csv").open(newline="") as stream:
        lines = list(DictReader(stream))
    line_starts, line_ends, predicted_start, predicted_end, position = [], [], [], [], 0
    for line in lines:
        tokens = words(line["lyrics_line"])
        if not tokens:
            continue
        if tokens != case["expected"][position:position + len(tokens)]:
            raise ValueError("Les annotations ligne et mot divergent")
        line_starts.append(parse(line["start_time"]))
        line_ends.append(parse(line["end_time"]))
        first, last = observed[position], observed[position + len(tokens) - 1]
        predicted_start.append(first.start if first else None)
        predicted_end.append(last.end if last else None)
        position += len(tokens)
    if position != len(starts):
        raise ValueError("Les annotations ligne ne couvrent pas tous les mots")
    spans = [(a, b) for row in row_times if (a := parse(row["word_start"])) is not None
             and (b := parse(row["word_end"])) is not None]
    gaps = [(left + .5, right - .5) for (_, left), (right, _) in zip(spans, spans[1:]) if right - left >= 2.]
    return dict(words=summarize(starts, [word.start if word else None for word in observed]),
                word_ends=summarize(ends, [word.end if word else None for word in observed]),
                lines=summarize(line_starts, predicted_start), line_ends=summarize(line_ends, predicted_end),
                words_in_annotated_gaps=sum(any(a <= w.start <= b for a, b in gaps) for w in heard))


def turbo_worker(case_path, stem_path, output, lexicon, model, variants):
    case, stem = read(case_path), read(stem_path)
    compare = runpy.run_path(str(HERE.with_name("qualify-kara-singing.py")))["lexical_errors"]
    emit(phase="model")
    began = monotonic()
    with closing(Preparer(PreparedCache(output.parent / "prepared", model),
                          threads=4, require_vulkan=True)) as worker:
        worker.settings()
        decoder = worker.load_decoder()
        model_seconds = monotonic() - began
        observations, scores, resumed = {}, {}, []
        receipts = output.parent / "observations"
        receipts.mkdir(mode=0o700, exist_ok=True)
        origin = case["start"]

        def evaluate(seen, language, seconds):
            scores = dict(**compare(case["expected"], [w.text for w in seen]),
                          **word_details(case["expected"], seen, ARGOT), language=language, decode_s=seconds)
            if case["annotation"] is not None:
                scores["independent_timing"] = timed_score(case, seen)
            return scores

        with ExitStack() as stack:
            emit(phase="pcm")
            if case["end"] is None:
                mix_count, mix = stack.enter_context(pcm_file(Path(case["path"])))
                voice_count, voice = stack.enter_context(pcm_file(Path(stem["vocals"])))
                if abs(mix_count - voice_count) > RATE // 20:
                    raise ValueError("Les pistes n’ont pas la même durée PCM")
            else:
                end, offset = case["end"], stem["offset"]
                m = pcm(case["path"], origin, end)
                v = pcm(stem["vocals"], origin - offset, end - offset)
                mix_count, voice_count = len(m) // 2, len(v) // 2
                if abs(mix_count - voice_count) > RATE // 50:
                    raise ValueError("Les extraits n’ont pas la même durée PCM")
                mix = lambda a, b: m[a * 2:b * 2]
                voice = lambda a, b: v[a * 2:b * 2]
            pcm_seconds = monotonic() - began - model_seconds
            window, beam = None, ()
            for variant in variants:
                if saved := restore_variant(receipts, variant):
                    language, seen, score, span, confirmation = saved
                    observations[variant], scores[variant] = (language, seen), score
                    resumed.append(variant)
                    if variant == "targeted":
                        window, beam = span, confirmation
                    continue
                emit(phase=variant)
                start = monotonic()
                if variant in ("mix", "vocals", "normalized", "filtered"):
                    fetch, count = (mix, mix_count) if variant == "mix" else (voice, voice_count)
                    if variant == "normalized":
                        fetch = lambda a, b: normalized(voice(a, b))
                    if variant == "filtered":
                        fetch = lambda a, b: highpass(voice(a, b))
                    if case["end"] is not None:
                        language, seen = observe(decoder, fetch(0, count), origin)
                    else:
                        language, seen = windowed(decoder, count, fetch, origin, adaptive=False)
                elif variant == "adaptive":
                    language, seen = windowed(decoder, voice_count, voice, origin, adaptive=True)
                elif variant == "targeted":
                    language, baseline = observations["vocals"]
                    _, support = observations["mix"]
                    seen, window, beam = reread(decoder, voice, voice_count, origin, language, baseline, support)
                else:
                    language, baseline = observations["mix"]
                    seen = consensus(baseline, observations["vocals"][1], observations["filtered"][1])
                elapsed = monotonic() - start
                observations[variant] = language, seen
                scores[variant] = evaluate(seen, language, elapsed)
                if variant in ("targeted", "combined"):
                    scores[variant]["changed"] = sum(a.text != b.text for a, b in zip(baseline, seen, strict=True))
                save_variant(receipts, variant, language, seen, scores[variant],
                             window=window if variant == "targeted" else None, beam=beam)
            if lexicon:
                emit(phase="phonetics")
                language, seen = observations["targeted"]
                if saved := restore_variant(receipts, "phonetics"):
                    scores["phonetics"] = saved[2]
                    resumed.append("phonetics")
                else:
                    began = monotonic()
                    corrected, attempted = seen, False
                    if window:
                        low, high = window
                        offset = origin + low / RATE
                        _, baseline = observations["vocals"]
                        near = [w for w in baseline if offset <= w.start < origin + high / RATE and w.confidence < .62]
                        with lexicon.open() as stream:
                            ranked = ranked_forms(map(json.loads, stream), genre=case["genre"],
                                                  language=language, single=True)
                        hint = prompts(ranked, near, language).get(5)
                        if hint and any(sound(w.text, language) for w in near):
                            _, hinted = observe(decoder, voice(low, high), offset, language=language, prompt=hint)
                            corrected = substitutions(seen, beam, hinted, language)
                            attempted = True
                    elapsed = monotonic() - began
                    scores["phonetics"] = dict(**evaluate(corrected, language, elapsed), attempted=attempted,
                                               changed=sum(a.text != b.text for a, b in zip(seen, corrected, strict=True)))
                    save_variant(receipts, "phonetics", language, corrected, scores["phonetics"])
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)
    result = dict(id=case["id"], genre=case["genre"], scores=scores,
                  previous=case.get("previous"), model_s=model_seconds, pcm_s=pcm_seconds,
                  child_rss_kib=usage.ru_maxrss, child_cpu_s=usage.ru_utime + usage.ru_stime, resumed=resumed)
    emit(complete=True, **result)


def aggregate(workspace):
    plan = read(workspace / "manifest.json")
    groups = {"tous": [case["id"] for case in plan["cases"]]}
    for case in plan["cases"]:
        groups.setdefault(case["genre"], []).append(case["id"])
    result = {}
    for group, ids in groups.items():
        done = [read(receipt)["events"][-1]["scores"] for identity in ids
                if (receipt := workspace / identity / "turbo.json").is_file()]
        variants = sorted({variant for item in done for variant in item})
        comparable = [item for item in done if all(name in item for name in plan.get("variants", ()))]
        metrics = {}
        for variant in variants:
            scores = [row[variant] for row in comparable if variant in row]
            if not scores:
                continue
            total = {key: sum(s[key] for s in scores if key in s) for key in (
                     "expected", "errors", "substitutions", "deletions", "insertions", "characters",
                     "character_errors", "decode_s", "uncertain_words", "uncertain_passages", "changed")}
            metrics[variant] = dict(**total, count=len(scores),
                                    wer=total["errors"] / total["expected"] if total["expected"] else None,
                                    cer=total["character_errors"] / total["characters"] if total["characters"] else None)
        result[group] = dict(planned=len(ids), complete=len(comparable), scores=metrics)
    return result


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--library", type=Path)
    parser.add_argument("--annotations", type=Path)
    parser.add_argument("--lexicon", action="store_true", help="Tester aussi le lexique historique hors évaluations")
    parser.add_argument("--profile", choices=("power-saver", "balanced", "performance"))
    parser.add_argument("--memory-psi", type=float, choices=(1., 2., 3.), default=1.,
                        help="Le comparatif manuel au-dessus de 1 %% conserve une réserve de 4 Gio")
    parser.add_argument("--model", type=Path, default=MODEL)
    parser.add_argument("--engine", choices=("upstream", "serial"), default="serial")
    parser.add_argument("--segment", type=int, choices=(4, 5, 6, 7), default=7)
    parser.add_argument("--keep-instrumental", action="store_true", help="Garder la seconde piste pour un A/B Demucs")
    parser.add_argument("--stems-from", type=Path, help="Relire les séparations vérifiées d’un essai précédent")
    parser.add_argument("--variants", choices=VARIANTS, nargs="+", default=VARIANTS)
    parser.add_argument("--phase", choices=("demucs", "turbo", "both"), default="both")
    parser.add_argument("--case", action="append")
    parser.add_argument("--keep-going", action="store_true", help="Reporter une piste interrompue par PSI ou température")
    parser.add_argument("--summary", action="store_true", help="Lire les résultats sans lancer de modèle")
    args = parser.parse_args()
    if args.summary:
        print(json.dumps(aggregate(args.workspace), ensure_ascii=False, indent=2, allow_nan=False))
        return
    if (args.annotations is None and not all((args.previous, args.index, args.library))
            or args.annotations is not None and (args.previous or args.index or args.library or args.lexicon)):
        parser.error("Indiquer --previous, --index et --library, ou bien --annotations")
    args.workspace = args.workspace.resolve()
    if sys.byteorder != "little":
        parser.error("Le banc PCM exige un hôte petit-boutiste")
    phases = set(args.variants)
    if ("targeted" in phases and not {"mix", "vocals"} <= phases
            or "combined" in phases and not {"mix", "vocals", "filtered"} <= phases
            or args.lexicon and "targeted" not in phases):
        parser.error("Les reprises exigent les hypothèses de base")
    args.variants = [name for name in VARIANTS if name in phases]
    source_root = (args.annotations or args.library).resolve(strict=True)
    if args.workspace.is_relative_to(source_root):
        parser.error("La sortie doit se trouver hors bibliothèque")
    os.umask(0o077)
    args.workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if args.workspace.stat().st_mode & 0o077:
        parser.error("Utiliser un dossier d’essai privé")
    cases = read_cases(args)
    for case in cases:
        if not case["id"].isascii() or not case["id"].replace("-", "").isalnum():
            raise ValueError("Identifiant de corpus invalide")
        case["audio_sha256"] = digest(Path(case["path"]))
        case.setdefault("annotation", None)
    selected = [case for case in cases if not args.case or case["id"] in args.case]
    if not selected or args.case and len(selected) != len(set(args.case)):
        parser.error("Cas inconnus ou absents")
    lexicon = args.previous / "corpus.jsonl" if args.lexicon else None
    checkpoint = digest(CHECKPOINT)
    if args.stems_from:
        args.stems_from = args.stems_from.resolve(strict=True)
        prior = read(args.stems_from / "manifest.json")
        if (args.keep_instrumental or args.stems_from == args.workspace or prior["cases"] != cases
                or prior["checkpoint"] != checkpoint or prior["engine"] != args.engine
                or prior.get("segment", 7) != args.segment):
            parser.error("Les séparations sources ne couvrent pas le même corpus ou moteur")
    paths = [HERE, HERE.with_name("_parole_separation_metrics.py"),
             HERE.with_name("_parole_qualification_receipts.py"), SOURCE]
    paths.extend(SOURCE.parents[2] / "bin" / name
                 for name in ("_kara_turbo.py", "_parole_acoustic.py", "_parole_lexicon.py"))
    plan = dict(cases=cases, weights=digest(args.model), checkpoint=checkpoint,
                code={path.name: digest(path) for path in paths}, variants=args.variants,
                profile=args.profile, memory_psi=args.memory_psi, engine=args.engine,
                segment=args.segment,
                keep_instrumental=args.keep_instrumental,
                lexicon=digest(lexicon) if lexicon else None)
    with lease(args.workspace / "campaign.lock", wait=False) as owner:
        if owner is None:
            raise RuntimeError("Qualification déjà en cours")
        manifest = args.workspace / "manifest.json"
        if manifest.exists() and read(manifest) != plan:
            raise ValueError("Le corpus, le modèle ou le protocole ont changé depuis le départ")
        write(manifest, plan)
        deferred = 0
        for i, case in enumerate(selected, 1):
            folder = args.workspace / case["id"]
            folder.mkdir(exist_ok=True)
            print(f"{i}/{len(selected)} · {case['id']} · {case['genre']}", flush=True)
            case_path = folder / "case.json"
            write(case_path, case)
            try:
                if args.phase != "turbo":
                    prepare_stem(args, case, folder)
                out = folder / "turbo.json"
                if args.phase != "demucs" and not out.exists():
                    separation = folder / "demucs.json"
                    saved = read(separation)
                    if digest(Path(saved["vocals"])) != saved["sha256"]:
                        raise ValueError("Piste vocale modifiée entre deux exécutions")
                    report = guarded(args, [sys.executable, "-B", str(HERE), "--turbo-worker",
                                            str(case_path), str(separation), str(out),
                                            str(lexicon.resolve()) if lexicon else "", str(args.model.resolve()),
                                            *args.variants], out)
                    write(out, report)
                    print(json.dumps(report["events"][-1]["scores"], ensure_ascii=False), flush=True)
            except Interrupted as error:
                if not args.keep_going or not str(error).startswith(("Pression mémoire", "Limite thermique")):
                    raise
                deferred += 1
                print(f"Report · {error}", flush=True)
        if deferred:
            raise Interrupted(f"{deferred} cas reportés pour ressources")


if __name__ == "__main__":
    def cancel(*_):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancel)
    try:
        if sys.argv[1:2] == ["--demucs-worker"]:
            demucs_worker(*sys.argv[2:])
        elif sys.argv[1:2] == ["--turbo-worker"]:
            case, stem, output, lexicon, model, *variants = sys.argv[2:]
            turbo_worker(Path(case), Path(stem), Path(output), Path(lexicon) if lexicon else None, Path(model), variants)
        else:
            main()
    except Interrupted as error:
        raise SystemExit(str(error)) from None
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        if sys.argv[1:2] in (["--turbo-worker"], ["--demucs-worker"]):
            emit(failure=str(error))
        raise SystemExit(str(error)) from None
