"""Shared offline wav2vec2 CTC inference and decoding for Kara."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import wave

from _kara_turbo import RATE, digest
from _kara_words import words

HOP = 320
KERNEL = 400
CORE = RATE * 16
PAD = RATE * 2


def cpu_session(model):
    import onnxruntime as ort

    policy = ort.SessionOptions()
    policy.intra_op_num_threads = policy.inter_op_num_threads = 1
    session = ort.InferenceSession(str(model), policy, providers=["CPUExecutionProvider"])
    (input_node,) = session.get_inputs()
    return session, input_node.name


def window_grid(size, left):
    import numpy as np

    right = min(left + CORE, size)
    start, end = max(0, left - PAD), min(size, right + PAD)
    centers = start + np.arange((end - start - KERNEL) // HOP + 1) * HOP + KERNEL // 2
    return start, end, (centers >= left) & (centers < right)


def emissions(runtime, pcm, mask, symbols):
    import numpy as np

    # Wav2vec2 normalizes within each padded window. The shared receipt
    # identifies the input PCM before this model-specific transformation.
    session, input_name = runtime
    signal = np.frombuffer(pcm, dtype="<i2").astype(np.float32) / 32768
    signal = (signal - signal.mean()) / np.sqrt(signal.var() + 1e-7)
    (logits,) = session.run(None, {input_name: signal[None, :]})
    logits = logits[0]
    if logits.shape != (len(mask), symbols) or not np.isfinite(logits).all():
        raise ValueError("Horloge ou alphabet du modèle CTC inattendu")
    usable = logits[mask]
    logp = usable - usable.max(axis=1, keepdims=True)
    logp -= np.log(np.exp(logp).sum(axis=1, keepdims=True))
    return logp


def decode_pcm(model, vocabulary, pcm):
    import numpy as np

    size = len(pcm) // 2
    if len(pcm) % 2 or not KERNEL <= size <= 29 * RATE:
        raise ValueError("Fenêtre CTC PCM16 hors limites")
    if (not isinstance(vocabulary, dict) or not 3 <= len(vocabulary) <= 512
            or any(type(index) is not int for index in vocabulary.values())
            or set(vocabulary.values()) != set(range(len(vocabulary)))
            or not {"<pad>", "|"} <= vocabulary.keys()):
        raise ValueError("Alphabet CTC invalide")
    runtime = cpu_session(model)
    chunks = []
    for left in range(0, size, CORE):
        start, end, mask = window_grid(size, left)
        chunks.append(emissions(runtime, pcm[2 * start:2 * end], mask, len(vocabulary)))
    logp = np.concatenate(chunks)
    offset = (KERNEL - HOP) / (2 * RATE)
    return [{"word": word, "start": start, "end": end}
            for word, start, end in greedy(logp, vocabulary, offset, HOP / RATE)]


def greedy(logp, vocabulary, offset, hop):
    alphabet = {value: key for key, value in vocabulary.items()}
    blank, separator = vocabulary["<pad>"], vocabulary["|"]
    symbols, start, end, previous = [], None, None, blank
    for frame, symbol in enumerate(logp.argmax(axis=1)):
        if symbol == separator and symbols:
            normalized = words("".join(symbols))
            if len(normalized) == 1:
                yield normalized[0], offset + start * hop, offset + (end + 1) * hop
            symbols, start, end = [], None, None
        character = alphabet[symbol]
        if len(character) == 1 and character != "|":
            if symbol != previous:
                symbols.append(character)
            if start is None:
                start = frame
            end = frame
        previous = symbol
    if symbols:
        normalized = words("".join(symbols))
        if len(normalized) == 1:
            yield normalized[0], offset + start * hop, offset + (end + 1) * hop



def viterbi(logp, tokens, blank):
    import numpy as np

    if not tokens or len(tokens) > len(logp):
        raise ValueError("Référence CTC hors limites")
    labels = np.full(2 * len(tokens) + 1, blank)
    labels[1::2] = tokens
    skip = (labels[2:] != blank) & (labels[2:] != labels[:-2])
    # One byte per predecessor bounds this prototype at 200 million cells.
    # A forced path proposes alignments, including for words never sung.
    if len(labels) * len(logp) > 200_000_000:
        raise ValueError("Trellis CTC trop volumineux")
    causes = np.empty((len(logp), len(labels)), dtype=np.uint8)
    score = np.full(len(labels), -np.inf, dtype=np.float32)
    score[0] = 0
    for frame, likelihood in enumerate(logp):
        best = score.copy()
        cause = causes[frame]
        cause.fill(0)
        improve = score[:-1] > best[1:]
        best[1:][improve] = score[:-1][improve]
        cause[1:][improve] = 1
        improve = skip & (score[:-2] > best[2:])
        best[2:][improve] = score[:-2][improve]
        cause[2:][improve] = 2
        score = best + likelihood[labels]
    state = len(labels) - (2 if score[-2] > score[-1] else 1)
    if not np.isfinite(score[state]):
        raise ValueError("Aucun chemin CTC complet")
    spans = [[] for _ in tokens]
    for frame in range(len(logp) - 1, -1, -1):
        if state % 2:
            spans[state // 2].append(frame)
        state -= int(causes[frame, state])
    return spans


def predict(logp, vocabulary, target, *, language="en"):
    import numpy as np

    blank = vocabulary["<pad>"]
    text = "|".join(target)
    if language == "en":
        text = text.upper()
    tokens = [vocabulary[character] for character in text]
    spans = viterbi(logp, tokens, blank)
    position = 0
    output = []
    for word in target:
        indices = range(position, position + len(word))
        # Retain the weakest character's best acoustic posterior. This is a
        # rejection signal, not a calibrated probability for sung words.
        character_scores = [float(logp[spans[i], tokens[i]].max()) for i in indices]
        minimum = float(np.exp(min(character_scores)))
        mean = float(np.exp(sum(character_scores) / len(character_scores)))
        start = (spans[position][-1] * HOP + KERNEL // 2 - HOP // 2) / RATE
        end = (spans[position + len(word) - 1][0] * HOP + KERNEL // 2 + HOP // 2) / RATE
        output.append((start, end, minimum, mean))
        position += len(word) + 1
    return output




def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    parser.add_argument("model", type=Path)
    parser.add_argument("vocabulary", type=Path)
    args = parser.parse_args()
    with args.vocabulary.open("rb") as stream:
        vocab = stream.read(32769)
    if len(vocab) > 32768:
        raise ValueError("Alphabet CTC trop volumineux")
    with wave.open(str(args.wav), "rb") as source:
        if ((source.getframerate(), source.getnchannels(), source.getsampwidth()) != (RATE, 1, 2)
                or not KERNEL <= source.getnframes() <= 29 * RATE):
            raise ValueError("Format de la fenêtre CTC incompatible")
        pcm = source.readframes(source.getnframes())
        if len(pcm) != source.getnframes() * 2:
            raise ValueError("Fenêtre CTC tronquée")
    model_sha256 = digest(args.model)
    decoded = decode_pcm(args.model, json.loads(vocab), pcm)
    print(json.dumps({"input_sha256": sha256(pcm).hexdigest(), "engine": "ctc_greedy_cpu",
                      "model_sha256": model_sha256, "vocabulary_sha256": sha256(vocab).hexdigest(),
                      "words": decoded}, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
