"""Run the production voice recognition path on a captured window, offline."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import wave

import numpy as np

from _kara_prepare import PreparedCache, Preparer
from _kara_voice import RATE, transcribe_player, transcribe_prepared_job


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("language")
    parser.add_argument("start", type=float)
    parser.add_argument("end", type=float)
    parser.add_argument("--device", choices=("cpu", "gpu"), default="gpu")
    parser.add_argument("--backing", type=Path)
    parser.add_argument("--original", type=Path)
    parser.add_argument("--guide", type=Path)
    parser.add_argument("--exact", action="store_true")
    parser.add_argument("--beam", action="store_true")
    parser.add_argument("--threads", type=int)
    args = parser.parse_args()
    with wave.open(str(args.wav), "rb") as source:
        if ((source.getframerate(), source.getnchannels(), source.getsampwidth()) != (RATE, 1, 2)
                or not 0 < source.getnframes() <= RATE * 32):
            parser.error("Format de la fenêtre WAV incompatible.")
        pcm = source.readframes(source.getnframes())
    prepared = Preparer(PreparedCache(), device=args.device, threads=args.threads)
    try:
        prepared.settings()  # Resolves the native build used by the production cache.
        decoder = prepared.load_decoder()
        if args.exact:
            language, spans = decoder.decode(pcm, args.language, args.start, beam=args.beam, trace_tokens=True)
            print(json.dumps({"language": language, "segments": [asdict(span) for span in spans],
                              "decoder": decoder.identity}, ensure_ascii=False))
            return
        audio = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
        evidence = {}
        if args.backing and args.original:
            heard, classification = transcribe_prepared_job(decoder, audio, {
                "instrumental": True, "track_start": args.start, "track_end": args.end, "language": args.language,
                "backing": str(args.backing.resolve(strict=True)), "original": str(args.original.resolve(strict=True)),
                "guide": str(args.guide.resolve(strict=True)) if args.guide else "",
            }, diagnostics=evidence)
        else:
            heard, classification = transcribe_player(decoder, audio, args.language, diagnostics=evidence)
        print(json.dumps({"observed": heard, "classification": classification, "decoder": evidence,
                          "checked_accompaniment": bool(args.backing and args.original)}, ensure_ascii=False))
    finally:
        prepared.close()


if __name__ == "__main__":
    main()
