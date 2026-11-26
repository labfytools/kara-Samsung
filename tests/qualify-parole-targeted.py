#!/usr/bin/env python3
"""Banc local de 50 extraits, avec relecture des passages incertains."""

from argparse import ArgumentParser
from collections import defaultdict
from contextlib import closing
from hashlib import sha256
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
from time import monotonic, sleep

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_prepare import PreparedCache, Preparer
from _kara_turbo import RATE
from _kara_words import words
from _parole_acoustic import prompts, substitutions
from _parole_index import collection_reader, key, sidecar
from _parole_lexicon import identities, ranked_forms, records
from _parole_night import LOGIN, MANAGER, Login, acpi_lid_closed, mains_online
from _paroles_lrc import parse_lrc


def extract(root, path):
    candidate = Path(path)
    try:
        if (not candidate.is_relative_to(root) or candidate.resolve(strict=True) != candidate
                or not candidate.is_file()):
            return None
    except OSError:
        return None
    lines = [(t, text) for t, text in parse_lrc(sidecar(candidate, ".lrc")[1]) if words(text)]
    if len(lines) < 5:
        return None
    start = max(0., lines[0][0] - .08)
    sample = [(t, text) for t, text in lines[:5] if t < start + 19.]
    end = sample[-1][0] - .05
    if end - start < 5:
        return None
    return start, end, [w for _, text in sample[:-1] for w in words(text)]


def admit(login, temperatures, *, running):
    # Apply the two production memory floors separately. The native
    # model occupies part of the admission budget once loaded.
    deadline = monotonic() + 1800
    while True:
        pressure = float(Path("/proc/pressure/memory").read_text().split()[1].split("=")[1])
        mem = next(int(row.split()[1]) for row in Path("/proc/meminfo").read_text().splitlines()
                   if row.startswith("MemAvailable:"))
        cpu, gpu = temperatures()
        if (acpi_lid_closed() is not True and mains_online() is True and not login.sleeping()
                and not login.get(MANAGER, LOGIN + ".Manager", "LidClosed")
                and pressure < 1 and mem >= (1536 if running else 4096) * 1024
                and cpu is not None and gpu is not None and 0 < cpu < 78 and 0 < gpu < 75):
            return
        if monotonic() >= deadline:
            raise RuntimeError("Mesure suspendue par le capot, l’alimentation ou les ressources")
        sleep(2)


def observations(decoder, login, temperatures, pcm, language, origin, **settings):
    admit(login, temperatures, running=True)
    language, segments = decoder.decode(pcm, language, origin, **settings)
    return language, [w for segment in segments if segment.no_speech <= .65 for w in segment.words]


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--collection", required=True, type=Path)
    parser.add_argument("--genres", type=Path, help="groupes de genre documentés par artiste dans un JSON privé")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    root, index = args.library.resolve(strict=True), args.index.resolve(strict=True)
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        songs = db.execute("SELECT * FROM songs ORDER BY id").fetchall()
        with collection_reader(args.collection, root) as batch:
            genres = {}
            for offset in range(0, len(songs), 128):
                paths = [Path(row["path"]) for row in songs[offset:offset + 128]]
                genres.update((name, row["genre"]) for name, row in batch(paths).items() if row["genre"])
    candidates = defaultdict(list)
    tags = json.loads(args.genres.read_text()) if args.genres else {}
    artist_genres = {key(name): key(genre) for genre, artists in tags.items() for name in artists}
    for row in songs:
        genre = genres.get(row["path"])
        if not genre or key(genre) == "music":
            genre = artist_genres.get(key(row["owner"]))
            if genre:
                genres[row["path"]] = genre
        if row["lrc"] in ("local", "externe") and genre and row["duration"] > 30.:
            candidates[key(genre)].append(row)
    # Keep the ordering reproducible and prevent abundant artists from
    # dominating the first eligible cases in a genre.
    groups = []
    for genre, rows in sorted(candidates.items(), key=lambda item: (-len(item[1]), item[0])):
        artists = defaultdict(int)
        group = []
        for row in sorted(rows, key=lambda r: sha256(r["path"].encode()).digest()):
            if artists[row["owner"]] >= 2:
                continue
            sample = extract(root, row["path"])
            if sample:
                group.append((genre, row, sample))
                artists[row["owner"]] += 1
            if len(group) == 50:
                break
        groups.append(iter(group))
    chosen, recordings = [], set()
    while len(chosen) < 50:
        previous = len(chosen)
        for group in groups:
            # Catalogue and PCM identities apply across genres as well as
            # editions. Multiple copies must count as one evaluated song.
            for sample in group:
                _, row, _ = sample
                identifiers = identities(row, json.loads(row["info"]))
                if identifiers & recordings:
                    continue
                recordings.update(identifiers)
                chosen.append(sample)
                break
            if len(chosen) == 50:
                break
        if previous == len(chosen):
            break
    if len({row["id"] for _, row, _ in chosen}) != 50:
        parser.error("50 enregistrements distincts avec genre et LRC sont requis")

    os.umask(0o077)
    plan = [dict(id=row["id"], genre=genre, start=s[0], end=s[1]) for genre, row, s in chosen]
    manifest_path = args.output / "manifest.json"
    corpus_path = args.output / "corpus.jsonl"
    result_path = args.output / "result.jsonl"
    registered = set()
    evaluated = set()
    if args.resume:
        if json.loads(manifest_path.read_text()) != plan or not corpus_path.is_file():
            parser.error("le corpus ou les extraits ne correspondent pas à la session à reprendre")
        with result_path.open(encoding="utf-8") as results:
            for result in map(json.loads, results):
                registered.add(result["id"])
                if result["tests"]:
                    evaluated.add(result["genre"])
    else:
        args.output.mkdir(mode=0o700)
        with manifest_path.open("x", encoding="utf-8") as manifest:
            json.dump(plan, manifest)
        with corpus_path.open("x", encoding="utf-8") as corpus:
            for record in records(index, root, exclude=[row["id"] for _, row, _ in chosen], genres=genres):
                corpus.write(json.dumps(record, ensure_ascii=False) + "\n")
    compare = runpy.run_path(str(Path(__file__).with_name("qualify-kara-singing.py")))["lexical_errors"]
    temperatures = runpy.run_path(str(Path(__file__).with_name("observe-parole-night.py")))["temperatures"]
    login = Login()
    admit(login, temperatures, running=False)
    # Reserve the same model slot as live Kara. A benchmark must never load
    # a second copy of the weights while a preparation owns the GPU.
    preparer = Preparer(PreparedCache(args.output / "prepared"), threads=4, require_vulkan=True)
    try:
        preparer.settings()
        decoder = preparer.load_decoder()
        with result_path.open("a" if args.resume else "x", encoding="utf-8", buffering=1) as output:
            ranking = {}
            for genre, row, (start, end, expected) in chosen:
                if row["id"] in registered:
                    continue
                pcm = subprocess.check_output(
                    ["ffmpeg", "-v", "error", "-nostdin", "-protocol_whitelist", "file,pipe",
                     "-ss", str(start), "-i", row["path"], "-map", "0:a:0", "-vn", "-sn", "-dn",
                     "-t", str(end - start), "-ac", "1", "-ar", str(RATE), "-f", "s16le", "-"],
                    timeout=20)
                began = monotonic()
                language, initial = observations(decoder, login, temperatures, pcm, "", start)
                baseline = compare(expected, [w.text for w in initial])
                record = dict(id=row["id"], genre=genre, language=language, baseline=baseline,
                              seconds=round(monotonic() - began, 3), tests={})
                uncertain = [w for w in initial if w.confidence < .62]
                if uncertain:
                    center = min(uncertain, key=lambda w: (w.confidence, w.start)).start
                    lo = max(0, round((center - start - 2.5) * RATE))
                    hi = min(len(pcm) // 2, round((center - start + 2.5) * RATE))
                    window, origin = pcm[lo * 2:hi * 2], start + lo / RATE
                    near = [w for w in uncertain if origin <= w.start <= start + hi / RATE]
                    _, confirm = observations(decoder, login, temperatures, window, language, origin, beam=True)
                    alternative = substitutions(initial, confirm, confirm, language)
                    permitted = any(a.text != b.text for a, b in zip(initial, alternative))
                    if not permitted and genre in evaluated:
                        record["veto"] = "aucune autre lecture phonétique confirmée sans contexte"
                        output.write(json.dumps(record, ensure_ascii=False) + "\n")
                        continue
                    if (genre, language) not in ranking:
                        with corpus_path.open(encoding="utf-8") as corpus:
                            ranking[genre, language] = ranked_forms(map(json.loads, corpus), genre=genre,
                                                                    language=language, single=True)
                    variants = prompts(ranking[genre, language], near, language)
                    if variants:
                        evaluated.add(genre)
                        for size, hint in variants.items():
                            began = monotonic()
                            try:
                                _, hypothesis = observations(decoder, login, temperatures, window, language, origin, prompt=hint)
                            except ValueError as error:
                                record["tests"][size] = {"rejected": str(error)}
                                continue
                            final = substitutions(initial, confirm, hypothesis, language)
                            record["tests"][size] = dict(score=compare(expected, [w.text for w in final]),
                                                         changed=sum(a.text != b.text for a, b in zip(initial, final)),
                                                         seconds=round(monotonic() - began, 3))
                output.write(json.dumps(record, ensure_ascii=False) + "\n")
    finally:
        preparer.close()


if __name__ == "__main__":
    main()
