"""Lecture des repères temporels LRC utilisés par l’affichage Strawberry."""
import re
from unicodedata import normalize

MAX_LRC_BYTES = 64_000
STAMP = re.compile(r"\[(\d{1,3}):([0-5]\d)(?:[.:](\d{1,3}))?\]")
OFFSET = re.compile(r"^\[offset:\s*([+-]?\d+)\s*\]\s*$", re.I | re.M)
WORD_STAMP = re.compile(r"<\d{1,3}:[0-5]\d(?:[.:]\d{1,3})?>")


def parse_lrc(text):
    offset = OFFSET.search(text)
    offset_seconds = int(offset[1]) / 1000 if offset else 0
    lines = {}
    for line in text.splitlines():
        line = line.lstrip("\ufeff \t")
        stamps = []
        while (stamp := STAMP.match(line)):
            minutes, seconds, fraction = stamp.groups()
            time = int(minutes) * 60 + int(seconds) + int((fraction or "").ljust(3, "0")) / 1000
            stamps.append(max(0.0, time - offset_seconds))
            line = line[stamp.end():].lstrip()
        phrase = normalize("NFC", WORD_STAMP.sub("", line).strip())
        for time in stamps:
            old = lines.get(time)
            if old and phrase and phrase != old:
                lines[time] = old + "\n" + phrase
            elif not old:
                lines[time] = phrase
    return sorted(lines.items())
