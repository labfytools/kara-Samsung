"""Repères de mots issus du décodage aligné de Whisper."""
from bisect import bisect_right
from itertools import accumulate
import re
import subprocess
from tempfile import TemporaryFile

from _kara_words import words


def text_boundaries(text):
    result = []
    for match in re.finditer(r"\[[^\]]*\]|\S+", text):
        terms = words(match.group())
        for i, term in enumerate(terms):
            left = match.start() + (match.end() - match.start()) * i // len(terms)
            right = match.start() + (match.end() - match.start()) * (i + 1) // len(terms)
            result.append((term, left, right))
    normalized = words(text)
    return [(word, left, right) for word, (_, left, right) in zip(normalized, result)]


def segment_words(tokens, finish, origin, low, high):
    if not tokens:
        return []
    payload = b"".join(part for _, part in tokens)
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError:
        return []
    ends = list(accumulate(len(part) for _, part in tokens))
    offsets = [0, *accumulate(len(char.encode("utf-8")) for char in text)]
    result = []
    for word, start, stop in text_boundaries(text):
        first = bisect_right(ends, offsets[start])
        after = bisect_right(ends, offsets[stop] - 1) + 1
        moment = tokens[first][0] / 100
        following = (tokens[after][0] if after < len(tokens) else finish) / 100
        duration_limit = max(.27, min(1.25, .10 * len(word) + .16))
        end = min(moment + duration_limit, max(moment + .055, following))
        begin, end = origin + moment, origin + end
        if low <= begin < high:
            result.append((begin, end, word))
    return result


def read_timing(lines, progress, *, streaming=False):
    observed, tokens = [], []
    origin = low = high = 0.
    finish = 0
    failed = False
    for line in lines:
        kind, *values = line.split()
        if kind in ("window", "segment", "progress", "done"):
            observed.extend(segment_words(tokens, finish, origin, low, high))
            tokens.clear()
        if kind == "window":
            origin, low, high = (int(value) / 100 for value in values)
        elif kind == "segment":
            finish = int(values[0])
        elif kind == "token":
            tokens.append((int(values[0]), bytes.fromhex(values[1])))
        elif kind == "progress":
            progress(int(values[0]))
        elif kind == "error":
            failed = True
        elif kind == "done":
            if failed:
                raise RuntimeError("Whisper n’a pas pu analyser la ligne.")
            return sorted(observed)
    if streaming:
        raise EOFError("Le processus Whisper s’est arrêté.")
    observed.extend(segment_words(tokens, finish, origin, low, high))
    return sorted(observed)


def native_timing(executable, model, language, audio, progress):
    if not executable.is_file() or not model.is_file():
        return None
    with TemporaryFile() as pcm:
        pcm.write(audio)
        pcm.seek(0)
        with subprocess.Popen((str(executable), str(model), language), stdin=pcm,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True) as worker:
            observed = read_timing(worker.stdout, progress)
            if worker.wait() != 0:
                return None
    return observed


def word_checkpoints(text, hits, start, stop):
    bounds = text_boundaries(text)
    if not bounds:
        return None
    known = {i: (a, b) for i, a, b, _ in hits if 0 <= i < len(bounds)}
    if len(known) < 2 or len(known) * 2 < len(bounds):
        return None
    points = [(start, 0)]
    cursor = 0
    last = start
    for i, (a, b) in sorted(known.items()):
        a = max(last, min(stop, a))
        b = max(a, min(stop, b))
        if i > cursor:
            gap = (a - last) / (i - cursor)
            for index in range(cursor, i):
                points.append((last + gap * (index - cursor + 1), bounds[index][2]))
        points.append((a, bounds[i][1]))
        points.append((b, bounds[i][2]))
        cursor = i + 1
        last = b
    for index in range(cursor, len(bounds)):
        last = min(stop, last + .28)
        points.append((last, bounds[index][2]))
    if points[-1][1] != len(text):
        points.append((points[-1][0], len(text)))
    return [(round(a, 3), char) for a, char in points]
