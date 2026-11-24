// Shared browser/Node engine. Inputs are normalized PCM on the capture clock.
(function (root) {
  'use strict';

  function levels(samples) {
    let sum = 0, peak = 0, clipped = 0;
    for (const sample of samples) {
      const magnitude = Math.abs(sample);
      sum += sample * sample;
      peak = Math.max(peak, magnitude);
      if (magnitude >= 0.995) clipped++;
    }
    return {rms: Math.sqrt(sum / Math.max(1, samples.length)), peak, clipped};
  }

  function resample(samples, fromRate, toRate) {
    if (fromRate === toRate) return samples;
    // Windowed sinc preserves the time origin and suppresses aliases at 44.1 kHz.
    const output = new Float32Array(Math.round(samples.length * toRate / fromRate));
    const cutoff = Math.min(1, toRate / fromRate);
    const radius = 16;
    for (let i = 0; i < output.length; i++) {
      const position = i * fromRate / toRate;
      for (let j = Math.max(0, Math.ceil(position - radius));
        j <= Math.min(samples.length - 1, Math.floor(position + radius)); j++) {
        const distance = position - j;
        const phase = Math.PI * distance * cutoff;
        const sinc = phase ? Math.sin(phase) / phase : 1;
        output[i] += samples[j] * cutoff * sinc * (0.5 + 0.5 * Math.cos(Math.PI * distance / radius));
      }
    }
    return output;
  }

  function fft(real, imag, inverse = false) {
    const n = real.length;
    for (let i = 1, j = 0; i < n; i++) {
      let bit = n >> 1;
      for (; j & bit; bit >>= 1) j ^= bit;
      j ^= bit;
      if (i >= j) continue;
      const r = real[i], v = imag[i];
      real[i] = real[j];
      imag[i] = imag[j];
      real[j] = r;
      imag[j] = v;
    }
    for (let length = 2; length <= n; length *= 2) {
      const angle = (inverse ? 2 : -2) * Math.PI / length;
      const stepR = Math.cos(angle), stepI = Math.sin(angle);
      const half = length / 2;
      for (let start = 0; start < n; start += length) {
        let wr = 1, wi = 0;
        for (let j = start; j < start + half; j++) {
          const k = j + half;
          const r = real[k] * wr - imag[k] * wi;
          const i = real[k] * wi + imag[k] * wr;
          real[k] = real[j] - r;
          imag[k] = imag[j] - i;
          real[j] += r;
          imag[j] += i;
          const nextR = wr * stepR - wi * stepI;
          wi = wr * stepI + wi * stepR;
          wr = nextR;
        }
      }
    }
  }

  function correlation(recording, reference) {
    const n = 2 ** Math.ceil(Math.log2(recording.length + reference.length - 1));
    const xr = new Float64Array(n), xi = new Float64Array(n);
    const yr = new Float64Array(n), yi = new Float64Array(n);
    xr.set(recording);
    let mean = 0;
    for (const sample of reference) mean += sample / reference.length;
    let energy = 0;
    for (let j = 0; j < reference.length; j++) {
      yr[j] = reference[j] - mean;
      energy += yr[j] ** 2;
    }
    fft(xr, xi);
    fft(yr, yi);
    for (let k = 0; k < n; k++) {
      const r = xr[k] * yr[k] + xi[k] * yi[k];
      xi[k] = xi[k] * yr[k] - xr[k] * yi[k];
      xr[k] = r;
    }
    fft(xr, xi, true);
    const count = recording.length - reference.length + 1;
    const scores = new Float64Array(count);
    const matchedRms = new Float64Array(count);
    let sum = 0, sumSquares = 0;
    for (let j = 0; j < reference.length; j++) {
      sum += recording[j];
      sumSquares += recording[j] ** 2;
    }
    // Prefix-equivalent rolling variance normalizes every candidate window.
    // Keep the matched amplitude separately so near-silent windows cannot win.
    for (let i = 0; i < count; i++) {
      const variance = sumSquares - sum ** 2 / reference.length;
      const dot = xr[i] / n;
      if (variance > 1e-12 && energy > 0) {
        scores[i] = Math.max(-1, Math.min(1, dot / Math.sqrt(variance * energy)));
        matchedRms[i] = Math.abs(dot) / Math.sqrt(energy * reference.length);
      }
      if (i + 1 === count) break;
      const outgoing = recording[i], incoming = recording[i + reference.length];
      sum += incoming - outgoing;
      sumSquares += incoming ** 2 - outgoing ** 2;
    }
    return {scores, matchedRms};
  }

  function median(values) {
    if (!values.length) return null;
    const sorted = [...values].sort((a, b) => a - b);
    const middle = Math.floor(sorted.length / 2);
    return sorted.length % 2 ? sorted[middle] : (sorted[middle - 1] + sorted[middle]) / 2;
  }

  function atFractionalSample(samples, index, fraction) {
    const a = samples[index - 1] ?? 0;
    const b = samples[index] ?? 0;
    const c = samples[index + 1] ?? 0;
    const d = samples[index + 2] ?? 0;
    return b + 0.5 * fraction * (c - a + fraction *
      (2 * a - 5 * b + 4 * c - d + fraction * (3 * (b - c) + d - a)));
  }

  function alignedMetrics(recording, reference, position) {
    const index = Math.floor(position);
    const offset = position - index;
    let sumX = 0, sumY = 0, squareX = 0, squareY = 0, dot = 0;
    for (let j = 0; j < reference.length; j++) {
      const x = atFractionalSample(recording, index + j, offset);
      const y = reference[j];
      sumX += x;
      sumY += y;
      squareX += x * x;
      squareY += y * y;
      dot += x * y;
    }
    const n = reference.length;
    const cov = dot - sumX * sumY / n;
    const xEnergy = squareX - sumX ** 2 / n;
    const yEnergy = squareY - sumY ** 2 / n;
    return {
      correlation: Math.min(1, Math.abs(cov) / Math.sqrt(xEnergy * yEnergy)),
      gainDb: 20 * Math.log10(Math.abs(cov) / yEnergy),
      matchedRms: Math.abs(cov) / Math.sqrt(yEnergy * n),
      polarity: Math.sign(cov),
    };
  }

  function analyze(recording, reference, rate, anchorSample, noiseRms = 0) {
    const rejected = (reason, measurements = {}) =>
      ({ok: false, reason, confidence: 0, reflections: [], ...measurements});
    if (![44100, 48000].includes(rate) || !Number.isFinite(anchorSample) ||
        !Number.isFinite(noiseRms) || noiseRms < 0 || reference.length < rate * 0.02 ||
        reference.length > rate * 0.25 || recording.length > rate * 1.6) {
      return rejected('Capture ou référence invalide');
    }
    // Include the adjacent integer samples so fractional boundary peaks can
    // be refined before the physical delay window is enforced.
    const first = Math.max(0, Math.floor(anchorSample));
    const last = Math.ceil(anchorSample + rate * 0.8);
    if (last + reference.length > recording.length || first > last) {
      return rejected('Fenêtre de retour incomplète');
    }

    const {scores, matchedRms} = correlation(recording, reference);
    const probes = [];
    const background = [];
    for (let index = first; index <= last; index++) {
      const score = Math.abs(scores[index]);
      if ((index - first) % 37 === 0) background.push(score);
      if (score >= 0.12 && score >= Math.abs(scores[index - 1] || 0) &&
          score > Math.abs(scores[index + 1] || 0)) {
        probes.push({index, score, strength: matchedRms[index]});
      }
    }
    const typical = median(background);
    const spread = median(background.map(value => Math.abs(value - typical)));
    // The score floor adapts to the observed correlation background. The
    // amplitude floor below also rejects high normalized scores at tiny levels.
    const scoreFloor = Math.max(0.17, typical + 9 * spread);
    const strongest = Math.max(0, ...probes.filter(p => p.score >= scoreFloor).map(p => p.strength));
    const floor = Math.max(0.00035, noiseRms * 0.48, strongest * 0.22);
    const candidates = probes.filter(p => p.score >= scoreFloor && p.strength >= floor);
    if (!candidates.length) {
      return rejected('Signal absent ou noyé dans le bruit', {
        ...levels(recording), correlation: Math.max(0, ...probes.map(p => p.score))
      });
    }

    // Collapse the local oscillations of the matched-filter lobe. Keep
    // multiple separated peaks to distinguish arrival from stronger echoes.
    const guard = Math.round(rate * 0.0025);
    const peaks = [];
    for (const candidate of candidates.sort((a, b) => b.strength - a.strength || a.index - b.index)) {
      if (!peaks.some(peak => Math.abs(peak.index - candidate.index) <= guard)) peaks.push(candidate);
    }
    peaks.sort((a, b) => a.index - b.index);
    const direct = peaks[0];
    function refined(peak) {
      if (peak.index === 0 || peak.index + 1 === scores.length) return peak.index;
      const center = peak.score;
      const left = Math.abs(scores[peak.index - 1]);
      const right = Math.abs(scores[peak.index + 1]);
      const curvature = left - 2 * center + right;
      const offset = curvature < 0 ? Math.max(-0.5, Math.min(0.5, 0.5 * (left - right) / curvature)) : 0;
      return peak.index + offset;
    }
    const position = refined(direct);
    const measured = levels(recording.subarray(direct.index, direct.index + reference.length));
    const delayMs = (position - anchorSample) * 1000 / rate;
    const reflections = peaks.slice(1, 9).map(peak =>
      ({delayMs: (refined(peak) - anchorSample) * 1000 / rate, correlation: peak.score}));
    const {correlation: alignedScore, gainDb, matchedRms: alignedRms, polarity} =
      alignedMetrics(recording, reference, position);
    const snrDb = noiseRms > 0 ? 20 * Math.log10(alignedRms / noiseRms) : null;
    const metrics = {...measured, delayMs: Math.max(0, delayMs), correlation: alignedScore, gainDb, snrDb, reflections, polarity};
    if (measured.clipped) return rejected('Retour saturé', metrics);
    if (peaks.length > 8 || peaks.some(p => p !== direct && p.index - direct.index < rate * 0.005 &&
        p.strength > direct.strength * 0.75)) {
      return rejected('Pics concurrents non séparables avec confiance', metrics);
    }
    if (delayMs < -500 / rate || delayMs > 800 + 500 / rate) {
      return rejected('Pic hors fenêtre de recherche', metrics);
    }
    const confidence = Math.min(1, alignedScore / 0.68, direct.strength / (1.6 * floor));
    return {ok: true, reason: reflections.length ? 'Première arrivée avec retours secondaires' : 'Arrivée isolée',
      confidence, ...metrics};
  }

  function summarize(trials) {
    const annotated = trials.map(trial => ({...trial, outlier: false, deviationMs: null}));
    const methods = ['chirp', 'pn'].map(method => {
      const group = annotated.filter(trial => trial.method === method);
      const accepted = group.filter(trial => trial.ok);
      const values = accepted.map(trial => trial.delayMs);
      const center = median(values);
      const mad = median(values.map(value => Math.abs(value - center)));
      // The 2 ms floor keeps native clock quantization from labeling otherwise
      // repeatable observations as outliers. Sparse groups are only indicative.
      const fence = Math.max(2, 3.5 * 1.4826 * mad);
      for (const trial of accepted) trial.outlier = Math.abs(trial.delayMs - center) > fence;
      const kept = accepted.filter(trial => !trial.outlier);
      const delays = kept.map(trial => trial.delayMs);
      const medianMs = median(delays);
      const madMs = median(delays.map(value => Math.abs(value - medianMs)));
      for (const trial of accepted) trial.deviationMs = trial.delayMs - medianMs;
      const minMs = kept.length ? Math.min(...delays) : null;
      const maxMs = kept.length ? Math.max(...delays) : null;
      const quality = median(kept.map(trial => trial.confidence));
      return {method, total: group.length, accepted: accepted.length, inliers: kept.length,
        medianMs, madMs, minMs, maxMs, quality,
        stable: kept.length >= 5 && madMs <= 4 && maxMs - minMs <= 12 && quality >= 0.5};
    });
    const ranked = methods.filter(method => method.inliers > 0).sort((a, b) =>
      Number(b.stable) - Number(a.stable) || b.inliers - a.inliers ||
      a.madMs - b.madMs || b.quality - a.quality);
    const best = ranked[0] ?? null;
    const other = ranked[1];
    const agreement = other && other.inliers >= 4 && other.maxMs - other.minMs <= 12 &&
      Math.abs(best.medianMs - other.medianMs) <= 8;
    return {status: !best ? 'failed' : best.stable && agreement ? 'reliable' : 'indicative',
      best, methods, trials: annotated};
  }

  const api = {levels, resample, analyze, summarize};
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.KaraCalibration = api;
})(globalThis);
