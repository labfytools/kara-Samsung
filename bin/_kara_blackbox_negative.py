"""Run score-source gates against audio fixtures without a microphone."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import wave

import numpy as np

from _kara_scoring import score_reference_line, score_breakdown
from _kara_turbo import RATE, ObservedWord, Segment
from _kara_voice import transcribe_prepared_job


def fixture_wave(path, samples):
    pcm = (samples * 32768).astype("<i2")
    with wave.open(str(path), "wb") as output:
        output.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        output.writeframes(pcm.tobytes())
    return pcm.astype(np.float32) / 32768


def checks():
    class FixedDecoder:
        # Known lexical evidence exercises the real DSP source gates and
        # score matcher without treating noise as human speech or loading Turbo.
        def __init__(self, word):
            self.word, self.calls = ObservedWord(word, .4, .65, .95), 0

        def decode(self, _pcm, language, _origin, **_options):
            self.calls += 1
            return language, [Segment(self.word.text, .4, .65, .08, (self.word,))]

    rng = np.random.default_rng(17)
    reference = {"words": [{"id": 0, "text": "Bonjour", "normalized": "bonjour", "start": .4}]}
    with TemporaryDirectory(prefix="kara-negative-score-") as directory:
        root = Path(directory)
        backing = fixture_wave(root / "backing.wav", rng.normal(0, .08, 2 * RATE))
        original = fixture_wave(root / "original.wav", rng.normal(0, .08, 2 * RATE))
        player = (.04 * np.sin(2 * np.pi * 220 * np.arange(RATE) / RATE)).astype(np.float32)
        job = {"instrumental": True, "track_start": 0., "track_end": 1., "language": "fr",
               "backing": str(root / "backing.wav"), "original": str(root / "original.wav")}
        cases = {
            "silence": (np.zeros_like(player), job, "bonjour", "silence", 0, 0),
            "music_alone": (backing[2000:18000] * .3, job, "bonjour", "backing", 0, 0),
            "original_singer_alone": (original[2000:18000] * .3, job | {"guide": job["original"]},
                                      "bonjour", "guide", 0, 0),
            "wrong_lyrics": (player, job, "bonsoir", "voice", 1, 0),
            "positive_control": (player, job, "bonjour", "voice", 1, 1),
        }
        results = {}
        for name, (samples, spec, word, expected, calls, credit) in cases.items():
            decoder = FixedDecoder(word)
            observed, source = transcribe_prepared_job(decoder, samples, spec)
            decisions = score_reference_line(reference, observed, source=source)
            score = score_breakdown({"lines": [reference]}, {0: decisions})
            ok = (source, decoder.calls, score["correct"]) == (expected, calls, credit)
            results[name] = {"ok": ok, "source": source, "decoder_calls": decoder.calls,
                             "credited": score["correct"], "score": score["score"]}
    return {"kind": "synthetic_pcm_and_injected_decoder", "cases": results,
            "ok": all(result["ok"] for result in results.values())}


if __name__ == "__main__":
    result = checks()
    print(json.dumps(result))
    raise SystemExit(0 if result["ok"] else 1)
