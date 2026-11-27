"""CPU phonetic alignment and independent Turbo evidence for one recording."""
from base64 import b64encode
from importlib.metadata import version
from importlib.util import find_spec
import json
from math import isfinite
import os
from pathlib import Path
import sys
import subprocess
import wave

from _kara_audio_identity import pcm_digest
from _kara_chronos import (ASSETS, SETTINGS, atomic, audio_duration, implementation,
                           load_json, reference_lines, timed_lines)
from _kara_chronos_policy import decide, difficult, support
from _kara_import_resources import lease
from _kara_stems import ROOT as STEM_ROOT
from _kara_turbo import RATE, TurboDecoder, build_decoder, decode_segment, digest, pcm_file
from _kara_words import detect_language, words


def configured(name):
    path = Path(os.environ.get("KARA_CHRONOS_" + name, ""))
    if not path.is_absolute() or not path.exists():
        raise ValueError(f"Ressource CHRONOS locale absente ({name}).")
    return path


def verify(path, models, name):
    checksum = digest(path)
    if checksum != ASSETS[path.name]:
        raise ValueError(f"Empreinte CHRONOS invalide ({path.name}).")
    models.setdefault(name, {})[path.name] = checksum
    return path


class EvidenceDecoder(TurboDecoder):
    # The decoder header may include its padded extent. Retain the wire
    # landmarks so a short final window can be clipped to real audio.
    def read_line(self, deadline):
        line = super().read_line(deadline)
        if hasattr(self, "wire") and line.startswith((b"segment ", b"token ")):
            self.wire.append(line)
        return line


def window_words(wire, origin, duration):
    current, tokens = None, []
    for line in (*wire, b"segment"):
        kind, *fields = line.split()
        if kind == b"segment":
            if current:
                start, end, no_speech = current
                if start < duration and no_speech <= .65:
                    segment = decode_segment(start, min(end, duration), no_speech, tokens, origin, duration)
                    if segment:
                        yield from segment.words
            current = (int(fields[0]) / 100, int(fields[1]) / 100, float(fields[2])) if fields else None
            tokens = []
        else:
            tokens.append((int(fields[0]) / 100, float(fields[1]), bytes.fromhex(fields[2].decode("ascii"))))


def observations(voice, language, models):
    model = verify(configured("TURBO"), models, "whisper-large-v3-turbo-q5_0")
    binary = build_decoder()
    decoder = EvidenceDecoder(binary, model, "vulkan", 2, require_vulkan=True)
    models["whisper-large-v3-turbo-q5_0"].update(decoder.identity, executable_sha256=digest(binary))
    heard = []
    try:
        with pcm_file(voice) as (samples, read):
            for lo in range(0, samples, 25 * RATE):
                hi = min(samples, lo + 25 * RATE)
                decoder.wire = []
                decoder.decode(read(lo, hi), language, lo / RATE, beam=True)
                heard.extend((word.text, word.start, word.confidence)
                             for word in window_words(decoder.wire, lo / RATE, (hi - lo) / RATE))
    finally:
        decoder.close()
    return heard


def rows_for(source_lines, proposals, seconds):
    cursor = 0
    previous = -1.
    result = []
    for text in source_lines:
        marks = []
        for token in text.split():
            candidate = proposals[cursor]
            cursor += 1
            if candidate is not None:
                start, end = candidate["start"], candidate["end"]
                if not (isfinite(start) and isfinite(end) and 0 <= start < min(end, seconds)
                        and start >= previous):
                    candidate = None
                else:
                    previous = start
                    candidate = {**candidate, "end": min(end, seconds)}
            if candidate is None:
                state, reason = "absent", "no-admissible-alignment"
            elif candidate["lexical"] or candidate["acoustic"]:
                state = "experimental"
                reason = ("lexical-and-phonetic" if candidate["lexical"] and candidate["acoustic"]
                          else "lexical" if candidate["lexical"] else "phonetic")
            else:
                state, reason = "absent", "insufficient-lexical-and-phonetic-evidence"
            marks.append({"text": token, "status": state, "reason": reason, "proposal": candidate,
                          "start": candidate["start"] if state == "experimental" else None,
                          "end": candidate["end"] if state == "experimental" else None})
        start = marks[0]["start"] if marks else None
        result.append({"text": text, "start": start, "words": marks,
                       "status": "experimental" if start is not None else "absent",
                       "reason": "supported-first-word" if start is not None else "first-word-unconfirmed"})
    return result


def work(song, scratch, engine, expected):
    from _kara_chronos_contrast import scores
    from _kara_chronos_phones import HOP, decode, emissions, phonemizer
    from _kara_chronos_separate import unmix

    source = load_json(scratch / "source.json")
    lines = reference_lines(source)
    tokens = [token for line in lines for token in line.split()]
    if not 0 < len(tokens) <= 3000:
        raise ValueError("Nombre de mots hors du domaine CHRONOS.")
    seconds = audio_duration(song)
    if not .2 < seconds < 600:
        raise ValueError("Durée hors du domaine CHRONOS.")
    original = digest(song)
    if expected and expected != original:
        raise ValueError("L’empreinte audio diffère de celle de la capture.")
    language = detect_language(words(" ".join(tokens)))
    models = {}
    allow_demucs = os.environ.get("KARA_CHRONOS_ALLOW_DEMUCS", "1") == "1"
    result = {"version": 1, "implementation": implementation(), "engine": engine, "models": models,
              "settings": {**SETTINGS, "allow_demucs": allow_demucs}, "source": source, "duration": seconds, "language": language,
              "audio_sha256": original, "audio_pcm_sha256": pcm_digest(song), "views": {}}
    if language not in ("en", "fr"):
        result.update(status="abstained", reason="language-unsupported-or-uncertain",
                      lines=rows_for(lines, [None] * len(tokens), seconds))
        return result
    phones = phonemizer("fr-fr" if language == "fr" else "en-us", configured("PHONEMIZER"))
    voice = scratch / "voice.wav"
    if engine == "wav2vec2":
        weights = verify(configured("DEMUCS_BASE"), models, "htdemucs")
        with lease(STEM_ROOT / "demucs.lock", wait=False) as slot:
            if slot is None:
                raise RuntimeError("Demucs est déjà utilisé par Kara ou Parole.")
            separate(weights, song, seconds, voice)
        model_root = configured("WAV2VEC")
        model = verify(model_root / "model_int8.onnx", models, "wav2vec2-phoneme-int8")
        vocabulary = load_json(verify(model_root / "vocab.json", models, "wav2vec2-phoneme-int8"))
        # Keep the speech model's arena out of the Turbo process peak.
        work_item = {"tokens": tokens, "language": language, "model": str(model), "vocabulary": vocabulary}
        atomic(scratch / "wav2vec.json", json.dumps(work_item, ensure_ascii=False).encode())
        child("--wav2vec", voice, scratch)
        raw = load_json(scratch / "speech-rows.json")
        heard = observations(voice, language, models)
        flags = support(tokens, raw, heard)
        proposals = [dict(row, source="demucs", lexical=flag, acoustic=False)
                     if row else None for row, flag in zip(raw, flags, strict=True)]
        record_view(result, voice, "demucs")
    else:
        assets = configured("HUANG")
        for name in ("model.py", "utils.py", "checkpoint_Baseline"):
            verify(assets / name, models, "huang")
        sys.path.insert(0, str(assets))
        sys.path.append(str(configured("HUANG_SITE")))
        import torch
        from utils import phone2int
        torch.set_num_threads(2)
        torch.set_num_interop_threads(1)
        def evaluate(vocals):
            lp = emissions(vocals, assets / "checkpoint_Baseline")
            assigned = decode(lp, tokens, phones)
            return assigned, scores(lp, assigned, phone2int, HOP)

        unmix(verify(configured("UMX"), models, "open-unmix-hq"), song, seconds, voice)
        record_view(result, voice, "umx")
        base, contrast = evaluate(voice)
        # Timed sources already provide a display clock. In party
        # preparation, Huang can corroborate new onsets acoustically without
        # a second, full-song transcription. Replays keep their lexical audit.
        phonetic_only = (os.environ.get("KARA_CHRONOS_TIMED_PARTY") == "1"
                         and bool(timed_lines(source, seconds)))
        heard = [] if phonetic_only else observations(voice, language, models)
        result["lexical_evidence"] = "not-requested" if phonetic_only else "turbo"
        deep, deep_contrast = None, None
        if allow_demucs and (engine == "huang-ft" or difficult(base)):
            try:
                weights = verify(configured("DEMUCS_FT"), models, "htdemucs-ft-vocals")
                with lease(STEM_ROOT / "demucs.lock", wait=False) as slot:
                    if slot is None:
                        raise RuntimeError("Demucs est déjà utilisé.")
                    ft = scratch / "ft.wav"
                    separate(weights, song, seconds, ft)
                record_view(result, ft, "htdemucs-ft")
                deep, deep_contrast = evaluate(ft)
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                if engine == "huang-ft":
                    raise
                result["separation_deferral"] = str(error)
        groups = [line.split() for line in lines]
        if engine == "huang-ft":
            proposals = decide(groups, deep, None, heard, {"umx": deep_contrast})
            for row in proposals:
                if row:
                    row["source"] = "htdemucs-ft"
        else:
            proposals = decide(groups, base, deep, heard, {"umx": contrast, "demucs": deep_contrast}
                               if deep is not None else {"umx": contrast})
            for row in proposals:
                if row and row["source"] == "demucs":
                    row["source"] = "htdemucs-ft"
    result["source_observations"] = heard
    # Source agreement is a diagnostic on this exact recording. The
    # forced timestamps alone never establish an edition or remove text.
    result["edition_text_evidence"] = {"words": len(tokens),
        "lexically_corroborated": sum(bool(row and row["lexical"]) for row in proposals),
        "phonetic_support": sum(bool(row and row["acoustic"]) for row in proposals)}
    result["runtime"] = {name: version(name) for name in ("torch", "torchaudio", "onnxruntime", "demucs")}
    result["status"] = "experimental"
    result["lines"] = rows_for(lines, proposals, seconds)
    if not any(line["start"] is not None for line in result["lines"]):
        result.update(status="abstained", reason="no-supported-line-onset")
    if digest(song) != original:
        raise ValueError("Le fichier audio a changé pendant le calcul.")
    return result


def record_view(result, path, name):
    import numpy as np

    # The temporary stem is purged on exit. Preserve 50 ms min/max cells on
    # its exact sample clock for Studio, without retaining audio samples.
    with wave.open(str(path), "rb") as stream:
        rate = stream.getframerate()
        width = rate // 20
        cells = bytearray()
        while block := stream.readframes(width):
            samples = np.frombuffer(block, dtype="<i2")
            cells.append((int(samples.min()) // 256) & 255)
            cells.append((int(samples.max()) // 256) & 255)
        result["views"][name] = {"sha256": digest(path), "rate": rate,
                                 "frames": stream.getnframes(), "channels": stream.getnchannels(),
                                 "frames_per_cell": width, "envelope_s8": b64encode(cells).decode("ascii")}


def child(action, *arguments):
    subprocess.run([sys.executable, "-B", __file__, action, *map(str, arguments)],
                   stdin=subprocess.DEVNULL, check=True, timeout=295)


def separate(weights, song, seconds, voice):
    # Terminating the worker releases Torch's CPU arena. All stages remain
    # in the same supervised cgroup and retain the parent's model admission.
    child("--separate", weights, song, seconds, voice)


def stage(arguments):
    if arguments[0] == "--separate":
        from _kara_chronos_separate import demucs
        import torch

        torch.set_num_threads(2)
        _, weights, song, duration, voice = arguments
        demucs(Path(weights), Path(song), float(duration), Path(voice))
    else:
        from _kara_chronos_phones import phonemizer, wav2vec

        _, voice, scratch = arguments
        scratch = Path(scratch)
        spec = load_json(scratch / "wav2vec.json")
        phones = phonemizer("fr-fr" if spec["language"] == "fr" else "en-us", configured("PHONEMIZER"))
        rows = wav2vec(Path(voice), spec["tokens"], phones, Path(spec["model"]), spec["vocabulary"])
        atomic(scratch / "speech-rows.json", json.dumps(rows, ensure_ascii=False, allow_nan=False).encode())


def main():
    try:
        if find_spec("onnxruntime") is None:
            sys.path.append(str(configured("ONNX_SITE")))
        if sys.argv[1].startswith("--"):
            stage(sys.argv[1:])
            return 0
        song, scratch, engine, expected = sys.argv[1:]
        scratch = Path(scratch)
        # The lock coordinates with the preparation and score owners. No
        # model coexistence is required when entering the transient cgroup.
        runtime = Path(os.environ["XDG_RUNTIME_DIR"])
        with lease(runtime / "kara-model.lock", wait=False) as model:
            if model is None:
                raise RuntimeError("Un moteur Kara utilise déjà le créneau de calcul.")
            if engine == "edition":
                from _kara_chronos_editions import compare

                alternate = Path(load_json(scratch / "source.json")["alternate_audio"])
                other = digest(alternate)
                if digest(Path(song)) != expected:
                    raise ValueError("L’empreinte audio diffère de celle de la capture.")
                result = compare(Path(song), alternate)
                if result["source_sha"] != expected or result["target_sha"] != other:
                    raise ValueError("La source a changé pendant la comparaison.")
            else:
                result = work(Path(song), scratch, engine, expected)
        atomic(scratch / "result.json", json.dumps(result, ensure_ascii=False, allow_nan=False,
                                                  separators=(",", ":")).encode())
    except (ValueError, RuntimeError, OSError, KeyError, ImportError, subprocess.SubprocessError) as error:
        print(f"CHRONOS · {error}", flush=True)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
