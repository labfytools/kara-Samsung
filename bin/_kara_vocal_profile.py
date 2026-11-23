"""Mesures spectrales du mix stéréo, repérées mot par mot grâce aux paroles.

ATTENTION : on ne sait PAS isoler le timbre du chanteur depuis le mix final.
Ces profils ne servent qu'à piloter un filtre de karaoké conservateur. Ils ne
modifient ni le minutage, ni les paroles, ni les fichiers originaux.
"""
import hashlib
import json
import os
from pathlib import Path
import subprocess
from tempfile import NamedTemporaryFile

from _kara_dtw_words import text_boundaries
from _kara_timing import audio_signature, line_signature, time_at_character


VERSION = 2
LOWER_DEFAULT = 250
UPPER_DEFAULT = 2800
CUTOFF_MIN = 1800
CUTOFF_MAX = 3800
RATE = 16000
CACHE = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache") / "paroles-strawberry" / "vocal-profiles"
BANDS = ((80, 250), (250, 500), (500, 1000), (1000, 2000), (2000, 3800), (3800, 8000))


def _decode(song):
    import numpy as np
    try:
        data = subprocess.run(
            ("ffmpeg", "-nostdin", "-v", "error", "-i", str(song), "-vn", "-ac", "2", "-ar", str(RATE), "-f", "f32le", "-"),
            capture_output=True, timeout=45, check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("Lecture audio impossible pour l'analyse des mots") from error
    samples = np.frombuffer(data, dtype="<f4")
    if samples.size < RATE * 4:
        raise ValueError("Fichier trop court pour l'analyse spectrale")
    return samples[:samples.size // 2 * 2].reshape(-1, 2)


def _bounds(rows, times, marks, duration):
    """Utilise les vrais repères mot à mot, ou un découpage approximatif du LRC."""
    for line, ((_, text), begin) in enumerate(zip(rows, times)):
        bound = text_boundaries(text)
        if not bound:
            continue
        next_line = times[line + 1] if line + 1 < len(times) else duration
        stop = max(begin, min(duration, next_line))
        aligned = marks[line] if marks and line < len(marks) else None
        if aligned:
            intervals = [(time_at_character(aligned, a), time_at_character(aligned, b)) for _, a, b in bound]
            accuracy = "calé"
        else:
            # Sans horodatage par mot, les mesures restent approximatives.
            span = max(0., min(7., stop - begin))
            intervals = [(begin + span * j / len(bound), begin + span * (j + 1) / len(bound))
                         for j in range(len(bound))]
            accuracy = "estimé"
        for index, ((word, _, _), (start, end)) in enumerate(zip(bound, intervals)):
            start = max(0., min(duration, start))
            end = max(start, min(duration, end))
            # Étendre les mots très courts pour disposer d'une fenêtre spectrale exploitable.
            center = .5 * (start + end)
            width = max(.18, min(.85, end - start + .1))
            first = max(0., center - width / 2)
            last = min(duration, center + width / 2)
            if last - first >= .12:
                yield line, index, word, round(first, 3), round(last, 3), accuracy


def _spectral(sample):
    import numpy as np
    if len(sample) < 512:
        return None
    # Une seule voie (Mid) donne une mesure du centre du mix, pas de la voix seule.
    center = sample.mean(axis=1)
    side = (sample[:, 0] - sample[:, 1]) * .5
    n = 2048
    segments = max(1, (len(center) - n) // 1024 + 1)
    positions = np.linspace(0, max(0, len(center) - n), min(4, segments), dtype=int)
    window = np.hanning(n)
    pm = ps = None
    for position in positions:
        mid = center[position:position + n]
        stereo = side[position:position + n]
        if len(mid) < n:
            mid = np.pad(mid, (0, n - len(mid)))
            stereo = np.pad(stereo, (0, n - len(stereo)))
        energy_mid = abs(np.fft.rfft(mid * window)) ** 2
        energy_side = abs(np.fft.rfft(stereo * window)) ** 2
        pm = energy_mid if pm is None else pm + energy_mid
        ps = energy_side if ps is None else ps + energy_side
    pm /= len(positions)
    ps /= len(positions)
    hz = np.fft.rfftfreq(n, 1. / RATE)
    region = (hz >= LOWER_DEFAULT) & (hz <= 4600)
    total = pm[region].sum()
    if total < 1e-5:
        return None
    percentile = np.cumsum(pm[region]) / total
    upper = round(float(np.interp(.92, percentile, hz[region])) / 100) * 100
    upper = int(max(CUTOFF_MIN, min(CUTOFF_MAX, upper)))
    center_ratio = float(total / (total + ps[region].sum() + 1e-12))
    return upper, round(center_ratio, 3)


def _compute(song, rows, times, marks, progress=None):
    import numpy as np
    if progress:
        progress(0, 1)
    stereo = _decode(song)
    duration = len(stereo) / RATE
    jobs = list(_bounds(rows, times, marks, duration))
    if not jobs:
        raise ValueError("Aucun mot exploitable pour le profil audio")
    profiles = []
    usable = []
    reliable = []
    for n, (line, index, word, start, end, accuracy) in enumerate(jobs, 1):
        clip = stereo[round(start * RATE):round(end * RATE)]
        metrics = _spectral(clip)
        item = {"ligne": line, "mot": index, "debut_s": start, "fin_s": end,
                "minutage": accuracy, "bande_hz": metrics[0] if metrics else None,
                "energie_centrale": metrics[1] if metrics else None}
        profiles.append(item)
        if metrics:
            usable.append(metrics[0])
            if accuracy == "calé":
                reliable.append(metrics[0])
        if progress and (n == len(jobs) or n % 10 == 0):
            progress(n, len(jobs))
    values = reliable if len(reliable) >= 8 else usable
    if values:
        # Une bande unique sur toute la chanson évite les changements de
        # coefficients en pleine syllabe et les artefacts sur les instruments.
        upper = int(round(float(np.median(values)) / 100) * 100)
        upper = max(CUTOFF_MIN, min(CUTOFF_MAX, upper))
    else:
        upper = UPPER_DEFAULT
    return {"version": VERSION, "lower_hz": LOWER_DEFAULT, "upper_hz": upper,
            "mots_total": len(jobs), "mots_analyses": len(usable),
            "mots_calés": sum(p["minutage"] == "calé" for p in profiles),
            "frequences": "estimation_spectrale_du_mix_non_voix_isolee",
            "mots": profiles}


def cached_profile(song, rows, times, marks, cache=CACHE):
    """Récupère un profil complet et valide, sans relancer l'analyse."""
    song = Path(song).resolve()
    rows = tuple((float(t), str(text)) for t, text in rows)
    try:
        key = {"version": VERSION, "audio": audio_signature(song),
               "lines": line_signature(rows),
               "times": hashlib.sha256(json.dumps(times).encode()).hexdigest(),
               "marks": hashlib.sha256(json.dumps(marks).encode()).hexdigest()}
        identifier = hashlib.sha256(os.fsencode(song)).hexdigest()
        stored = json.loads((cache / f"{identifier}.json").read_text())
        profile = stored.get("profile", {})
        if stored.get("key") != key or not isinstance(profile, dict) or not profile.get("mots_analyses"):
            return None
        lower, upper = profile.get("lower_hz"), profile.get("upper_hz")
        if not (isinstance(lower, int) and 200 <= lower <= 400
                and isinstance(upper, int) and CUTOFF_MIN <= upper <= CUTOFF_MAX):
            return None
        return profile
    except (OSError, ValueError, TypeError, AttributeError, KeyError):
        return None


def analyze(song, rows, times, marks, cache=CACHE, progress=None):
    """Recalcule si le son, les paroles ou les repères ont changé."""
    song = Path(song).resolve()
    rows = tuple((float(t), str(text)) for t, text in rows)
    key = {"version": VERSION, "audio": audio_signature(song),
           "lines": line_signature(rows),
           "times": hashlib.sha256(json.dumps(times).encode()).hexdigest(),
           "marks": hashlib.sha256(json.dumps(marks).encode()).hexdigest()}
    identifier = hashlib.sha256(os.fsencode(song)).hexdigest()
    target = cache / f"{identifier}.json"
    try:
        saved = json.loads(target.read_text())
        if saved.get("key") == key and saved.get("profile", {}).get("mots_analyses"):
            if progress:
                progress(1, 1)
            return saved["profile"]
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    profile = _compute(song, rows, times, marks, progress)
    cache.mkdir(mode=0o700, parents=True, exist_ok=True)
    draft = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=cache, delete=False) as output:
            draft = Path(output.name)
            json.dump({"key": key, "profile": profile}, output, ensure_ascii=False)
        os.chmod(draft, 0o600)
        os.replace(draft, target)
    finally:
        if draft:
            draft.unlink(missing_ok=True)
    return profile
