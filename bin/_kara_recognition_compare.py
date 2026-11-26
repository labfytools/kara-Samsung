"""Compare local decoders and lexical rules on one verified PCM window."""
from _kara_words import words
from _parole_acoustic import sound


def edits(expected, observed, matches):
    previous = list(range(len(observed) + 1))
    for i, left in enumerate(expected, 1):
        current = [i]
        for j, right in enumerate(observed, 1):
            current.append(min(previous[j] + 1, current[-1] + 1,
                               previous[j - 1] + (not matches(left, right))))
        previous = current
    return previous[-1]


def phonetic_match(left, right, language, radius):
    if left == right:
        return True
    if language not in ("fr", "en"):
        return False
    a, b = sound(left, language), sound(right, language)
    return min(len(a), len(b)) >= 4 and edits(a, b, str.__eq__) <= radius


def comparison(expected, observed, language):
    literal = edits(expected, observed, str.__eq__)
    supported = language in ("fr", "en")
    return {"reference_words": len(expected), "observed_words": len(observed), "word_edits": literal,
            "phonetic_key_edits": (edits(expected, observed, lambda a, b: phonetic_match(a, b, language, 0))
                                   if supported else None),
            "phonetic_distance_one_edits": (edits(expected, observed, lambda a, b: phonetic_match(a, b, language, 1))
                                            if supported else None)}


def paired_results(pcm_sha256, outputs, language, truth=None):
    hypotheses = {}
    for name, result in outputs.items():
        if result["input_sha256"] != pcm_sha256:
            raise ValueError(f"Le moteur {name} a lu un autre PCM.")
        hypotheses[name] = [token for entry in result["words"] for token in words(entry["word"])]
    result = {"input_sha256": pcm_sha256, "recognizers": outputs, "phonetic_language": language,
              "phonetics": "lexical_comparison_on_detected_words", "human_reference": None}
    if "ctc" in hypotheses and "turbo" in hypotheses:
        result["ctc_to_turbo"] = comparison(hypotheses["ctc"], hypotheses["turbo"], language)
    if truth is not None:
        expected = words(truth)
        if not expected:
            raise ValueError("La transcription humaine contient zéro mot évaluable.")
        result["human_reference"] = {}
        for name, hypothesis in hypotheses.items():
            metric = comparison(expected, hypothesis, language)
            result["human_reference"][name] = {**metric, "wer": metric["word_edits"] / len(expected)}
    return result
