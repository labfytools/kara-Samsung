#!/usr/bin/env python3
"""Compare Turbo avec et sans lexique local sur de courts extraits tenus à l'écart."""

import argparse
from contextlib import closing
import json
import os
from pathlib import Path
import runpy
import sqlite3
import subprocess
import sys
from time import monotonic


BIN = Path(__file__).resolve().parents[1] / "bin"
sys.path.insert(0, str(BIN))

from _kara_stems import cached_stems
from _kara_turbo import MODEL, RATE, TurboDecoder, build_decoder
from _kara_words import words
from _parole_index import sidecar
from _parole_lexicon import records, select_prompt
from _paroles_lrc import parse_lrc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--index", required=True, type=Path, help="copie SQLite de la bibliothèque")
    parser.add_argument("--library", required=True, type=Path)
    parser.add_argument("--case", required=True, action="append", help="chemin relatif d’un morceau avec LRC")
    args = parser.parse_args()
    library = args.library.resolve(strict=True)
    index = args.index.resolve(strict=True)
    cases = []
    with closing(sqlite3.connect(index.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for relative in args.case:
            path = (library / relative).resolve(strict=True)
            if not path.is_relative_to(library):
                parser.error("morceau hors bibliothèque")
            row = db.execute("SELECT * FROM songs WHERE path=?", (str(path),)).fetchone()
            _, text = sidecar(path, ".lrc")
            stamped = [(stamp, line) for stamp, line in parse_lrc(text) if line.strip()]
            if row is None or row["lrc"] not in {"local", "externe"} or len(stamped) < 3:
                parser.error("cas absent de l’index ou sans LRC de référence")
            start = max(0., stamped[0][0] - .08)
            selected = [(stamp, line) for stamp, line in stamped[:5] if stamp < start + 26.]
            end = selected[-1][0] - .05
            expected = [token for _, line in selected[:-1] for token in words(line)]
            if not expected or end <= start:
                parser.error("fenêtre d’évaluation inutilisable")
            cases.append((path, row, start, end, expected))

    os.umask(0o077)
    args.output.mkdir(mode=0o700)
    corpus_path = args.output / "corpus.jsonl"
    with corpus_path.open("x", encoding="utf-8") as corpus:
        count = 0
        for record in records(index, library, exclude=[row["id"] for _, row, *_ in cases]):
            corpus.write(json.dumps(record, ensure_ascii=False) + "\n")
            count += 1
    print(f"{count} enregistrements d’apprentissage, {len(cases)} morceaux tenus à l’écart", flush=True)

    def prompt(**context):
        with corpus_path.open(encoding="utf-8") as corpus:
            return select_prompt(map(json.loads, corpus), **context)

    # The existing WER comparator reports insertions, deletions and
    # substitutions. The decoder receives no text from the held-out audio.
    compare = runpy.run_path(str(Path(__file__).with_name("qualify-kara-singing.py")))["lexical_errors"]
    general = prompt()
    decoder = TurboDecoder(build_decoder(), MODEL, "gpu", 4, require_vulkan=True)
    try:
        with (args.output / "result.jsonl").open("x", encoding="utf-8", buffering=1) as output:
            for path, row, start, end, expected in cases:
                genre = json.loads(row["info"]).get("tags", {}).get("genre") or ""
                contexts = {"base": "", "général": general, "artiste": prompt(artist=row["owner"])}
                if genre:
                    contexts["genre"] = prompt(genre=genre)
                stems = cached_stems(path)
                inputs = [("mix", path)]
                if stems:
                    inputs.append(("voix", stems[1]))
                for signal, audio in inputs:
                    pcm = subprocess.check_output(["ffmpeg", "-v", "error", "-nostdin", "-ss", str(start),
                                                   "-i", str(audio), "-t", str(end - start), "-ac", "1",
                                                   "-ar", str(RATE), "-f", "s16le", "-"], timeout=20)
                    for mode, hints in contexts.items():
                        if mode != "base" and not hints:
                            continue
                        began = monotonic()
                        language, segments = decoder.decode(pcm, "", prompt=hints)
                        heard = [word.text for segment in segments if segment.no_speech <= .65 for word in segment.words]
                        result = dict(case=row["id"], signal=signal, mode=mode, language=language,
                                      seconds=round(monotonic() - began, 3), bytes_prompt=len(hints.encode()),
                                      errors=compare(expected, heard))
                        print(json.dumps(result, ensure_ascii=False), file=output)
    finally:
        decoder.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
