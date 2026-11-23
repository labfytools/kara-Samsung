#!/usr/bin/env python3
"""Regression checks for song vocabulary languages used by Turbo and LRC timing."""
import sys
from pathlib import Path
sys.path.insert(0, str(Path.home()/".local/bin"))
from _kara_words import LyricVocabulary

CASES = {
    "fr": (
        "Je suis avec toi dans la nuit",
        "Pourquoi tu me dis que je suis toujours là",
        "Nous sommes ensemble avec les autres",
    ),
    "en": (
        "I love you baby and I know your heart",
        "Tonight we gonna make it right forever",
        "You tell me what you want and why",
    ),
    "es": (
        "Quiero bailar contigo esta noche",
        "Siempre tengo tu amor en mi corazón",
        "Dime por qué lloras mi vida",
        "Vamos conmigo nunca voy a dejarte",
    ),
    "auto": (
        "Hey hey hey",
        "Yeah yeah yeah",
    ),
}
for expected, lines in CASES.items():
    vocabulary = LyricVocabulary(lines)
    actual = vocabulary.language or "auto"
    assert actual == expected, (expected, actual)
    if expected != "auto":
        assert vocabulary.prompt(0, 0)
        assert sum(len(line) for line in vocabulary.tokens) > 0
    print(expected, "PASS")

rap = LyricVocabulary(("Wesh le frérot il a le seum", "Je suis dans la moula et je sais pourquoi",
                       "Le daron revient avec la meuf", "Toujours le charo dans le quartier"))
assert rap.language == "fr"
print("fr-rap PASS")

mixed = LyricVocabulary(("I love you tonight", "Quiero bailar contigo esta noche",
                         "Forever with my heart", "Siempre tengo tu amor"))
assert mixed.language == ""
print("mixed-auto PASS")
