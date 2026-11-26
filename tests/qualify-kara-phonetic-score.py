#!/usr/bin/env python3
"""Évalue hors ligne les crédits supplémentaires d’une comparaison phonétique de Kara."""
from argparse import ArgumentParser
from collections import Counter
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
import _kara_scoring as scoring
from _kara_blackbox import events, session_folders, validate_recording
from _kara_recognition_compare import phonetic_match
from _kara_words import detect_language

BASE_MATCH = scoring.same_word
MODES = {"exact": None, "cle_phonetique": 0, "edition_phonetique": 1}


def evaluate(reference, heard, source, tolerance, language):
    choices = {}
    for mode, edits in MODES.items():
        def match(target, text):
            if BASE_MATCH(target, text):
                return True
            return edits is not None and phonetic_match(target, text, language, edits)

        # Reuse the production source gate and the existing match clock.
        # Only this offline call changes lexical comparison.
        with patch.object(scoring, "same_word", match):
            outcome = scoring.score_reference_line(reference, heard, tolerance, source=source)
        choices[mode] = sum(rule["status"] == "correct" for rule in outcome)
    return choices


def controls():
    examples = (
        ("homophone", "bateau", "bato", 1., "voice", "fr"),
        ("mot_voisin_fr", "bateau", "bâton", 1., "voice", "fr"),
        ("mot_voisin_en", "light", "night", 1., "voice", "en"),
        ("refrain_hors_temps", "bateau", "bato", 3., "voice", "fr"),
        ("voix_originale", "bateau", "bateau", 1., "original", "fr"),
        ("capture_interrompue", "bateau", "bateau", 1., "capture", "fr"),
    )
    return {label: evaluate({"words": [{"id": 0, "text": wanted, "normalized": wanted, "start": 1.}]},
                            [{"word": heard, "start": start, "confidence": .9}], source, scoring.SCORE_TOLERANCE,
                            language)
            for label, wanted, heard, start, source, language in examples}


def recordings(root):
    totals = Counter()
    for folder in session_folders(root):
        manifest = json.loads((folder / "manifest.json").read_bytes())
        if manifest["status"] == "recording":
            continue
        validate_recording(folder, manifest)
        for row in events(folder / "events.jsonl"):
            if row["event"] != "line-decision":
                continue
            data = row["data"]
            target = data["reference"]
            # A sparse or mixed line does not establish a language for the
            # alternate comparator. Its exact score is still evaluated.
            language = detect_language([word["normalized"] for word in target["words"]])
            totals.update(lines=1, voice_lines=int(data["source"] == "voice"),
                          data_with_language=int(bool(language)))
            totals.update(evaluate(target, data["observed"], data["source"], data["tolerance"], language))
    return dict(totals)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--captures", type=Path)
    args = parser.parse_args()
    print(json.dumps(dict(fixtures=controls(), blackbox=recordings(args.captures) if args.captures else None), indent=2))


if __name__ == "__main__":
    main()
