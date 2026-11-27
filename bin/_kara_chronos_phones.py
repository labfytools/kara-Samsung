"""Full-track phonetic alignment ported from CHRONOS b4cbb40."""
import functools
import subprocess
import sys
import unicodedata

import numpy as np

from _kara_ctc import CORE, HOP as CTC_HOP, RATE, viterbi, window_grid

HOP = 768 / 22050


def emissions(audio, checkpoint):
    import torch
    import torchaudio
    from model import AcousticModel,train_audio_transforms
    net=AcousticModel(1,256,72,32).eval()
    weights=torch.load(checkpoint,map_location='cpu',weights_only=True)
    net.load_state_dict(weights['model_state_dict'])
    duration=float(subprocess.check_output(['ffprobe','-v','error','-show_entries','format=duration','-of','csv=p=0',str(audio)],timeout=15))
    if not 0.1<duration<600:raise ValueError('duration')
    raw=subprocess.check_output(['ffmpeg','-nostdin','-v','error','-protocol_whitelist','file,pipe','-threads','1','-i',str(audio),'-t','600','-ac','1','-ar','16000','-f','f32le','-'],timeout=90)
    x=torch.from_numpy(np.frombuffer(raw,dtype='<f4').copy())
    with torch.inference_mode():
        x=torchaudio.functional.resample(x,16000,22050)
        spec=train_audio_transforms(x)
        total=spec.shape[-1]//3
        parts=[]
        # Blocks align with maxpool stride. Surround each interior block
        # with two seconds of context for the first bidirectional LSTM.
        for i in range(0,total,480):
            lo=max(0,i-60);end=min(i+480,total);hi=min(total,end+60)
            lp=net(spec[None,None,:,lo*3:hi*3]).log_softmax(-1)[0]
            parts.append(lp[i-lo:end-lo].numpy())
        return np.concatenate(parts)

def phonemizer(language, site):
    sys.path.append(str(site))
    import espeakng_loader
    from phonemizer.backend.espeak.wrapper import EspeakWrapper
    from phonemizer.backend import EspeakBackend
    from phonemizer.separator import Separator
    EspeakWrapper.set_library(espeakng_loader.get_library_path())
    EspeakWrapper.set_data_path(espeakng_loader.get_data_path())
    backend=EspeakBackend(language,with_stress=False,language_switch='remove-flags')
    @functools.lru_cache(maxsize=4096)
    def phones(word):
        s=backend.phonemize([word],separator=Separator(phone=';',word='|',syllable=''),strip=True)[0]
        return [p for p in s.replace('|',';').split(';') if p]
    return phones

def decode(lp,words,phones):
    from utils import phone2int
    labels=[];groups=[]
    for w in words:
        ph=[(phone, phone2int.get(phone, 70)) for phone in phones(w)]
        if not ph:
            groups.append(None);continue
        if labels:labels.append(phone2int[' '])
        groups.append((len(labels),ph))
        labels.extend(i for _,i in ph)
    try:spans=viterbi(lp,labels,71)
    except ValueError as error:
        # Invalid length and missing CTC paths cannot establish any word
        # onset. A trellis resource limit is an operational failure instead.
        if str(error) in {'Référence CTC hors limites','Aucun chemin CTC complet'}:return [None]*len(words)
        raise
    peak=lp.max(axis=1)
    out=[]
    for g in groups:
        if g is None:out.append(None);continue
        a,ph=g
        margins=[];times=[]
        for j,(symbol,label) in enumerate(ph,a):
            frames=spans[j]
            margins.append(float(np.max(lp[frames,label]-peak[frames])))
            times.append([symbol,float(frames[-1]*HOP)])
        out.append(dict(start=times[0][1],end=(spans[a+len(ph)-1][0]+1)*HOP,
            margin=float(np.mean(margins)),min_margin=min(margins),phones=times))
    return out


def represent(phones, vocabulary):
    """Use the shortest deterministic inventory expansion of each eSpeak phone."""
    result = []
    for symbol in phones:
        symbol = unicodedata.normalize("NFC", symbol).translate(str.maketrans("", "", "ˈˌ͜͡"))
        if symbol in vocabulary:
            result.append(vocabulary[symbol])
            continue
        paths = {0: []}
        for left in range(len(symbol)):
            if left not in paths:
                continue
            for right in range(left + 1, len(symbol) + 1):
                segment = symbol[left:right]
                if segment in vocabulary:
                    option = paths[left] + [vocabulary[segment]]
                    if right not in paths or len(option) < len(paths[right]):
                        paths[right] = option
        if len(symbol) not in paths:
            return []
        result.extend(paths[len(symbol)])
    return result


def wav2vec(audio, tokens, phones, model, vocabulary):
    """Keep the laboratory grid and onset convention of the rap baseline."""
    import onnxruntime as ort

    config = ort.SessionOptions()
    config.intra_op_num_threads, config.inter_op_num_threads = 2, 1
    config.enable_cpu_mem_arena = False
    runtime = ort.InferenceSession(str(model), config, providers=["CPUExecutionProvider"])
    signal = np.frombuffer(subprocess.check_output(
        ("ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
         "-threads", "1", "-i", str(audio), "-t", "600", "-ac", "1", "-ar", "16000",
         "-f", "f32le", "-"), timeout=90), dtype="<f4")
    chunks = []
    for left in range(0, len(signal), CORE):
        start, end, mask = window_grid(len(signal), left)
        window = signal[start:end]
        if len(mask) == 0:
            continue
        feeds = {entry.name: ((window - window.mean()) / np.sqrt(window.var() + 1e-7))[None]
                 if entry.name == "input_values" else np.ones((1, len(window)), dtype=np.int64)
                 for entry in runtime.get_inputs()}
        logits = runtime.run(None, feeds)[0][0][mask]
        logp = logits - logits.max(axis=1, keepdims=True)
        logp -= np.log(np.exp(logp).sum(axis=1, keepdims=True))
        chunks.append(logp)
    if not chunks:
        return [None] * len(tokens)
    logp = np.concatenate(chunks)
    groups = [represent(phones(token), vocabulary) for token in tokens]
    try:
        spans = viterbi(logp, [label for group in groups for label in group], vocabulary["<pad>"])
    except ValueError as error:
        if str(error) in {"Référence CTC hors limites", "Aucun chemin CTC complet"}:
            return [None] * len(tokens)
        raise
    hop = CTC_HOP / RATE
    alphabet = {label: phone for phone, label in vocabulary.items()}
    offset = .0025
    cursor, rows = 0, []
    for group in groups:
        if not group:
            rows.append(None)
            continue
        starts = [[alphabet[label], spans[i][-1] * hop + offset]
                  for i, label in enumerate(group, start=cursor)]
        cursor += len(group)
        rows.append(dict(start=starts[0][1], end=(spans[cursor - 1][0] + 1) * hop + offset,
                         phones=starts))
    return rows
