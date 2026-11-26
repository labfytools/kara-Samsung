#!/usr/bin/env python3
"""Évalue des suggestions du lexique sur deux observations audio déjà calculées."""
from argparse import ArgumentParser
from collections import Counter, defaultdict
from dataclasses import replace
import json
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[1] / "bin"))
from _kara_turbo import digest
from _kara_words import words
from _parole_acoustic import sound, substitutions
from _parole_qualification_receipts import restore
from _parole_separation_metrics import edit_distance


def nearby_edit(baseline, vocals, filtered, language):
    by_word = defaultdict(list)
    for token in vocals:
        if token.confidence >= .65:
            by_word[token.text].append(token.start)
    alternate = baseline.copy()
    for i, word in enumerate(baseline):
        key = sound(word.text, language)
        if word.confidence >= .62 or len(key) < 4:
            continue
        candidates = set()
        for hint in filtered:
            if hint.text == word.text or hint.confidence < .65 or abs(hint.start - word.start) > .22:
                continue
            if not any(abs(t - word.start) <= .22 for t in by_word[hint.text]):
                continue
            candidate = sound(hint.text, language)
            if len(candidate) >= 4 and edit_distance(key, candidate) <= 1:
                candidates.add(hint.text)
        # The two separated variants share their stem. Use their agreement
        # to propose a word for review, never as independent confirmation.
        if len(candidates) == 1:
            alternate[i] = replace(word, text=next(iter(candidates)))
    return alternate


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("receipts", type=Path)
    parser.add_argument("lexicon", type=Path)
    parser.add_argument("--one-edit", action="store_true", help="Évaluer aussi des clés voisines d’une édition")
    args = parser.parse_args()
    plan = json.loads((args.receipts / "manifest.json").read_bytes())
    if digest(args.lexicon) != plan["lexicon"]:
        raise ValueError("Le lexique doit être celui tenu à part pour cette campagne")
    compare = runpy.run_path(str(HERE.with_name("qualify-kara-singing.py")))["lexical_errors"]
    propose = nearby_edit if args.one_edit else substitutions
    case_suggestions, contexts = [], set()
    for case in plan["cases"]:
        folder = args.receipts / case["id"] / "observations"
        source = [restore(folder, name) for name in ("mix", "vocals", "filtered")]
        if not all(source):
            raise ValueError("Le comparatif historique doit être apparié")
        language, baseline, scores, _, _ = source[0]
        acoustic = source[1][0] == source[2][0] == language
        alternate = propose(baseline, source[1][1], source[2][1], language) if acoustic else baseline
        proposals = []
        for i, (original, candidate) in enumerate(zip(baseline, alternate, strict=True)):
            if original.text == candidate.text:
                continue
            compatible = set()
            if i and candidate.start - baseline[i-1].start <= 1.2:
                compatible.add((language, baseline[i-1].text, candidate.text))
            if i + 1 < len(baseline) and baseline[i+1].start - candidate.start <= 1.2:
                compatible.add((language, candidate.text, baseline[i+1].text))
            proposals.append((i, candidate.text, compatible))
            contexts.update(compatible)
        case_suggestions.append((case, [w.text for w in baseline], scores, proposals))
    supported = set()
    if contexts:
        with args.lexicon.open() as stream:
            for entry in map(json.loads, stream):
                language = entry["language"]
                for form in entry["forms"]:
                    if " " not in form:
                        continue
                    parts = words(form)
                    if len(parts) == 2 and (context := (language, *parts)) in contexts:
                        supported.add(context)
                if supported == contexts:
                    break
    totals = defaultdict(Counter)
    for case, baseline, old, proposals in case_suggestions:
        accepted = 0
        for position, candidate, context in proposals:
            # A recorded bigram can support an acoustic alternative. Its
            # popularity never creates an alternative or changes a timing.
            if context & supported:
                baseline[position] = candidate
                accepted += 1
        new = compare(case["expected"], baseline)
        for genre in ("total", case["genre"]):
            totals[genre].update(excerpts=1, proposed=len(proposals), supported=accepted, words=old["expected"],
                                 original_errors=old["errors"], suggested_errors=new["errors"])
    print(json.dumps(totals, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
