"""Read-only capture replay and separate experimental CHRONOS receipts."""
from hashlib import sha256
import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile

from _kara_blackbox import digest, events, sync_directory, validate_recording
from _kara_blackbox_lyrics import load
from _kara_chronos import calculate, load_json
from _kara_paths import music_library
from _kara_scoring import score_breakdown, score_reference_line
from _kara_words import words


def capture_audio(document, explicit=None):
    expected = document["audio"]["original"]
    if explicit is None:
        root = music_library()
        audio = (root / expected["library_path"]).resolve(strict=True)
        if not audio.is_relative_to(root):
            raise ValueError("L’enregistrement désigné sort de la bibliothèque.")
    else:
        audio = explicit.resolve(strict=True)
    return audio, expected["sha256"]


def source_for(document):
    if document.get("chronos"):
        return document["chronos"]["source"]
    if not document["original"]:
        raise ValueError("La capture ne contient aucune parole source.")
    historical = document.get("historical")
    return {"kind": document["source"], "original": document["original"],
            "timed": bool(historical or document["source"] == "sidecar-lrc"),
            "order": historical.get("source_order") if historical else None}


def differences(first, second):
    first_lines, second_lines = first["lines"], second["lines"]
    if [line["text"] for line in first_lines] != [line["text"] for line in second_lines]:
        return {"state": "lyrics-differ", "lines": []}
    lines = []
    for index, (old, new) in enumerate(zip(first_lines, second_lines, strict=True)):
        if [word["text"] for word in old["words"]] != [word["text"] for word in new["words"]]:
            return {"state": "word-boundaries-differ", "lines": []}
        changes = []
        for j, (a, b) in enumerate(zip(old["words"], new["words"], strict=True)):
            x, y = a.get("start"), b.get("start")
            if x is not None or y is not None:
                changes.append({"word": j, "text": a["text"], "a": x, "b": y,
                                "delta_ms": round((y - x) * 1000, 3) if x is not None and y is not None else None})
        lines.append({"index": index, "a": old["start"], "b": new["start"], "words": changes})
    return {"state": "comparable", "lines": lines}


def score_replay(folder, turn, alignment):
    """Evaluate captured windows with changed starts and original recognition."""
    observed, historical, void = {}, None, False
    for row in events(folder / "events.jsonl"):
        if row.get("turn") != turn:
            continue
        if row["event"] == "line-decision":
            data = row["data"]
            observed[data["line"]] = data
        elif row["event"] == "turn-end":
            historical, void = row["data"].get("summary"), row["data"].get("score_void", False)
    if not observed:
        return {"state": "observations-unavailable", "historical": historical}
    lines = []
    for line in alignment["lines"]:
        # A whitespace token can contain several normalized scoring words.
        # Their individual starts are unknown. Preserve them as uncertain.
        reference = []
        for row in line["words"]:
            tokens = words(row["text"])
            for token in tokens:
                reference.append({"id": len(reference), "text": token, "normalized": token,
                                  "start": row["start"] if len(tokens) == 1 else None})
        lines.append({"words": reference})
    results, comparable = {}, 0
    for index, data in observed.items():
        if not 0 <= index < len(lines):
            continue
        new = lines[index]
        if ([item["normalized"] for item in data["reference"]["words"]]
                != [item["normalized"] for item in new["words"]]):
            continue
        comparable += 1
        results[index] = score_reference_line(new, data["observed"], data["tolerance"], source=data["source"])
    if not comparable:
        return {"state": "lyrics-differ", "historical": historical}
    summary = score_breakdown({"lines": lines}, results)
    if void:
        summary["score"] = None
    return {"state": "experimental", "basis": "captured-recognition-windows",
            "historical": historical, "comparison": summary, "comparable_lines": comparable,
            "captured_lines": len(observed), "target_lines": len(lines),
            "events": results, "score_void": void}


def write_exclusive(path, result):
    """Publish after fsync and reserve the name without replacing a prior run."""
    with NamedTemporaryFile(dir=path.parent) as draft:
        draft.write(json.dumps(result, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode())
        draft.flush()
        os.fsync(draft.fileno())
        os.link(draft.name, path)
        sync_directory(path.parent)


def replay(folder, turn, engine, *, explicit=None, output=None, alternate=None):
    manifest_path = folder / "manifest.json"
    manifest = load_json(manifest_path, maximum=16_000_000)
    validate_recording(folder, manifest)
    cycle = f"{folder.name}/{turn}"
    if cycle not in manifest.get("cycles", {}):
        raise ValueError("Sélectionne un cycle BlackBox V3.")
    captured = load(folder, turn)
    reference = captured["reference"]
    audio, expected = capture_audio(reference, explicit)
    if output is not None:
        output = output.absolute()
        if output.resolve().is_relative_to(folder.parent.resolve()) or output.exists():
            raise ValueError("Choisis une nouvelle sortie en dehors de l’inventaire des captures.")
    if alternate is not None:
        analysis = calculate(audio, {"alternate_audio": str(alternate.resolve(strict=True))}, "edition",
                             expected_audio=expected)
        result = {"edition": analysis}
        method = "edition"
    else:
        source = source_for(reference)
        alignment = calculate(audio, source, engine, expected_audio=expected)
        baseline = reference.get("chronos") or reference.get("historical")
        result = {"alignment": alignment, "divergences": differences(baseline, alignment) if baseline else None,
                  "score_replay": score_replay(folder, turn, alignment)}
        method = engine
    result.update({"version": 1, "cycle": cycle, "captured_reference_sha256": captured["sha256"],
                   "recording_sha256": expected, "events_sha256": manifest["files"]["events.jsonl"]["sha256"]})
    if output is None:
        root = folder.parent.parent / "chronos-replays" / folder.name
        root.mkdir(parents=True, mode=0o700, exist_ok=True)
        raw = json.dumps(result, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()
        output = root / f"{turn}-{method}-{sha256(raw).hexdigest()[:24]}.json"
        if output.is_file() and load_json(output, maximum=16_000_000) == json.loads(raw):
            return {**result, "saved_as": str(output)}
    write_exclusive(output, result)
    return {**result, "saved_as": str(output)}
