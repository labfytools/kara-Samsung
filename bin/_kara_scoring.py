"""Compare les mots du micro à ceux de la ligne en cours."""
from dataclasses import dataclass
import re

SCORE_TOLERANCE = 1.0

HELD_VOWEL = re.compile(r"([aeiouy])\1{2,}")


def same_word(expected, heard):
    if expected == heard:
        return True
    if len(expected) < 3 or not HELD_VOWEL.search(heard):
        return False
    return expected in (HELD_VOWEL.sub(r"\1", heard), HELD_VOWEL.sub(r"\1\1", heard))


@dataclass(frozen=True)
class AudioWindow:
    line: int
    start: float
    stop: float
    rate: float
    word_times: tuple = ()

    def observed(self, spans, tokenize):
        heard = []
        for begin, end, text in spans:
            tokens = tokenize(text)
            for index, word in enumerate(tokens):
                middle = begin + (end - begin) * (index + .5) / len(tokens)
                point = self.start + middle * self.rate
                if self.start <= point < self.stop:
                    heard.append((point, word))
        heard.sort(key=lambda item: item[0])
        return heard

    def evidence(self, expected, heard, *, tolerance=None):
        # Préfère le mot le plus proche de son repère lorsqu'il se répète.
        calibrated = len(self.word_times) == len(expected)
        row = [(0, 0., ())] * (len(heard) + 1)
        for index, target in enumerate(expected):
            previous, row = row, [(0, 0., ())]
            for column, (point, word) in enumerate(heard, 1):
                best = max(previous[column], row[-1])
                distance = abs(point - self.word_times[index]) if calibrated else 0.
                if same_word(target, word) and (tolerance is None or distance <= tolerance):
                    count, cost, indices = previous[column - 1]
                    cost -= min(4, distance / (tolerance or SCORE_TOLERANCE))
                    best = max(best, (count + 1, cost, indices + ((index, point),)))
                row.append(best)
        return row[-1][2]

    def matches(self, expected, heard):
        return tuple(index for index, _ in self.evidence(expected, heard))


def add_evidence(confirmed, index, point, expected):
    """Ajoute un mot sans reprendre une occurrence déjà créditée au même endroit."""
    if index in confirmed:
        return
    for other, time in confirmed.items():
        if expected[index] == expected[other] and abs(point - time) <= .24:
            return
        if (index - other) * (point - time) < 0 and abs(point - time) > .15:
            return
    confirmed[index] = point
