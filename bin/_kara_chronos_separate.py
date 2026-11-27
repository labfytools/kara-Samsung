"""Temporary CHRONOS vocal views with the original audio clock preserved."""
import math
import subprocess
import wave

import numpy as np

RATE, FFT, HOP = 44100, 4096, 1024


def decode(audio, start, seconds, rate=RATE, channels=2, fmt="f32le"):
    return subprocess.check_output(
        ("ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
         "-ss", str(start), "-threads", "1", "-i", str(audio), "-t", str(seconds),
         "-map", "0:a:0", "-ac", str(channels), "-ar", str(rate), "-f", fmt, "-"), timeout=90)


def unmix(checkpoint, audio, duration, output):
    import onnxruntime as ort

    options = ort.SessionOptions()
    options.intra_op_num_threads = options.inter_op_num_threads = 1
    session = ort.InferenceSession(str(checkpoint), options, providers=["CPUExecutionProvider"])
    (tensor,) = session.get_inputs()
    window = np.hanning(FFT + 1)[:-1].astype(np.float32)
    with wave.open(str(output), "wb") as target:
        target.setparams((2, 2, RATE, 0, "NONE", "not compressed"))
        for start in range(0, math.ceil(duration), 16):
            left, right = max(0, start - 2), min(duration, start + 18)
            signal = np.frombuffer(decode(audio, left, right - left), dtype="<f4").reshape(-1, 2).T
            if signal.shape[-1] < FFT:
                break
            center = np.pad(signal, ((0, 0), (FFT // 2, FFT // 2)), mode="reflect")
            frames = np.lib.stride_tricks.sliding_window_view(center, FFT, axis=-1)[:, ::HOP, :]
            spectrum = np.fft.rfft(frames * window, axis=-1).astype(np.complex64)
            magnitude = np.abs(spectrum)
            features = np.ascontiguousarray(magnitude.transpose(0, 2, 1)[None])
            (estimated,) = session.run(None, {tensor.name: features})
            if estimated.shape != features.shape or not np.isfinite(estimated).all():
                raise ValueError("Sortie UMX-HQ invalide.")
            vocal = spectrum * (estimated[0].transpose(0, 2, 1) / np.maximum(magnitude, 1e-12))
            blocks = np.fft.irfft(vocal, n=FFT, axis=-1).astype(np.float32) * window
            result, norm = np.zeros_like(center), np.zeros(center.shape[-1], dtype=np.float32)
            for i, block in enumerate(blocks.transpose(1, 0, 2)):
                base = i * HOP
                result[:, base:base + FFT] += block
                norm[base:base + FFT] += window ** 2
            position = FFT // 2 + (start - left) * RATE
            limit = min(position + 16 * RATE, FFT // 2 + signal.shape[-1])
            pcm = result[:, position:limit] / norm[position:limit]
            target.writeframes(np.clip(pcm.T * 32768, -32768, 32767).astype("<i2").tobytes())


def demucs(weights, audio, duration, output):
    import torch
    from demucs.apply import apply_model
    from demucs.htdemucs import HTDemucs
    from demucs.pretrained import get_model
    from fractions import Fraction

    # Model files are verified against explicit upstream SHA-256 values
    # before entry. Torch still deserializes with the restricted loader.
    admitted = [HTDemucs, Fraction, np.dtype, np.core.multiarray.scalar, type(np.dtype("float64"))]
    with torch.serialization.safe_globals(admitted):
        model = get_model(weights.name.split("-")[0], repo=weights.parent).eval()
    index = model.sources.index("vocals")
    pcm = decode(audio, 0, duration, fmt="s16le")
    x = torch.from_numpy(np.frombuffer(pcm, dtype="<i2").copy().reshape(-1, 2).T).float() / 32768
    del pcm
    center = x.mean(0)
    mean, deviation = center.mean(), center.std()
    del center
    if not float(deviation) > 0:
        raise ValueError("La séparation vocale exige un signal non constant.")
    with wave.open(str(output), "wb") as target, torch.inference_mode():
        target.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
        for start in range(0, x.shape[-1], RATE * 18):
            stop = min(x.shape[-1], start + RATE * 18)
            left, right = max(0, start - RATE * 3), min(x.shape[-1], stop + RATE * 3)
            prediction = apply_model(model, ((x[:, left:right] - mean) / deviation)[None],
                                     device="cpu", shifts=0, overlap=.1, segment=7,
                                     split=True, progress=False, num_workers=0)
            voice = (prediction[0, index, :, start - left:stop - left].mean(0) * deviation + mean)
            target.writeframes((voice.clamp(-1, 32767/32768).numpy() * 32768).round().astype("<i2").tobytes())
