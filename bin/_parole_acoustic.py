"""Experimental lexical re-decoding with an unprompted acoustic veto."""

from collections import defaultdict
from dataclasses import replace
import re
from unicodedata import combining, normalize


def sound(value, language):
    # A rough key for ranking hints, independent of publication spelling.
    value = "".join(c for c in normalize("NFKD", value.casefold()) if not combining(c))
    value = re.sub(r"[^a-z]", "", value)
    if language == "fr":
        for before, after in (("eaux", "o"), ("eau", "o"), ("au", "o"), ("ph", "f"), ("qu", "k"),
                              ("ch", "s"), ("c", "k"), ("ou", "u"), ("ai", "e"), ("ei", "e")):
            value = value.replace(before, after)
        value = value.rstrip("xztds")
    return re.sub(r"(.)\1+", r"\1", value)


def prompts(ranked, uncertain, language):
    preferred = {sound(word.text, language) for word in uncertain} - {""}
    close, rest, seen = [], [], set()
    for form in ranked:
        if " " in form or len(form.encode()) > 12 or form.casefold() in seen:
            continue
        seen.add(form.casefold())
        (close if sound(form, language) in preferred else rest).append(form)
    choices = close + rest
    return {n: " ".join(choices[:n]) for n in (5, 10, 20, 50) if len(choices) >= n}


def substitutions(baseline, unprompted, prompted, language):
    """Return corrected spellings on existing word slots only.

    Both a fresh unprompted read and the contextual read must support a
    different word at the same acoustic location. Their agreement is
    evidence for a candidate, never a certificate of transcription quality.
    """
    output = list(baseline)
    by_word = defaultdict(list)
    for word in unprompted:
        by_word[word.text].append(word)
    for i, word in enumerate(baseline):
        if word.confidence >= .62:
            continue
        original_sound = sound(word.text, language)
        if not original_sound:
            continue
        matches = {}
        for hinted in prompted:
            if (hinted.text == word.text or abs(word.start - hinted.start) > .22
                    or sound(hinted.text, language) != original_sound or hinted.confidence < .65):
                continue
            if any(abs(item.start - word.start) <= .22 and item.confidence >= .65
                   for item in by_word[hinted.text]):
                matches[hinted.text] = hinted
        if len(matches) == 1:
            output[i] = replace(word, text=next(iter(matches)))
    return output
