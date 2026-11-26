#!/usr/bin/env python3
"""Compare Qwen avec les extraits et les références figés d’Omega I."""
from argparse import ArgumentParser
from collections import Counter
from math import isfinite
import os
from pathlib import Path
import re
import runpy
import sys
from tempfile import TemporaryDirectory
from time import monotonic, sleep, time_ns
import wave

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import RATE, digest
from _kara_words import words
from _parole_process import Interrupted, run
from _parole_separation_metrics import edit_distance, process_tree

RADV = Path("/usr/share/vulkan/icd.d/radeon_icd.json")


def main():
    cli = ArgumentParser(description=__doc__)
    for name in ("baseline", "workspace", "build", "model"):
        cli.add_argument(name, type=lambda value: Path(value).resolve(strict=True))
    cli.add_argument("--library", type=Path, required=True)
    cli.add_argument("--per-genre", type=int, help="Premier N extrait(s) par genre dans l’ordre du manifeste")
    cli.add_argument("--input", choices=("mix", "vocals"), default="mix")
    cli.add_argument("--backend", choices=("cpu", "vulkan"), default="cpu")
    cli.add_argument("--threads", type=int, choices=(1, 2), default=1)
    args = cli.parse_args()
    if args.per_genre is not None and args.per_genre < 1:
        cli.error("--per-genre doit être positif")
    if args.backend == "vulkan" and not RADV.is_file():
        cli.error("Le pilote Vulkan RADV est requis pour le comparatif AMD")
    os.umask(0o077)
    if args.workspace.stat().st_mode & 0o077:
        cli.error("le dossier d’essai doit être privé (0700)")

    bench = runpy.run_path(str(HERE.with_name("qualify-parole-separation.py")))
    monitor_type = runpy.run_path(str(HERE.with_name("benchmark-kara-preparation.py")))["Monitor"]
    compare = runpy.run_path(str(HERE.with_name("qualify-kara-singing.py")))["lexical_errors"]
    read, write = bench["read"], bench["write"]
    corpus = args.baseline / "manifest.json"
    binary = args.build / "bin/transcribe-cli"
    cgroup = Path("/sys/fs/cgroup") / Path("/proc/self/cgroup").read_text().strip().split(":")[-1].lstrip("/")
    cpu_max = (cgroup / "cpu.max").read_text().strip() if (cgroup / "cpu.max").exists() else None
    signature = dict(corpus=digest(corpus), model=digest(args.model), binary=digest(binary),
                     script=digest(HERE), input=args.input, backend=args.backend, threads=args.threads, cpu_max=cpu_max,
                     vulkan_driver=digest(RADV) if args.backend == "vulkan" else None)
    results = args.workspace / args.input
    results.mkdir(mode=0o700, exist_ok=True)
    plan = results / "engine.json"
    if plan.exists() and read(plan) != signature:
        raise ValueError("Le moteur ou le corpus diffère du reçu d’essai")
    write(plan, signature)

    counts = Counter()
    for case in read(corpus)["cases"]:
        genre, identity = case["genre"], case["id"]
        counts[genre] += 1
        if args.per_genre is not None and counts[genre] > args.per_genre:
            continue
        if not identity.isascii() or not identity.isalnum():
            raise ValueError("Identifiant d’extrait invalide")
        start, end = case["start"], case["end"]
        if end is None or not (isfinite(start) and isfinite(end) and 0 <= start < end <= start + 30):
            raise ValueError("Le banc attend un extrait de 30 secondes au plus")
        output = results / f"{identity}.json"
        if output.exists():
            continue
        source = Path(case["path"]).resolve(strict=True)
        if not source.is_relative_to(args.library.resolve()) or digest(source) != case["audio_sha256"]:
            raise ValueError("Audio hors bibliothèque ou différent du manifeste")
        original = args.baseline / identity
        baseline = read(original / "turbo.json")["events"][-1]["scores"][args.input]
        if baseline["expected"] != len(case["expected"]):
            raise ValueError("Référence et mesure historique incohérentes")
        if args.input == "vocals":
            stem = read(original / "demucs.json")
            source = Path(stem["vocals"])
            if digest(source) != stem["sha256"]:
                raise ValueError("Piste vocale différente du reçu historique")
            start, end = start - stem["offset"], end - stem["offset"]

        monitor = monitor_type(args.workspace, args.library, None, 1.0)
        monitor.login = bench["Login"]()

        def stop():
            count = len(monitor.samples)
            interrupted = monitor.stop()
            if len(monitor.samples) != count:
                processes = process_tree(os.getpid())
                monitor.samples[-1].update(
                    group_rss_kib=sum(p.get("VmRSS", 0) for p in processes),
                    group_pss_kib=sum(p.get("Pss", 0) for p in processes))
                if monitor.login.sleeping():
                    monitor.reason = "Mise en veille demandée"
            return interrupted or bool(monitor.reason)

        try:
            # Admission leaves headroom for a rising temperature or PSI.
            # The run still obeys every stop condition of the original guard.
            admission = monotonic() + 30
            while True:
                interrupted = stop()
                last = monitor.samples[-1] if monitor.samples else {}
                if (not interrupted and last["memory_available_kib"] >= 4096 * 1024
                        and last["cpu_c"] <= 74 and last["memory_psi_some_avg10_pct"] <= .5):
                    break
                if (monotonic() >= admission or monitor.reason and
                        not monitor.reason.startswith(("Limite thermique", "Pression mémoire"))):
                    raise Interrupted(monitor.reason or "Réserve mémoire ou thermique d’admission")
                sleep(1.1)
                monitor.reason = None
            with TemporaryDirectory(dir=results) as directory:
                clip = Path(directory) / "clip.wav"
                with wave.open(str(clip), "wb") as wav:
                    wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
                    wav.writeframes(bench["pcm"](source, start, end))
                # RADV needs the read-only DRM topology as well as the
                # device nodes. Without sysfs the loader found only llvmpipe.
                devices = []
                if args.backend == "vulkan":
                    devices.extend(("--dev-bind", "/dev/dri", "/dev/dri"))
                    for sysfs in ("/sys/devices", "/sys/class/drm", "/sys/dev/char"):
                        devices.extend(("--ro-bind", sysfs, sysfs))
                command = ["nice", "-n", "12", "bwrap", "--unshare-net", "--die-with-parent",
                           "--ro-bind", "/usr", "/usr", "--symlink", "usr/lib", "/lib",
                           "--symlink", "usr/lib", "/lib64", "--dev", "/dev", *devices, "--proc", "/proc",
                           "--ro-bind", str(args.build), "/engine", "--ro-bind", str(args.model), "/model.gguf",
                           "--ro-bind", str(clip), "/audio.wav", "/engine/bin/transcribe-cli", "--quiet",
                           "--threads", str(args.threads), "--backend", args.backend, "-m", "/model.gguf", "/audio.wav"]
                began = monotonic()
                env = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS=str(args.threads))
                if args.backend == "vulkan":
                    env["VK_DRIVER_FILES"] = str(RADV)
                code, raw = run(command, stop=stop, timeout=90, maximum=65536, env=env)
                elapsed = monotonic() - began
            report = raw.decode()
            backend = re.search(r"^  backend:\s+([^\r\n]+)", report, re.MULTILINE)
            selected = backend.group(1).strip() if backend else ""
            _, marker, transcript = report.partition("\ntext: ")
            if code or not marker or "\nrun: ok\n" not in report:
                raise ValueError(f"Le moteur {args.backend} a échoué ({code})")
            if args.backend == "vulkan" and "vulkan" not in selected.casefold():
                raise ValueError("Vulkan n’a pas été réellement sélectionné par le moteur")
            transcript = transcript.rsplit("\n  realtime:", 1)[0].strip()
            observed = [] if transcript == "(empty)" else words(transcript)
            target = case["expected"]
            chars, heard = " ".join(target), " ".join(observed)
            scores = dict(**compare(target, observed), characters=len(chars),
                          character_errors=edit_distance(chars, heard))
            write(output, dict(genre=genre, candidate=scores, baseline=baseline, elapsed=elapsed, backend=selected,
                               resources=bench["summarize_resources"](monitor.samples), samples=monitor.samples))
            print(f"{genre}: {scores['errors']}/{len(target)} en {elapsed:.2f} s", flush=True)
        except (Interrupted, TimeoutError) as error:
            write(results / f"{identity}.{time_ns()}.interrupted.json",
                  dict(genre=genre, reason=monitor.reason or str(error), samples=monitor.samples))
            print(f"{genre}: report · {monitor.reason or error}", flush=True)


if __name__ == "__main__":
    main()
