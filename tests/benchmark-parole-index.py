#!/usr/bin/python3 -B
"""Run the actual inventory on light FLAC files and private SQLite databases."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import ctypes
import os
import re
from contextlib import contextmanager
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import resource
import sqlite3
import subprocess
import sys
from statistics import median, quantiles
from threading import Thread
from time import perf_counter, sleep, time
from urllib.request import urlopen

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("workspace", type=Path)
parser.add_argument("--code", type=Path, default=Path(__file__).resolve().parents[1] / "bin")
parser.add_argument("--scale", type=int, default=2500)
parser.add_argument("--mode", choices=("scale", "imports", "batch", "metadata", "tags", "latency", "overlap"), default="scale")
parser.add_argument("--reference", action="store_true")
parser.add_argument("--batch", type=int, choices=(1, 16, 32, 64, 128), default=64)
options = parser.parse_args()
sys.path.insert(0, str(options.code.resolve()))
import _parole_index as module


def io_counters():
    return {name: int(value) for line in Path("/proc/self/io").read_text().splitlines()
            for name, value in [line.split(":")]}


def cpu_seconds(who):
    info = resource.getrusage(who)
    return info.ru_utime + info.ru_stime


def measure(action):
    began = perf_counter()
    cpu = cpu_seconds(resource.RUSAGE_SELF)
    children = cpu_seconds(resource.RUSAGE_CHILDREN)
    io = io_counters()
    result = action()
    elapsed = perf_counter() - began
    return dict(result, seconds=elapsed, cpu_seconds=cpu_seconds(resource.RUSAGE_SELF)-cpu,
                child_cpu=cpu_seconds(resource.RUSAGE_CHILDREN)-children,
                rss_kib=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
                disk_bytes={k: v-io[k] for k, v in io_counters().items() if k in ("read_bytes", "write_bytes")})


workspace = options.workspace.resolve()
workspace.mkdir(parents=True, exist_ok=True)
root = workspace / "music"
root.mkdir(exist_ok=True)
sample = workspace / "sample.flac"
if not sample.exists():
    subprocess.run(("ffmpeg", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=16000:cl=mono", "-t", "0.5",
                    "-metadata", "artist=Fixture", "-metadata", "album=Album", "-metadata", "title=Témoin",
                    str(sample)), check=True)
audio = sample.read_bytes()
collection = workspace / "collection.sqlite3"
index = module.Index(root, workspace / "state")
tags = dict(artist="Fixture", album="Album", title="Témoin", duration_seconds=.5, codec="flac")


def place(first, amount, *, catalogue=True, folder="initial"):
    paths = []
    for number in range(first, first + amount):
        path = root / folder / f"album-{number//10:06}" / f"piste-{number:06}.flac"
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_bytes(audio)
        paths.append(path)
    if catalogue:
        with sqlite3.connect(collection) as db:
            db.executemany("INSERT INTO songs VALUES(?,?,?,?,?,?,?,?,0,0)",
                           ((p.as_uri(), p.stem, "Fixture", p.parent.name, "Fixture",
                             int((st := p.stat()).st_mtime), st.st_size, 500_000_000) for p in paths))
    return paths


if options.mode in ("scale", "imports", "batch", "overlap"):
    with sqlite3.connect(collection) as db:
        db.executescript("""
            CREATE TABLE songs(url TEXT,title TEXT,artist TEXT,album TEXT,albumartist TEXT,
                               mtime INTEGER,filesize INTEGER,length INTEGER,unavailable INTEGER,playcount INTEGER);
            CREATE INDEX idx_url ON songs(url);
        """)


def emit(name, action):
    print(json.dumps(dict(case=name, **measure(action))), flush=True)


def inventory(**kwargs):
    probes = transactions = writes = 0
    opened = index.db

    def reader(path):
        nonlocal probes
        probes += 1
        return module.probe_track(path)

    @contextmanager
    def traced(*args, **values):
        with opened(*args, **values) as db:
            def trace(statement):
                nonlocal transactions, writes
                operation = statement.lstrip().partition(" ")[0]
                transactions += operation == "COMMIT"
                writes += operation in ("INSERT", "UPDATE", "DELETE")
            db.set_trace_callback(trace)
            yield db

    index.db = traced
    try:
        counts = module.inventory(index, collection, lambda *_: ("", "absent", {}),
                                  lambda: False, lambda *_: None, reader=reader, **kwargs)
    finally:
        index.db = opened
    return dict(counts, subprocesses=probes, committed=transactions, sql_writes=writes)


def deliver(paths, *, proofs=True, mask=None):
    versioned = ({str(path): {"token": module.revision(path.stat()), "tags": tags} for path in paths}
                 if proofs else None)
    index.notify(paths, mask or module.MEDIA, proofs=versioned, now=time() - 3)
    totals = dict(scanned=0, changed=0, committed=0, sql_writes=0, subprocesses=0, delivered=0,
                  receipts=0, batches=0)
    while events := index.events(time(), size=options.batch)[1]:
        values = inventory(events=events)
        totals["batches"] += 1
        for key in totals.keys() - {"batches"}:
            totals[key] += values[key]
    return totals


def publication(paths):
    return inventory() if options.reference else deliver(paths)


def local_changes(paths, mask_name):
    if options.reference:
        return inventory()
    return deliver(paths, proofs=False, mask=getattr(module, mask_name))


if options.mode == "scale":
    place(0, options.scale)
    emit("construction", inventory)
    emit("inchangée", inventory)
    with index.db() as db:
        expected, actual = options.scale, db.execute("SELECT total FROM library_stats").fetchone()[0]
        if expected != actual or db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise SystemExit("Index de fixture incohérent")
        print(json.dumps({"validation": actual, "display_plan": [row[3] for row in db.execute("""
            EXPLAIN QUERY PLAN SELECT id FROM songs ORDER BY owner,album,disc,track,title,id LIMIT 80
        """)]}))
elif options.mode == "imports":
    initial = place(0, options.scale)
    inventory()

    def albums(n):
        totals = dict(scanned=0, changed=0, committed=0, sql_writes=0, subprocesses=0)
        paths = place(0, 10*n, folder=f"ajout-{n}", catalogue=False)
        for start in range(0, len(paths), 10):
            result = publication(paths[start:start+10])
            for key in totals:
                totals[key] += result[key]
        return totals

    for n in (1, 10, 100):
        emit(f"{n} admissions de 10 pistes", lambda n=n: albums(n))
    new = place(0, 1000, folder="lot-mille", catalogue=False)
    emit("ajout groupé 1 000 pistes", lambda: publication(new))
    target = (initial + new)[:1000]
    for path in target:
        path.with_suffix(".lrc").write_text("[00:00.25]Repère synthétique\n")
    emit("ajout de 1 000 LRC", lambda: local_changes(target, "SIDECAR"))
    edited = workspace / "edited.flac"
    subprocess.run(("ffmpeg", "-v", "error", "-i", str(sample), "-map", "0", "-c", "copy",
                    "-metadata", "title=Nouveau titre témoin", str(edited)), check=True)
    correction = edited.read_bytes()
    for path in target:
        path.write_bytes(correction)
    emit("modification de 1 000 tags", lambda: local_changes(target, "MEDIA"))
    old_folder = root / "initial" / "album-000000"
    new_folder = root / "Artiste regroupé" / old_folder.name
    new_folder.parent.mkdir()
    old = list(old_folder.glob("*.flac"))
    old_folder.rename(new_folder)
    if options.reference:
        emit("regroupement, réconciliation", inventory)
    else:
        index.notify(old, module.REMOVE, now=time()-3)
        emit("regroupement, notification des destinations", lambda: local_changes(list(new_folder.glob("*.flac")), "MEDIA"))
elif options.mode == "batch":
    new = place(0, options.scale, catalogue=False)
    emit(f"lots de {options.batch}", lambda: deliver(new))
elif options.mode == "overlap":
    place(0, options.scale)
    inventory()
    paths = place(0, 60, folder="publication-simultanée", catalogue=False)
    block = bytes(64 * 1024)
    blocks_per_transfer = 64

    class Transfer(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", str(len(block) * blocks_per_transfer))
            self.end_headers()
            for _ in range(blocks_per_transfer):
                self.wfile.write(block)
                sleep(.012)

    with ThreadingHTTPServer(("127.0.0.1", 0), Transfer) as server:
        thread = Thread(target=server.serve_forever)
        thread.start()
        try:
            def download(number):
                with urlopen(f"http://127.0.0.1:{server.server_port}/fixture", timeout=10) as incoming, (
                        workspace / f"received-{number}").open("wb") as output:
                    while data := incoming.read(len(block)):
                        output.write(data)
                    output.flush()
                    os.fsync(output.fileno())
                    return output.tell()

            def flow(concurrent_index):
                with ThreadPoolExecutor(max_workers=5) as pool:
                    began = perf_counter()
                    downloads = [pool.submit(download, i) for i in range(4)]
                    indexing = pool.submit(publication, paths) if concurrent_index else None
                    amount = sum(task.result() for task in downloads)
                    transfer_seconds = perf_counter() - began
                    outcome = indexing.result() if indexing else {}
                return {"transferred_bytes": amount, "download_seconds": transfer_seconds,
                        "download_mib_per_second": amount / transfer_seconds / 1024**2,
                        "index_subprocesses": outcome.get("subprocesses", 0)}

            emit("4 transferts HTTP locaux seuls", lambda: flow(False))
            emit("4 transferts HTTP locaux et 60 publications", lambda: flow(True))
        finally:
            server.shutdown()
            thread.join()
elif options.mode == "latency":
    paths = place(0, 24, catalogue=False)
    for interval in (.25, .5, 1., 2.):
        def flow():
            module.EVENT_DELAY = interval
            module.EVENT_MAX_DELAY = max(1.5, interval)
            began = {}
            settled = {}
            writes = 0

            def publish():
                for path in paths:
                    began[str(path)] = time()
                    proof = {str(path): {"token": module.revision(path.stat()), "tags": tags}}
                    index.notify([path], proofs=proof)
                    sleep(.025)

            publisher = Thread(target=publish)
            publisher.start()
            deadline = perf_counter() + 15
            while (len(settled) < len(paths)) and perf_counter() < deadline:
                events = index.events(time())[1]
                if events:
                    values = inventory(events=events)
                    writes += values["committed"]
                    with index.db() as db:
                        for item in events:
                            if db.execute("SELECT 1 FROM songs WHERE path=?", (item["path"],)).fetchone():
                                settled[item["path"]] = time()
                sleep(.05)
            publisher.join()
            if len(settled) != len(paths):
                raise SystemExit("Livraison de latence incomplète")
            samples = [settled[path] - began[path] for path in sorted(settled)]
            return {"p50_seconds": median(samples), "p95_seconds": quantiles(samples, n=20)[-1],
                    "transactions_consumer": writes, "paths": len(samples)}
        emit(f"fenêtre {interval} seconde(s)", flow)
elif options.mode == "metadata":
    paths = place(0, options.scale, catalogue=False)
    ordered = sorted(paths)
    for workers in (1, 2, 4, 8, 16):
        def run():
            with ThreadPoolExecutor(max_workers=workers) as pool:
                results = list(pool.map(module.probe_track, ordered))
            return {"files": len(results), "subprocesses": len(results),
                    "tag_digest": sha256(json.dumps(results, sort_keys=True).encode()).hexdigest()}

        emit(f"FFprobe, {workers} travailleurs", run)

else:
    library = ctypes.CDLL("libtag_c.so.2")
    strings = ctypes.POINTER(ctypes.c_char_p)
    declarations = {
        "file_new": (ctypes.c_void_p, ctypes.c_char_p),
        "file_free": (None, ctypes.c_void_p),
        "file_is_valid": (ctypes.c_int, ctypes.c_void_p),
        "file_audioproperties": (ctypes.c_void_p, ctypes.c_void_p),
        "audioproperties_length": (ctypes.c_int, ctypes.c_void_p),
        "property_keys": (strings, ctypes.c_void_p),
        "property_get": (strings, ctypes.c_void_p, ctypes.c_char_p),
        "property_free": (None, strings),
    }
    for name, (returned, *parameters) in declarations.items():
        function = getattr(library, "taglib_" + name)
        function.restype, function.argtypes = returned, parameters

    def list_strings(pointer):
        try:
            result = []
            i = 0
            while pointer and pointer[i] is not None:
                result.append(pointer[i])
                i += 1
            return result
        finally:
            if pointer:
                library.taglib_property_free(pointer)

    def direct(path):
        pointer = library.taglib_file_new(os.fsencode(path))
        try:
            if not pointer or not library.taglib_file_is_valid(pointer):
                raise ValueError("format non lu par TagLib")
            keys = list_strings(library.taglib_property_keys(pointer))
            tags = {re.sub(r"[^a-z0-9]", "", key.decode().casefold()):
                    [value.decode() for value in list_strings(library.taglib_property_get(pointer, key))]
                    for key in keys}
            properties = library.taglib_file_audioproperties(pointer)
            duration = library.taglib_audioproperties_length(properties) if properties else None
            return tags, duration
        finally:
            if pointer:
                library.taglib_file_free(pointer)

    formats = (("opus", "libopus"), ("flac", "flac"), ("mp3", "libmp3lame"),
               ("ogg", "libvorbis"), ("m4a", "aac"))
    fields = ("title", "artist", "album", "albumartist", "track", "disc", "lyrics", "isrc")
    for extension, encoder in formats:
        media = workspace / f"tags.{extension}"
        command = ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=stereo",
                   "-t", "1.37", "-c:a", encoder]
        for name, value in zip(("title", "artist", "album", "album_artist", "track", "disc", "lyrics", "ISRC"),
                               ("Titre témoin", "Artiste, Invité", "Disque", "Artiste", "03/12", "2/2",
                                "Texte synthétique", "ZZABC2600001")):
            command.extend(("-metadata", f"{name}={value}"))
        subprocess.run((*command, str(media)), check=True)

        def with_ffprobe():
            tags, _ = module.probe_track(media)
            for _ in range(9):
                module.probe_track(media)
            return {"values": {key: tags[key] for key in fields if key in tags},
                    "duration": tags.get("duration_seconds"), "subprocesses": 10}

        def with_taglib():
            tags, duration = direct(media)
            for _ in range(9):
                direct(media)
            return {"values": tags, "duration": duration, "subprocesses": 0}

        emit(f"{extension}, FFprobe", with_ffprobe)
        emit(f"{extension}, TagLib C", with_taglib)
