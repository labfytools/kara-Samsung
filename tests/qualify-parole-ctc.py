#!/usr/bin/env python3
"""Banc hors ligne de repères CTC sur des paroles anglaises connues."""
from argparse import ArgumentParser
from csv import DictReader
import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import NamedTemporaryFile
from time import monotonic, sleep, time_ns

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import RATE, digest, pcm_file
from _kara_words import words
from _parole_process import Interrupted, run

HOP = 320
KERNEL = 400
CORE = RATE * 16
PAD = RATE * 2
GATES = (0, .20, .40, .60, .80)


def infer(model, source, checkpoint, symbols):
    import numpy as np
    import onnxruntime as ort

    session, input_name = None, None
    chunks, reused = [], 0
    with pcm_file(source) as (size, fetch):
        if not 1 <= size <= RATE * 3600:
            raise ValueError("Durée PCM hors limites")
        for left in range(0, size, CORE):
            right = min(left + CORE, size)
            start, end = max(0, left - PAD), min(size, right + PAD)
            centers = start + np.arange((end - start - KERNEL) // HOP + 1) * HOP + KERNEL // 2
            mask = (centers >= left) & (centers < right)
            receipt = checkpoint / f"{left:09d}.npy"
            if receipt.exists():
                data = np.load(receipt, allow_pickle=False)
                if data.shape != (int(mask.sum()), symbols) or data.dtype != np.float32 or not np.isfinite(data).all():
                    raise ValueError("Émissions CTC de reprise invalides")
                chunks.append(data)
                reused += 1
                continue
            if session is None:
                policy = ort.SessionOptions()
                policy.intra_op_num_threads = policy.inter_op_num_threads = 1
                session = ort.InferenceSession(str(model), policy, providers=["CPUExecutionProvider"])
                (input_node,) = session.get_inputs()
                input_name = input_node.name
            signal = np.frombuffer(fetch(start, end), dtype="<i2").astype(np.float32) / 32768
            signal = (signal - signal.mean()) / np.sqrt(signal.var() + 1e-7)
            (logits,) = session.run(None, {input_name: signal[None, :]})
            logits = logits[0]
            if logits.shape != (len(mask), symbols) or not np.isfinite(logits).all():
                raise ValueError("Horloge ou alphabet du modèle CTC inattendu")
            usable = logits[mask]
            logp = usable - usable.max(axis=1, keepdims=True)
            logp -= np.log(np.exp(logp).sum(axis=1, keepdims=True))
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


def viterbi(logp, tokens, blank):
    import numpy as np

    if not tokens or len(tokens) > len(logp):
        raise ValueError("Référence CTC hors limites")
    labels = np.full(2 * len(tokens) + 1, blank)
    labels[1::2] = tokens
    skip = (labels[2:] != blank) & (labels[2:] != labels[:-2])
    # One byte per predecessor bounds this prototype at 200 million cells.
    # A forced path proposes alignments, including for words never sung.
    if len(labels) * len(logp) > 200_000_000:
        raise ValueError("Trellis CTC trop volumineux")
    causes = np.empty((len(logp), len(labels)), dtype=np.uint8)
    score = np.full(len(labels), -np.inf, dtype=np.float32)
    score[0] = 0
    for frame, likelihood in enumerate(logp):
        best = score.copy()
        cause = causes[frame]
        cause.fill(0)
        improve = score[:-1] > best[1:]
        best[1:][improve] = score[:-1][improve]
        cause[1:][improve] = 1
        improve = skip & (score[:-2] > best[2:])
        best[2:][improve] = score[:-2][improve]
        cause[2:][improve] = 2
        score = best + likelihood[labels]
    state = len(labels) - (2 if score[-2] > score[-1] else 1)
    if not np.isfinite(score[state]):
        raise ValueError("Aucun chemin CTC complet")
    spans = [[] for _ in tokens]
    for frame in range(len(logp) - 1, -1, -1):
        if state % 2:
            spans[state // 2].append(frame)
        state -= int(causes[frame, state])
    return spans


def predict(logp, vocabulary, target):
    import numpy as np

    blank = vocabulary["<pad>"]
    text = "|".join(target).upper()
    tokens = [vocabulary[character] for character in text]
    spans = viterbi(logp, tokens, blank)
    position = 0
    output = []
    for word in target:
        indices = range(position, position + len(word))
        # Retain the weakest character's best acoustic posterior. This is a
        # rejection signal, not a calibrated probability for sung words.
        character_scores = [float(logp[spans[i], tokens[i]].max()) for i in indices]
        minimum = float(np.exp(min(character_scores)))
        mean = float(np.exp(sum(character_scores) / len(character_scores)))
        start = (spans[position][-1] * HOP + KERNEL // 2 - HOP // 2) / RATE
        end = (spans[position + len(word) - 1][0] * HOP + KERNEL // 2 + HOP // 2) / RATE
        output.append((start, end, minimum, mean))
        position += len(word) + 1
    return output


def worker(model, vocab, case_path, source, checkpoint):
    case = json.loads(case_path.read_bytes())
    annotations = Path(case["annotation"])
    vocabulary = json.loads(vocab.read_bytes())
    matcher = runpy.run_path(str(HERE.with_name("qualify-kara-reference-timing.py")))
    parse, summarize = matcher["onset"], matcher["summarize"]
    target = case["expected"]
    labels = (annotations / "words.words.txt").read_text().splitlines()
    with (annotations / "manual.csv").open(newline="") as stream:
        rows = list(DictReader(stream))
    if target != [word for label in labels for word in words(label)]:
        raise ValueError("Référence modifiée")
    starts, ends = [], []
    for label, row in zip(labels, rows, strict=True):
        count = len(words(label))
        starts.extend([parse(row["word_start"])] if count == 1 else [None] * count)
        ends.extend([parse(row["word_end"])] if count == 1 else [None] * count)
    lines = []
    position = 0
    with (annotations / "lines.csv").open(newline="") as stream:
        for line in DictReader(stream):
            expected = words(line["lyrics_line"])
            if not expected:
                continue
            right = position + len(expected)
            if expected != target[position:right]:
                raise ValueError("Les annotations ligne et mot divergent")
            lines.append((position, right - 1, parse(line["start_time"]), parse(line["end_time"])))
            position = right
    if position != len(target):
        raise ValueError("La référence ligne est incomplète")
    began = monotonic()
    logp, reused = infer(model, source, checkpoint, len(vocabulary))
    emission_s = monotonic() - began
    hypotheses = predict(logp, vocabulary, target)
    # Deliberately corrupt eight evenly spaced words. Their positions do not
    # influence thresholds or normal decoding and act as a negative control.
    altered = target.copy()
    negative = range(19, len(target), max(1, len(target) // 8))
    for i in negative:
        altered[i] = "xylophone"
    corrupted = predict(logp, vocabulary, altered)
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
    checkpoint = args.destination.resolve().with_suffix(".windows")
    checkpoint.mkdir(mode=0o700, exist_ok=True)
    manifest = checkpoint / "manifest.json"
    command = ["bwrap", "--unshare-net", "--die-with-parent", "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc",
               "--tmpfs", "/tmp", "--bind", str(checkpoint), str(checkpoint),
               str(args.worker_python.absolute()), "-B", str(HERE), "--worker", str(args.model), str(args.vocab),
               str(args.case), str(source), str(checkpoint)]

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
        worker(*map(Path, sys.argv[2:]))
    else:
        main()
