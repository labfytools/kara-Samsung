#!/usr/bin/env python3
"""Banc hors ligne de repères CTC sur des paroles anglaises ou françaises connues."""
from argparse import ArgumentParser
from collections import Counter
from csv import DictReader
import json
import os
from pathlib import Path
import re
import runpy
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep, time_ns
from unicodedata import normalize

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import RATE, digest, pcm_file
from _kara_words import words
from _parole_process import Interrupted, run

from _kara_ctc import CORE, HOP, KERNEL, cpu_session, emissions, predict, window_grid

GATES = (0, .20, .40, .60, .80)
FRENCH_WORDS = re.compile(r"[^\W_]+(?:['-][^\W_]+)*")
FRENCH_PUNCTUATION = str.maketrans({"’": "'", "‘": "'", "‐": "-", "‑": "-"})


def infer(model, source, checkpoint, symbols):
    import numpy as np
    runtime = None
    chunks, reused = [], 0
    with pcm_file(source) as (size, fetch):
        if not 1 <= size <= RATE * 3600:
            raise ValueError("Durée PCM hors limites")
        for left in range(0, size, CORE):
            start, end, mask = window_grid(size, left)
            receipt = checkpoint / f"{left:09d}.npy"
            if receipt.exists():
                data = np.load(receipt, allow_pickle=False)
                if data.shape != (int(mask.sum()), symbols) or data.dtype != np.float32 or not np.isfinite(data).all():
                    raise ValueError("Émissions CTC de reprise invalides")
                chunks.append(data)
                reused += 1
                continue
            if runtime is None:
                runtime = cpu_session(model)
            logp = emissions(runtime, fetch(start, end), mask, symbols)
            # A completed window stands alone. An interruption may leave a
            # temporary file but cannot make it visible as a complete window.
            with NamedTemporaryFile(dir=checkpoint, prefix=".ctc-", delete=False) as pending:
                draft = Path(pending.name)
                try:
                    np.save(pending, logp, allow_pickle=False)
                    pending.flush()
                    os.fsync(pending.fileno())
                    os.replace(draft, receipt)
                finally:
                    draft.unlink(missing_ok=True)
            chunks.append(logp)
    return np.concatenate(chunks), reused


def reference_units(labels, rows, target, parse, language):
    units, starts, ends, mapping = [], [], [], []
    for label, row in zip(labels, rows, strict=True):
        tokens = words(label)
        if not tokens:
            continue
        start, end = parse(row["word_start"]), parse(row["word_end"])
        if language == "fr":
            # Preserve trained graphemes and the single acoustic span of a
            # contraction. Kara's normalized count still checks line order.
            form = "|".join(FRENCH_WORDS.findall(normalize("NFC", label).casefold().translate(FRENCH_PUNCTUATION)))
            mapping.extend([len(units)] * len(tokens))
            units.append(form)
            starts.append(start)
            ends.append(end)
        else:
            mapping.extend(range(len(units), len(units) + len(tokens)))
            units.extend(tokens)
            starts.extend([start] if len(tokens) == 1 else [None] * len(tokens))
            ends.extend([end] if len(tokens) == 1 else [None] * len(tokens))
    if len(mapping) != len(target) or not all(units):
        raise ValueError("Unités CTC de la référence invalides")
    return units, starts, ends, mapping


def worker(model, vocab, case_path, source, checkpoint, language):
    case = json.loads(case_path.read_bytes())
    annotations = Path(case["annotation"])
    vocabulary = json.loads(vocab.read_bytes())
    matcher = runpy.run_path(str(HERE.with_name("qualify-kara-reference-timing.py")))
    parse, summarize = matcher["onset"], matcher["summarize"]
    normalized = case["expected"]
    labels = (annotations / "words.words.txt").read_text().splitlines()
    with (annotations / "manual.csv").open(newline="") as stream:
        rows = list(DictReader(stream))
    if normalized != [word for label in labels for word in words(label)]:
        raise ValueError("Référence modifiée")
    target, starts, ends, mapping = reference_units(labels, rows, normalized, parse, language)
    if language == "fr" and not set("".join(target)) <= vocabulary.keys():
        raise ValueError("Graphèmes français hors alphabet du modèle")
    counts = Counter(mapping)
    # The historical Turbo timing test excludes linked subtokens. Keep the
    # same denominator available alongside all original French word spans.
    comparable_starts = [start if counts[i] == 1 else None for i, start in enumerate(starts)]
    lines = []
    position = 0
    with (annotations / "lines.csv").open(newline="") as stream:
        for line in DictReader(stream):
            expected = words(line["lyrics_line"])
            if not expected:
                continue
            right = position + len(expected)
            if expected != normalized[position:right]:
                raise ValueError("Les annotations ligne et mot divergent")
            lines.append((mapping[position], mapping[right - 1], parse(line["start_time"]), parse(line["end_time"])))
            position = right
    if position != len(normalized):
        raise ValueError("La référence ligne est incomplète")
    began = monotonic()
    logp, reused = infer(model, source, checkpoint, len(vocabulary))
    emission_s = monotonic() - began
    hypotheses = predict(logp, vocabulary, target, language=language)
    # Deliberately corrupt eight evenly spaced words. Their positions do not
    # influence thresholds or normal decoding and act as a negative control.
    altered = target.copy()
    negative = range(19, len(target), max(1, len(target) // 8))
    for i in negative:
        altered[i] = "xylophone"
    corrupted = predict(logp, vocabulary, altered, language=language)
    results = {}
    for name, metric in (("minimum", 2), ("moyenne_geometrique", 3)):
        metrics = results[name] = {}
        for threshold in GATES:
            first = [w[0] if w[metric] >= threshold else None for w in hypotheses]
            last = [w[1] if w[metric] >= threshold else None for w in hypotheses]
            metrics[str(threshold)] = {
                "words": summarize(starts, first), "word_ends": summarize(ends, last),
                "lines": summarize([a for _, _, a, _ in lines], [first[i] for i, *_ in lines]),
                "line_ends": summarize([b for _, _, _, b in lines], [last[j] for _, j, *_ in lines]),
                "corrupted_accepted": sum(corrupted[i][metric] >= threshold for i in negative),
            }
            if language == "fr":
                metrics[str(threshold)]["words_shared_with_turbo"] = summarize(comparable_starts, first)
    print(json.dumps(dict(complete=True, seconds=monotonic() - began, emission_s=emission_s, frames=len(logp),
                          negatives=len(negative), windows_reused=reused, results=results), allow_nan=False), flush=True)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("case", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument("model", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument("vocab", type=lambda value: Path(value).resolve(strict=True))
    parser.add_argument("worker_python", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--vocals", action="store_true")
    parser.add_argument("--language", choices=("en", "fr"), default="en")
    args = parser.parse_args()
    os.umask(0o077)
    case = json.loads(args.case.read_bytes())
    audio = Path(case["path"])
    if not case["annotation"] or case["end"] is not None or digest(audio) != case["audio_sha256"]:
        raise ValueError("Le banc exige une chanson complète et des annotations indépendantes signées")
    directory = Path(case["annotation"])
    if [digest(directory / name) for name in ("words.words.txt", "manual.csv", "lines.csv")] != case["reference_hashes"]:
        raise ValueError("Les annotations diffèrent du manifeste")
    source = audio
    if args.vocals:
        stem = json.loads(args.case.with_name("demucs.json").read_bytes())
        source = Path(stem["vocals"])
        if stem["offset"] or digest(source) != stem["sha256"]:
            raise ValueError("Le stem ne correspond pas à la chronologie du morceau")
    workspace = args.destination.resolve().parent
    if workspace.stat().st_mode & 0o077 or args.destination.exists():
        raise ValueError("Choisir un nouveau reçu dans un dossier privé")
    bench = runpy.run_path(str(HERE.with_name("qualify-parole-separation.py")))
    Monitor = runpy.run_path(str(HERE.with_name("benchmark-kara-preparation.py")))["Monitor"]
    monitor = Monitor(workspace, audio.parent, None, 1.0)
    monitor.login = bench["Login"]()
    events, reason = [], None
    signature = {"model": digest(args.model), "source": digest(source), "case": digest(args.case),
                 "tool": digest(HERE), "vocabulary": digest(args.vocab)}
    if args.language != "en":
        signature["language"] = args.language
    checkpoint = args.destination.resolve().with_suffix(".windows")
    checkpoint.mkdir(mode=0o700, exist_ok=True)
    manifest = checkpoint / "manifest.json"
    command = ["bwrap", "--unshare-net", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               "--tmpfs", "/tmp", "--bind", str(checkpoint), str(checkpoint),
               str(args.worker_python.absolute()), "-B", str(HERE), "--worker", str(args.model), str(args.vocab),
               str(args.case), str(source), str(checkpoint), args.language]

    def stop():
        count = len(monitor.samples)
        interrupted = monitor.stop()
        if len(monitor.samples) != count:
            workers = bench["process_tree"](os.getpid())
            monitor.samples[-1]["group_pss_kib"] = sum(p.get("Pss", 0) for p in workers)
            if monitor.login.sleeping():
                monitor.reason = "Mise en veille demandée"
        return interrupted or bool(monitor.reason)

    with bench["lease"](checkpoint / "campaign.lock", wait=False) as owner:
        if owner is None or args.destination.exists():
            raise ValueError("Le dossier d’émissions CTC est déjà utilisé ou terminé")
        if manifest.exists() and json.loads(manifest.read_bytes()) != signature:
            raise ValueError("La signature des émissions CTC a changé")
        bench["write"](manifest, signature)
        try:
            deadline = monotonic() + 45
            while True:
                blocked = stop()
                last = monitor.samples[-1] if monitor.samples else {}
                if (not blocked and last["memory_available_kib"] >= 4096 * 1024
                        and last["cpu_c"] <= 74 and last["memory_psi_some_avg10_pct"] <= .5):
                    break
                if (monotonic() >= deadline or monitor.reason and
                        not monitor.reason.startswith(("Limite thermique", "Pression mémoire"))):
                    raise Interrupted(monitor.reason or "Réserve thermique ou mémoire d’admission")
                sleep(1.1)
                monitor.reason = None
            code, _ = run(command, stop=stop, event=events.append, timeout=420, maximum=32768,
                          env=dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", TMPDIR="/tmp"),
                          retain=(owner,))
            if code or len(events) != 1 or not events[0].get("complete"):
                raise ValueError(f"Échec du moteur CTC ({code})")
        except (Interrupted, TimeoutError) as error:
            reason = monitor.reason or str(error)
        report = {**signature, "reason": reason, "result": events[0] if events else None, "samples": monitor.samples}
        destination = args.destination.with_suffix(f".{time_ns()}.interrupted.json") if reason else args.destination
        bench["write"](destination, report)
    print(reason or f"Chanson complète · {events[0]['seconds']:.1f} s")


if __name__ == "__main__":
    if sys.argv[1:2] == ["--worker"]:
        model, vocab, case, audio, checkpoint, language = sys.argv[2:]
        worker(Path(model), Path(vocab), Path(case), Path(audio), Path(checkpoint), language)
    else:
        main()
