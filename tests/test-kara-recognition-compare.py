"""Evidence and clock checks for the shared PCM comparator."""
from pathlib import Path
import sys
import unittest

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bin"))
from _kara_ctc import greedy, predict, window_grid
from _kara_recognition_compare import paired_results


class ComparisonTests(unittest.TestCase):
    def test_equal_pcm_is_required_and_lexical_rules_are_identified(self):
        outputs = {"turbo": {"input_sha256": "pcm", "words": [{"word": "bateau"}]},
                   "ctc": {"input_sha256": "pcm", "words": [{"word": "bato"}]}}
        result = paired_results("pcm", outputs, "fr")
        self.assertIsNone(result["human_reference"])
        self.assertEqual(result["ctc_to_turbo"]["word_edits"], 1)
        self.assertEqual(result["ctc_to_turbo"]["phonetic_key_edits"], 0)
        human = paired_results("pcm", outputs, "fr", "bateau")["human_reference"]
        self.assertEqual(human["turbo"]["wer"], 0)
        self.assertEqual(human["ctc"]["wer"], 1)
        self.assertIsNone(paired_results("pcm", outputs, "es")["ctc_to_turbo"]["phonetic_key_edits"])
        outputs["ctc"]["input_sha256"] = "changed"
        with self.assertRaisesRegex(ValueError, "autre PCM"):
            paired_results("pcm", outputs, "fr")

    def test_shared_ctc_grid_supports_greedy_and_forced_paths(self):
        vocabulary = {"<pad>": 0, "|": 1, "A": 2, "B": 3}
        start, end, mask = window_grid(2000, 0)
        self.assertEqual((start, end, int(mask.sum())), (0, 2000, 6))
        logp = np.full((6, 4), -8, dtype=np.float32)
        logp[np.arange(6), (0, 2, 2, 1, 3, 0)] = 0
        unrestricted = list(greedy(logp, vocabulary, .0025, .02))
        self.assertEqual([word for word, _, _ in unrestricted], ["a", "b"])
        forced = predict(logp, vocabulary, ["a", "b"])
        for (_, begin, end), (a, b, _, _) in zip(unrestricted, forced, strict=True):
            self.assertAlmostEqual(a, begin)
            self.assertAlmostEqual(b, end)


if __name__ == "__main__":
    unittest.main()
