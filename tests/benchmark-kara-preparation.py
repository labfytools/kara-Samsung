#!/usr/bin/env python3
"""Banc hors ligne de préparation Kara. Les publications restent dans le dossier d'essai."""

import argparse
from collections import defaultdict
from contextlib import contextmanager, nullcontext
from functools import wraps
import json
import os
from pathlib import Path
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import sys
from time import monotonic, sleep, time_ns

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "bin"))

import _kara_prepare as prepare
import _kara_reference as reference
import _kara_refine as refine
import _kara_stems as stems
import _kara_turbo as turbo
from _kara_import_resources import lease
from _kara_paths import music_library
from _parole_night import LOGIN, MANAGER, Login, PROFILES, PROFILES_PATH, mains_online
from _parole_process import Interrupted, run
from _parole_resources import block_device, multimedia_active

GIB = 2**30
STEM_ROOT = stems.ROOT
CASES = REPO / "tests/fixtures/preparation-cases.json"


def command(*args):
    return subprocess.check_output(args, text=True, timeout=5).strip()


def read_sample(path, divisor=1):
    try:
        return int(path.read_text()) / divisor
    except (OSError, ValueError):
        return None


def record(path, data):
    prepare.atomic(path, (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode())


class Monitor:
    def __init__(self, workspace, library, profile, max_psi=1.0, *, closed_lid=False):
        self.devices = {block_device(path) for path in (workspace, library)}
        self.workspace, self.library = workspace, library
        self.profile = profile
        self.max_psi = max_psi
        self.login = Login() if profile or closed_lid else None
        self.closed_lid = closed_lid
        self.cgroup = Path("/sys/fs/cgroup") / command(
            "systemctl", "--user", "show", "kara.service", "-p", "ControlGroup", "--value").lstrip("/")
        self.sensors = {p.joinpath("name").read_text().strip(): p
                        for p in Path("/sys/class/hwmon").glob("hwmon*")}
        self.gpu = next(Path("/sys/class/drm").glob("card[0-9]*/device/gpu_busy_percent")).parent
        self.lid = next(Path("/proc/acpi/button/lid").glob("*/state"))
        self.next = 0
        self.start = monotonic()
        self.samples = []
        self.reason = None

    def stop(self):
        now = monotonic()
        if now < self.next:
            return bool(self.reason)
        self.next = now + 1
        try:
            self.sample(now)
        except (OSError, ValueError, KeyError, subprocess.SubprocessError) as error:
            self.reason = f"Mesure indisponible ({type(error).__name__})"
        return bool(self.reason)

    def sample(self, now):
        cpu_temp = read_sample(self.sensors["k10temp"] / "temp1_input", 1000)
        gpu_temp = read_sample(self.sensors["amdgpu"] / "temp1_input", 1000)
        lid_state = self.lid.read_text().split()[-1]
        expected_lid = "closed" if self.closed_lid else "open"
        # Preserve Parole's stricter cap when closed. A change of posture
        # ends the run instead of silently mixing thermal conditions.
        cpu_limit, gpu_limit = (70, 70) if self.closed_lid else (78, 75)
        if lid_state != expected_lid or mains_online() is not True:
            self.reason = "Position du capot modifiée ou alimentation hors secteur"
        elif (cpu_temp is None or gpu_temp is None
              or not (0 < cpu_temp < cpu_limit and 0 < gpu_temp < gpu_limit)):
            self.reason = f"Limite thermique ({cpu_temp} °C CPU, {gpu_temp} °C GPU)"
        mem = {key: int(value.split()[0]) for key, value in (
            line.split(":", 1) for line in Path("/proc/meminfo").read_text().splitlines())}
        pressure = float(Path("/proc/pressure/memory").read_text().split()[1].split("=")[1])
        # The manual comparator can tolerate brief reclaim activity while
        # enforcing a larger available-memory floor than the night worker.
        memory_floor = (1536 if self.max_psi == 1 else 4096) * 1024
        if mem["MemAvailable"] < memory_floor or pressure >= self.max_psi:
            self.reason = "Pression mémoire"
        busy = False
        procs = self.cgroup / "cgroup.procs"
        if procs.is_file():
            for pid in procs.read_text().split():
                try:
                    args = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
                except FileNotFoundError:
                    continue
                busy |= any(Path(arg.decode(errors="replace")).name in
                            {"_kara_stems.py", "_kara_prepare.py", "_kara_voice.py", "_kara_voice_session.py"}
                            for arg in args if arg)
        if busy or multimedia_active():
            self.reason = "Activité Kara ou multimédia prioritaire"
        if any(shutil.disk_usage(path).free < 20 * GIB for path in (self.workspace, self.library)):
            self.reason = "Réserve de 20 Gio"
        if self.login:
            try:
                if self.closed_lid and self.login.sleeping():
                    self.reason = "Mise en veille demandée"
                if self.profile:
                    active_profile = self.login.get(PROFILES_PATH, PROFILES, "ActiveProfile", service=PROFILES)
                    if active_profile != self.profile:
                        self.reason = "Profil énergétique modifié"
            except self.login.glib.Error:
                self.reason = "État de la session ou du profil indisponible"
        disk = {}
        for line in Path("/proc/diskstats").read_text().splitlines():
            row = line.split()
            if tuple(map(int, row[:2])) in self.devices:
                disk[row[2]] = [int(row[i]) for i in (5, 9, 12)]
        cpu = list(map(int, Path("/proc/stat").read_text().splitlines()[0].split()[1:9]))
        self.samples.append({
            "seconds": round(now - self.start, 3), "cpu_ticks": [sum(cpu), cpu[3] + cpu[4]],
            "gpu_busy_pct": read_sample(self.gpu / "gpu_busy_percent"),
            "gpu_gtt_bytes": read_sample(self.gpu / "mem_info_gtt_used"),
            "gpu_vram_bytes": read_sample(self.gpu / "mem_info_vram_used"),
            "memory_available_kib": mem["MemAvailable"], "swap_free_kib": mem["SwapFree"],
            "memory_psi_some_avg10_pct": pressure,
            "cpu_c": cpu_temp, "gpu_c": gpu_temp,
            "amdgpu_ppt_w": read_sample(self.sensors["amdgpu"] / "power1_average", 1_000_000),
            "disk": disk,
        })


class Timings:
    def __init__(self):
        self.durations = defaultdict(float)
        self.counts = defaultdict(int)
        self.stack = []

    @contextmanager
    def stage(self, label):
        # Exclusive wall times keep local PCM decoding and validation out of
        # the enclosing inference or alignment phase totals.
        frame = [monotonic(), 0.]
        self.stack.append(frame)
        try:
            yield
        finally:
            elapsed = monotonic() - frame[0]
            self.stack.pop()
            self.durations[label] += elapsed - frame[1]
            self.counts[label] += 1
            if self.stack:
                self.stack[-1][1] += elapsed

    def wrap(self, owner, name, label):
        original = getattr(owner, name)

        @wraps(original)
        def measured(*args, **kwargs):
            with self.stage(label):
                return original(*args, **kwargs)

        setattr(owner, name, measured)

    def pcm(self, owner):
        original = owner.pcm_file

        @contextmanager
        def measured(path):
            with self.stage("decode_audio"):
                pcm = original(path)
                values = pcm.__enter__()
            try:
                yield values
            finally:
                pcm.__exit__(None, None, None)

        owner.pcm_file = measured


@contextmanager
def lid_mode(enabled):
    if not enabled:
        yield
        return
    login = Login()
    interface = LOGIN + ".Manager"
    try:
        if (mains_online() is not True or login.sleeping()
                or login.get(MANAGER, interface, "LidClosed")):
            raise Interrupted("Qualification à démarrer sur secteur, capot ouvert")
        # Keep only the low-level lid handle. Suspend remains available and
        # the descriptor cannot outlive this parent or pass to the engines.
        fd = login.inhibit("handle-lid-switch", "block")
        try:
            print("Banc prêt · fermer le capot dans les 90 s", flush=True)
            deadline = monotonic() + 90
            while not login.get(MANAGER, interface, "LidClosed"):
                if monotonic() >= deadline or mains_online() is not True or login.sleeping():
                    raise Interrupted("Fermeture du capot non admise")
                sleep(.5)
            yield
        finally:
            os.close(fd)
    except login.glib.Error as error:
        raise Interrupted("État du capot ou inhibiteur logind indisponible") from error


def sizes(folder):
    return {p.name: {"bytes": p.stat().st_size, "allocated": p.stat().st_blocks * 512}
            for p in folder.iterdir() if p.is_file() and p.name != "lock"}


def worker(workspace, case, mode, threads, stems_from=None):
    stems.ROOT = (stems_from or workspace) / "stems"
    base_song = next(path for path in (workspace / "input" / case).iterdir()
                     if path.suffix in (".opus", ".mp3"))
    stem_song = base_song
    if stems_from is not None:
        stem_song = stems_from / "input" / case / base_song.name
        # Audio equality ties both sides of a Turbo A/B to the exact same
        # stems from the reference run. Neither side rewrites that cache.
        if turbo.digest(stem_song) != turbo.digest(base_song) or not stems.stem_directory(stem_song).is_dir():
            raise ValueError("Les pistes de référence ne correspondent pas à l'entrée du comparatif")

        def comparison_stems(_):
            pair = stems.cached_stems(stem_song)
            if pair is None:
                raise ValueError("Les pistes de référence ne passent plus la validation")
            return pair

        prepare.cached_stems = comparison_stems
    timing = Timings()
    start = monotonic()
    details = {}
    if mode == "demucs":
        # The private cache does not grant another Demucs admission slot.
        with lease(STEM_ROOT / "demucs.lock", wait=False) as slot:
            if slot is None:
                raise RuntimeError("Créneau Demucs Kara occupé")
            output = stems.stem_directory(base_song)
            if output.exists():
                # A worker can publish just before the monitor cancels it.
                # Keep such a generation outside the fresh timing sample.
                output.rename(output.with_name(f"interrupted-{time_ns()}"))
            child_details = workspace / "evidence" / f"{case}-demucs-phases.json"
            popen = subprocess.Popen

            def measured_popen(argv, *args, **kwargs):
                if tuple(argv[1:3]) == ("-m", "demucs"):
                    argv = (argv[0], "-B", __file__, "--demucs-worker", str(child_details), *argv[3:])
                return popen(argv, *args, **kwargs)

            subprocess.Popen = measured_popen
            timing.wrap(stems, "pcm_digest", "decode_audio_identity")
            timing.wrap(stems, "publish", "cache_write")
            timing.wrap(stems, "aligned_pair", "stem_validation")
            with timing.stage("demucs"):
                files = stems.separate_locked(base_song)
            if not files:
                raise ValueError("Pistes Demucs non validées")
            details = {"storage": sizes(files[0].parent)}
            measured = json.loads(child_details.read_text())
            timing.durations["demucs"] -= sum(measured["seconds"].values())
            timing.durations.update(measured["seconds"])
            timing.counts.update(measured["calls"])
    else:
        # A hard link to the private input keeps the plain test on identical
        # samples while keeping the production local-LRC path unmodified below.
        song = base_song
        plain = None
        if mode in ("plain", "annotated"):
            song = workspace / "untimed" / case / base_song.name
            song.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            if not song.exists():
                os.link(base_song, song)
            generated = song.with_suffix(".lrc")
            if generated.exists():
                generated.rename(workspace / "evidence" / f"{case}-{mode}-{time_ns()}.interrupted.lrc")
            plain = (base_song.parent / "strawberry.txt").read_bytes()
            if stems_from is None:
                prepare.cached_stems = lambda _: stems.cached_stems(stem_song)
        os.environ["KARA_MUSIC_LIBRARY"] = str(workspace)
        cache = prepare.PreparedCache(workspace / mode)
        folder = cache.folder(song)
        if folder.exists():
            # An interruption after publication must not turn a fresh
            # timing into a cache hit on resume. Preserve the private draft.
            folder.rename(workspace / "evidence" / f"{case}-{mode}-{time_ns()}.interrupted-cache")
        for owner, name, label in (
            (prepare, "transcribe_file", "turbo"), (prepare, "choose_alignment", "alignment"),
            (prepare, "refine", "refinement"), (prepare, "validate", "reference_validation"),
            (reference, "finish_alignment", "reference_validation"),
            (refine, "finish_alignment", "reference_validation"),
            (prepare, "pcm_digest", "decode_audio_identity"), (prepare, "atomic", "cache_write"),
        ):
            timing.wrap(owner, name, label)
        timing.wrap(turbo.TurboDecoder, "__init__", "model_load")
        timing.pcm(turbo)
        timing.pcm(refine)
        engine = prepare.Preparer(cache, require_vulkan=True, threads=threads)
        # Failed plain receipts omit the lexical lines. Keep a private
        # diagnostic snapshot so the missed onsets can be audited.
        observed = []

        def trace(_, kind, **data):
            if kind == "reference-line":
                observed.append(data["line"])

        if mode in ("plain", "annotated"):
            engine.trace = trace
        try:
            ready, report, _, duration = engine.prepare(song, plain=plain, observed_corrections=True)
        finally:
            engine.close()
        details = {"ready": ready, "report": report, "duration": duration, "storage": sizes(folder)}
        if observed:
            record(workspace / "evidence" / f"{case}-{mode}.json", observed)
    own, children = (resource.getrusage(who) for who in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN))
    return {
        "seconds": monotonic() - start, "phase_seconds": dict(timing.durations),
        "phase_calls": dict(timing.counts), "cpu_seconds": sum(r.ru_utime + r.ru_stime for r in (own, children)),
        "peak_rss_kib_self": own.ru_maxrss, "peak_rss_kib_child": children.ru_maxrss, **details,
    }


def stage_inputs(workspace, cases, library, index):
    source_db = sqlite3.connect(index.as_uri() + "?mode=ro", uri=True) if index else nullcontext()
    with source_db as db:
        for case in cases:
            source = (library / case["song"]).resolve(strict=True)
            if not source.is_relative_to(library):
                raise ValueError("Morceau situé hors bibliothèque")
            folder = workspace / "input" / case["id"]
            folder.mkdir(parents=True, exist_ok=True)
            originals = (source,) if case.get("annotated") else (source, source.with_suffix(".lrc"))
            for original in originals:
                local = folder / original.name
                if not local.exists():
                    shutil.copyfile(original, local)
                if turbo.digest(original) != turbo.digest(local):
                    raise ValueError(f"Entrée modifiée depuis le début du banc : {case['id']}")
            if case.get("plain") or case.get("annotated"):
                if case.get("annotated"):
                    lyrics = prepare.read_source(source.with_suffix(".txt")).decode("utf-8-sig")
                else:
                    if db is None:
                        raise ValueError("--index est requis pour les paroles Strawberry")
                    row = db.execute("SELECT info FROM songs WHERE path=? AND lyrics='Strawberry'",
                                     (str(source),)).fetchone()
                    lyrics = json.loads(row[0]).get("plain") if row else None
                if not lyrics or len(lyrics.encode()) > prepare.MAX_LRC_BYTES:
                    raise ValueError(f"Texte Strawberry absent : {case['id']}")
                path = folder / "strawberry.txt"
                if path.exists() and path.read_text() != lyrics:
                    raise ValueError(f"Texte Strawberry modifié : {case['id']}")
                path.write_text(lyrics)


def bench(args):
    cases = json.loads(args.corpus.read_text())
    ids = [case["id"] for case in cases]
    if len(ids) != len(set(ids)) or not all(re.fullmatch("[a-z0-9-]{1,48}", identity) for identity in ids):
        raise ValueError("Identifiants du corpus invalides")
    cases = [case for case in cases if not args.case or case["id"] in args.case]
    if not cases or args.case and len(cases) != len(set(args.case)):
        raise ValueError("Cas d'essai inconnu")
    workspace, library = args.workspace.resolve(), args.library.resolve(strict=True)
    if workspace.is_relative_to(library) or workspace == REPO:
        raise ValueError("Utiliser un dossier d'essai dédié, hors bibliothèque")
    workspace.mkdir(mode=0o700, parents=True, exist_ok=True)
    if workspace.stat().st_mode & 0o077:
        raise ValueError("Le dossier d'essai doit être privé (0700)")
    if shutil.disk_usage(workspace).free < 21 * GIB:
        raise ValueError("Espace insuffisant pour les copies privées de ce banc")
    with lease(workspace / "bench.lock", wait=False) as slot:
        if slot is None:
            raise ValueError("Ce banc est déjà en cours")
        stage_inputs(workspace, cases, library, args.index.resolve(strict=True) if args.index else None)
        (workspace / "evidence").mkdir(exist_ok=True)
        (workspace / "results").mkdir(exist_ok=True)
        sources = [*sorted((REPO / "bin").glob("_kara_*.py")), turbo.SOURCE, Path(__file__)]
        config = {"profile": args.profile, "demucs_threads": args.demucs_threads, "turbo_threads": args.threads,
                  "closed_lid": args.closed_lid,
                  "memory_psi_limit_pct": args.memory_psi,
                  "stem_reference": str(args.stems_from) if args.stems_from else None,
                  "turbo_weights_file": prepare.file_token(turbo.MODEL), "demucs": stems.model_signature(),
                  "kernel": os.uname().release,
                  "sources": {str(path.resolve().relative_to(REPO)): turbo.digest(path) for path in sources}}
        config_file = workspace / "config.json"
        if config_file.exists() and json.loads(config_file.read_text()) != config:
            raise ValueError("Configuration modifiée. Utiliser un nouveau dossier de mesures.")
        record(config_file, config)
        for case in cases:
            for mode in args.phase:
                if mode == "plain" and not case.get("plain"):
                    continue
                if (mode == "annotated") != bool(case.get("annotated")):
                    continue
                case_id = case["id"]
                result = workspace / "results" / f"{case_id}-{mode}.json"
                if result.exists():
                    continue
                stem_results = (args.stems_from or workspace) / "results"
                if mode in ("timed", "plain") and not (stem_results / f"{case_id}-demucs.json").exists():
                    raise ValueError("Le résultat Demucs est requis avant Turbo pour ce cas.")
                monitor = Monitor(workspace, library, args.profile, args.memory_psi, closed_lid=args.closed_lid)
                print(f"{case_id} {mode}", flush=True)
                deadline = monotonic() + 180
                while (monitor.stop() and monotonic() < deadline and
                       monitor.reason.startswith(("Limite thermique", "Pression mémoire", "Profil énergétique modifié"))):
                    sleep(2)
                    monitor.reason = None
                if monitor.reason:
                    raise Interrupted(monitor.reason)
                monitor.samples.clear()
                monitor.start = monitor.next = monotonic()
                env = dict(os.environ, KARA_DEMUCS_THREADS=str(args.demucs_threads))
                command_line = ["nice", "-n", "12", sys.executable, "-B", __file__, str(workspace),
                                "--worker", case_id, mode, "--threads", str(args.threads)]
                if args.stems_from:
                    command_line.extend(("--stems-from", str(args.stems_from)))
                try:
                    code, output = run(command_line, stop=monitor.stop, timeout=1800, env=env)
                except Interrupted:
                    record(result.with_suffix(f".{time_ns()}.interrupted.json"),
                           {"reason": monitor.reason, "system": monitor.samples})
                    raise Interrupted(monitor.reason) from None
                measured = json.loads(output.splitlines()[-1]) if output else {}
                if code:
                    raise RuntimeError(measured.get("error", f"Moteur arrêté ({code})"))
                record(result, {**measured, "system": monitor.samples})
                print(f"{case_id} {mode} {measured['seconds']:.1f} s", flush=True)


def main():
    if sys.argv[1:2] == ["--demucs-worker"]:
        from demucs import separate

        timing = Timings()
        for name, label in (("load_track", "decode_audio"), ("apply_model", "demucs_inference"),
                            ("save_audio", "flac_encoding")):
            timing.wrap(separate, name, label)
        separate.main(sys.argv[3:])
        record(Path(sys.argv[2]), {"seconds": dict(timing.durations), "calls": dict(timing.counts)})
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workspace", type=Path)
    parser.add_argument("--index", type=Path)
    parser.add_argument("--library", type=Path, default=music_library())
    parser.add_argument("--corpus", type=Path, default=CASES)
    parser.add_argument("--profile", choices=("power-saver", "balanced", "performance"))
    parser.add_argument("--closed-lid", action="store_true", help="Qualifier la charge avec fermeture physique du capot.")
    parser.add_argument("--memory-psi", choices=(1., 2., 3.), type=float, default=1.,
                        help="Seuil PSI manuel. Au-dessus de 1 %% le banc réserve 4 Gio de RAM disponible.")
    parser.add_argument("--threads", type=int, choices=range(1, 33), default=4)
    parser.add_argument("--demucs-threads", type=int, choices=(2, 6), default=2)
    parser.add_argument("--stems-from", type=Path)
    parser.add_argument("--phase", choices=("demucs", "timed", "plain", "annotated"), nargs="+",
                        default=["demucs", "timed", "plain"])
    parser.add_argument("--case", action="append")
    parser.add_argument("--worker", nargs=2, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.stems_from:
        args.stems_from = args.stems_from.resolve()
        if (args.worker and args.worker[1] not in ("timed", "plain")
                or not args.worker and not set(args.phase).issubset({"timed", "plain"})
                or args.stems_from == args.workspace.resolve()):
            parser.error("--stems-from requiert un dossier de référence distinct et une phase timed ou plain.")

    def cancel(_signal, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, cancel)
    if args.worker:
        try:
            print(json.dumps(worker(args.workspace, *args.worker, args.threads, args.stems_from),
                             allow_nan=False), flush=True)
        except (ValueError, OSError, RuntimeError, subprocess.SubprocessError) as error:
            print(json.dumps({"error": str(error)}), flush=True)
            return 1
    else:
        try:
            with lid_mode(args.closed_lid):
                bench(args)
        except (ValueError, OSError, Interrupted, KeyboardInterrupt, RuntimeError, subprocess.SubprocessError) as error:
            print(f"Banc interrompu : {error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
