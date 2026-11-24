#!/usr/bin/env node
'use strict';

const assert = require('node:assert/strict');
const {execFileSync} = require('node:child_process');
const path = require('node:path');
const dsp = require('../bin/_kara_audio_calibration.js');
const root = path.resolve(__dirname, '..');

function reference48(method) {
  // Use the exact PCM generator behind the authenticated playback endpoint.
  const pcm = execFileSync('python3', ['-c', [
    'import sys',
    'sys.path.insert(0, "bin")',
    'from _kara_audio_calibration import generate_probe',
    'sys.stdout.buffer.write(generate_probe(sys.argv[1]))',
  ].join('\n'), method], {cwd: root});
  return Float32Array.from({length: pcm.length / 2}, (_, i) => pcm.readInt16LE(i * 2) / 32768);
}

function fixture(reference, rate, paths, {noise = 0, drift = 0, lowpass = false,
  phase = false, anchor = 0, unrelated = false} = {}) {
  let seed = 0xb123fade;
  const out = Float32Array.from({length: Math.ceil(rate * 1.15)}, (_, i) => {
    seed = (Math.imul(1664525, seed) + 1013904223) >>> 0;
    return (seed / 2 ** 32 - 0.5) * Math.sqrt(12) * noise +
      (unrelated ? 0.22 * Math.sin(2 * Math.PI * 930 * i / rate) : 0);
  });
  let signal = reference;
  if (lowpass) {
    const alpha = 1 - Math.exp(-2 * Math.PI * 1800 / rate);
    let state = 0;
    signal = Float32Array.from(signal, x => (state += alpha * (x - state)));
  }
  if (phase) {
    // A causal first-order all-pass modifies spectral phase without changing
    // the steady-state magnitude response. This is additional to polarity.
    let previousX = 0, previousY = 0;
    signal = Float32Array.from(signal, x => {
      const y = -0.6 * x + previousX + 0.6 * previousY;
      previousX = x;
      previousY = y;
      return y;
    });
  }
  for (const [delayMs, gain] of paths) {
    const offset = anchor + rate * delayMs / 1000;
    for (let i = Math.ceil(offset); i < out.length; i++) {
      const source = (i - offset) / (1 + drift);
      const j = Math.floor(source);
      if (j >= signal.length) break;
      out[i] += gain * (signal[j] + (source - j) * ((signal[j + 1] || 0) - signal[j]));
    }
  }
  return out;
}

const emitted = Object.fromEntries(['chirp', 'pn'].map(method => [method, reference48(method)]));
let worstClean = 0, worstPerturbed = 0;
for (const method of ['chirp', 'pn']) {
  const reference = emitted[method];
  assert(reference.length >= 48000 * 0.02 && reference.length <= 48000 * 0.25);
  assert(dsp.levels(reference).peak <= 0.25);
  for (const rate of [44100, 48000]) {
    const ref = dsp.resample(reference, 48000, rate);
    const measure = (events, config) => dsp.analyze(fixture(ref, rate, events, config),
      ref, rate, config?.anchor ?? 0, config?.noise ?? 0);
    function accurate(delay, result, tolerance, clean = false) {
      const detail = method + '/' + rate + ' à ' + delay + ' ms ' + JSON.stringify(result);
      assert(result.ok, detail);
      const error = Math.abs(delay - result.delayMs);
      assert(error < tolerance, detail);
      if (clean) worstClean = Math.max(worstClean, error);
      else worstPerturbed = Math.max(worstPerturbed, error);
    }
    for (const delay of [0, 0.125, 0.25, 0.5, 1, 1.5, 50, 205.125, 380.25, 679.5, 800]) {
      const anchor = delay === 0 ? 0 : rate * 0.137 + 0.3;
      const result = measure([[delay, 0.8]], {anchor});
      accurate(delay, result, 0.025, true);
      assert(result.correlation > 0.98, method + '/' + rate + ' ' + delay + ' ' + JSON.stringify(result));
    }
    accurate(0, measure([[0, 0.8]], {anchor: rate * 0.137 + 0.3}), 0.025, true);
    const echoed = measure([[205.125, 0.44], [325.125, 1]], {noise: 0.004});
    accurate(205.125, echoed, 0.06);
    assert(echoed.reflections.some(peak => Math.abs(peak.delayMs - 325.125) < 0.06));
    const overlap = measure([[271.375, 0.55], [298.375, 1]], {noise: 0.004});
    accurate(271.375, overlap, 0.08);
    assert(overlap.reflections.some(peak => Math.abs(peak.delayMs - 298.375) < 0.08));
    const early = measure([[205, 0.06], [325, 1]]);
    accurate(325, early, 0.05);
    const modified = measure([[380.375, -0.4]], {noise: 0.005, drift: 450e-6, lowpass: true, phase: true});
    accurate(380.375, modified, 0.45);
    assert.equal(modified.polarity, -1);
    const quiet = measure([[79.125, 0.2]], {noise: 0.002});
    accurate(79.125, quiet, 0.05);
    assert(Math.abs(quiet.gainDb - 20 * Math.log10(0.2)) < 0.4);
    assert(!measure([]).ok, 'silence');
    assert(!measure([[240, 0.25]], {noise: 0.2}).ok, 'noisy signal below detection');
    assert(!measure([], {noise: 0.006, unrelated: true}).ok, 'unrelated tone');
    assert(!measure([[820, 0.9]]).ok, 'return outside search range');
    const wrongProbe = dsp.resample(emitted[method === 'pn' ? 'chirp' : 'pn'], 48000, rate);
    assert(!dsp.analyze(fixture(wrongProbe, rate, [[230, 1]]), ref, rate, 0).ok,
      'a return from the other probe is not evidence for this probe');
    assert(!measure([], {noise: 0.2}).ok, 'strong uncorrelated noise');
    assert(!measure([[210, 0.8], [213.5, 0.8]]).ok, 'unresolved close competition');
    assert(!measure([[248, 30]]).ok, 'saturation');
    assert(!dsp.analyze(new Float32Array(1000), ref, rate, 0).ok, 'truncated capture');
  }
}

function trial(method, delayMs, ok = true) {
  return {method, delayMs, ok, confidence: 0.94};
}
const chirps = [320, 319, 321, 320, 320, 440].map(delay => trial('chirp', delay));
const pn = [318, 321, 319, 322, 321, 320].map(delay => trial('pn', delay));
const campaign = dsp.summarize([...chirps, ...pn]);
assert.equal(campaign.status, 'reliable');
assert.equal(campaign.best.method, 'pn');
assert.equal(campaign.methods[0].medianMs, 320);
assert(campaign.trials[5].outlier);
assert.equal(dsp.summarize(chirps).status, 'indicative');
assert.equal(dsp.summarize([...chirps, ...pn.map(value =>
  trial(value.method, value.delayMs + 120))]).status, 'indicative');
assert.equal(dsp.summarize([trial('pn', 320, false)]).status, 'failed');
console.log('PASS: écarts max corrigés ' + worstClean.toFixed(4) + ' ms (propres), ' +
  worstPerturbed.toFixed(4) + ' ms (perturbés) ; rejets et agrégation conformes');
