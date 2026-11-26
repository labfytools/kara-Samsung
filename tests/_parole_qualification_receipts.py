"""Observations privées et reprises du banc de transcription."""
import json

from _kara_prepare import atomic
from _kara_turbo import ObservedWord


def pack(observed):
    return [[w.text, w.start, w.end, w.confidence, w.end_origin] for w in observed]


def unpack(series):
    return [ObservedWord(*word) for word in series]


def receipt_path(folder, name):
    return folder / f"{name}.json"


def save(folder, name, language, observed, scores, *, window=None, beam=()):
    # The workspace plan already binds the observations to the model and
    # PCM input. Publish a complete variant before attempting the next one.
    data = dict(language=language, words=pack(observed), score=scores)
    if window is not None:
        data.update(window=list(window), beam=pack(beam))
    atomic(receipt_path(folder, name), json.dumps(data, ensure_ascii=False, allow_nan=False).encode())


def restore(folder, name):
    path = receipt_path(folder, name)
    if not path.is_file():
        return None
    value = json.loads(path.read_bytes())
    return (value["language"], unpack(value["words"]), value["score"],
            value.get("window"), unpack(value.get("beam", ())))
