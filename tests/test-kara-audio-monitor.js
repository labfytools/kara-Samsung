// Exercise the actual Worklet and mobile page without a microphone or socket.
'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const workletScript = fs.readFileSync('bin/_kara_micro_worklet.js', 'utf8');
const page = fs.readFileSync('bin/_kara_micro_test.html', 'utf8');
const script = page.match(/<script>\s*([\s\S]*?)<\/script>/)[1];

for (const rate of [44100, 48000]) {
  for (const blockMs of [10, 20]) {
    let Processor;
    const audio = {sampleRate: rate, currentFrame: 0,
      AudioWorkletProcessor: class {
        constructor() {
          this.messages = [];
          this.port = {postMessage: (data, transfer) => {
            assert.equal(transfer[0], data.pcm);
            this.messages.push(data);
          }};
        }
      },
      registerProcessor: (name, constructor) => { Processor = constructor; }};
    vm.createContext(audio);
    vm.runInContext(workletScript, audio);
    const node = new Processor({processorOptions: {monitorBlockMs: blockMs}});
    const frames = Math.round(rate * blockMs / 1000);
    for (let quantum = 0; quantum < 16; quantum++) {
      audio.currentFrame = quantum * 128;
      node.process([[new Float32Array(128).fill(.25)]], []);
    }
    assert.equal(node.messages.length, Math.floor(2048 / frames));
    node.messages.forEach((entry, i) => {
      assert.equal(entry.sequence, i);
      assert.equal(entry.endFrame, frames * (i + 1));
      assert.equal(entry.endSec, entry.endFrame / rate);
      assert.equal(entry.pcm.byteLength, frames * 2);
      assert.equal(new Int16Array(entry.pcm)[0], 7373);
      const firstQuantum = Math.floor(i * frames / 128) * 128;
      const lastQuantum = Math.floor((entry.endFrame - 1) / 128) * 128;
      assert.equal(entry.readySpanFrames, lastQuantum - firstQuantum);
      assert.equal(entry.quantumFrames, 128);
    });
  }
}

const profiles = {stable: [20, 80], balanced: [10, 40], 'low-latency': [10, 20]};
const element = () => ({value: '90', textContent: '', disabled: false, children: [],
  replaceChildren() { this.children = []; }, appendChild(child) { this.children.push(child); }});
function harness(preferenceFor = () => ({route: 'a'.repeat(64), saved: false,
  settings: {profile: 'stable', volume_percent: 50, mic_gain_percent: 55, echo_guard: true,
    chrome_echo: true, chrome_noise: true, pulse_buffer: 'standard', experimental: true}})) {
  const sockets = [], tracks = [], processors = [], contexts = [];
  const elements = new Map();
  const get = id => {
    if (!elements.has(id)) elements.set(id, element());
    return elements.get(id);
  };
  get('profile').value = 'stable';
  get('output').value = 'analog';
  get('echo-guard').checked = true;
  get('chrome-echo').checked = true;
  get('chrome-noise').checked = true;
  get('speaker-volume').value = '50';
  const disconnectable = () => ({connect() {}, disconnect() {}});
  const openTimers = new Set();
  const environment = {
    ArrayBuffer, DataView, Uint8Array, URLSearchParams, AbortSignal, setTimeout, clearTimeout,
    async fetch(url) {
      const request = new URL(url, 'https://kara.test');
      assert.equal(request.pathname, '/monitor-preferences');
      assert.equal(request.searchParams.get('token'), 'PRIVATE_TEST_TOKEN');
      return {ok: true, json: async () => preferenceFor(request.searchParams.get('output'))};
    },
    setInterval(fn) { openTimers.add(fn); return fn; },
    clearInterval(fn) { openTimers.delete(fn); },
    performance: {now: () => 150},
    location: {search: '?token=PRIVATE_TEST_TOKEN', host: 'localhost'},
    document: {visibilityState: 'visible', addEventListener() {},
      getElementById: get, createElement: element},
    window: {isSecureContext: true, addEventListener() {}},
    navigator: {mediaDevices: {getUserMedia(constraints) {
      assert.equal(constraints.audio.echoCancellation, get('chrome-echo').checked);
      assert.equal(constraints.audio.noiseSuppression, get('chrome-noise').checked);
      assert.equal(constraints.audio.autoGainControl, false);
      const track = {readyState: 'live', stopped: 0, stop() { this.stopped++; },
        getSettings: () => ({deviceId: 'PRIVATE_DEVICE', sampleRate: 48000,
          echoCancellation: constraints.audio.echoCancellation ? 'remote-only' : false,
          noiseSuppression: constraints.audio.noiseSuppression})};
      tracks.push(track);
      return Promise.resolve({getAudioTracks: () => [track], getTracks: () => [track]});
    }}},
    AudioContext: class {
      constructor() {
        Object.assign(this, {sampleRate: 48000, currentTime: 1, baseLatency: .01,
          audioWorklet: {addModule: async () => {}}, state: 'running', closed: 0});
        contexts.push(this);
      }
      async resume() {}
      async close() { this.closed++; }
      createMediaStreamSource() { return disconnectable(); }
      createGain() { return {...disconnectable(), gain: {value: 1}}; }
    },
    AudioWorkletNode: class {
      constructor(context, name, options) {
        Object.assign(this, disconnectable(), {options, port: {postMessage() {},
          closed: false, close() { this.closed = true; }}});
        processors.push(this);
      }
    },
    WebSocket: class {
      static OPEN = 1;
      constructor(address) {
        this.address = new URL(address);
        this.sent = [];
        this.bufferedAmount = 0;
        sockets.push(this);
      }
      send(message) { this.sent.push(message); }
      close() { this.readyState = 3; }
      accept() {
        this.readyState = 1;
        const profile = this.address.searchParams.get('profile');
        const [block_ms, queue_max_ms] = profiles[profile];
        const echo_guard = this.address.searchParams.get('echo_guard') === '1';
        this.onmessage({data: JSON.stringify({ready: true, profile, block_ms, queue_max_ms,
          requested_latency_ms: profile === 'stable' ? 10 : 20, monitor_version: 2,
          echo_guard, echo_guard_attenuation_db: echo_guard ? 9 : 0})});
      }
    }
  };
  vm.createContext(environment);
  vm.runInContext(script, environment);
  return {get, environment, sockets, tracks, processors, contexts, openTimers,
    run: source => vm.runInContext(source, environment)};
}

async function main() {
  const h = harness();
  await new Promise(resolve => setImmediate(resolve));
  const trend = h.run('describeSamples(Array.from({length:600},(_,i)=>[i*10,i*.02]))');
  assert.equal(trend.trend_ms_per_s, 2);
  assert.equal(trend.n, 600);
  assert.equal(h.run('describeSamples([[10, 4]])').p95, null);
  assert.equal(h.run('measuredRate([[1000000,1],[1005000,240001]])'), 48000);
  let stalePacket = null;
  for (const profile of ['balanced', 'low-latency', 'stable']) {
    h.get('profile').value = profile;
    h.get('profile').onchange();
    h.get('echo-guard').checked = profile !== 'stable';
    h.get('echo-guard').onchange();
    h.get('chrome-echo').checked = profile !== 'low-latency';
    h.get('chrome-noise').checked = profile !== 'balanced';
    h.get('short-buffer').checked = profile !== 'balanced';
    const activation = h.run('startMic()');
    // A second click during asynchronous startup must not acquire another track.
    await h.run('startMic()');
    await new Promise(resolve => setImmediate(resolve));
    const socket = h.sockets.at(-1);
    assert.equal(socket.address.searchParams.get('route'), 'a'.repeat(64));
    assert.equal(socket.address.searchParams.get('profile'), profile);
    assert.equal(socket.address.searchParams.get('echo_guard'), profile === 'stable' ? '0' : '1');
    assert.equal(socket.address.searchParams.get('pulse_buffer'), profile === 'low-latency' ? 'short' : null);
    socket.accept();
    await activation;
    const processor = h.processors.at(-1);
    const [blockMs, queueMs] = profiles[profile];
    assert.equal(h.get('echo-guard').disabled, true);
    assert.equal(h.get('chrome-echo').disabled, true);
    assert.equal(h.get('chrome-noise').disabled, true);
    assert.equal(processor.options.processorOptions.monitorBlockMs, blockMs);
    const payload = new ArrayBuffer(blockMs * 96);
    const onPacket = processor.port.onmessage;
    const packet = (sequence, age) => onPacket({data: {pcm: payload, endSec: 1 - age / 1000,
      endFrame: (sequence + 1) * blockMs * 48 + (sequence >= 2 ? 128 : 0),
      readySpanFrames: 384, quantumFrames: 128, sequence}});
    if (stalePacket) stalePacket(99, 0);
    packet(0, queueMs + 10);
    socket.bufferedAmount = 4000;
    packet(1, 0);
    socket.bufferedAmount = 0;
    packet(2, 0);
    packet(2, 0);
    packet(1, 0);
    stalePacket = packet;
    const binary = socket.sent.filter(value => value instanceof ArrayBuffer);
    assert.equal(binary.length, profile === 'stable' ? 2 : 1);
    assert.equal(new DataView(binary.at(-1)).getUint32(4, true), 2);
    const dropped = profile === 'stable' ? 1 : 2;
    assert.equal(h.run('clientDropped'), dropped);
    socket.onmessage({data: JSON.stringify({levels: true, rms: .1, peak: .3,
      uptime_sec: 2, sequence_missing: dropped, queue_drops: 0, source_starvations: 0})});
    assert.match(h.get('monitor-json').textContent, /"base_latency_ms": 10/);
    assert.match(h.get('monitor-json').textContent, /"worklet_duplicates": 2/);
    assert.doesNotMatch(h.get('monitor-json').textContent, /PRIVATE_/);
    const diagnostic = JSON.parse(h.get('monitor-json').textContent);
    assert.equal(diagnostic.capture.echoCancellation, profile === 'low-latency' ? false : 'remote-only');
    assert.equal(diagnostic.capture.noiseSuppression, profile !== 'balanced');
    assert.equal(diagnostic.client.timings_ms.assembly_audio_ms.max, 8);
    assert.equal(diagnostic.client.timings_ms.assembly_audio_ms.p95, null);
    assert.equal(diagnostic.client.worklet_missing_frames, 128);
    assert.equal(diagnostic.client.drop_reasons.old, profile === 'stable' ? 0 : 1);
    assert.equal(diagnostic.unmeasured_ms.voice_to_acoustic, null);
    if (profile !== 'stable') h.get('restore-stable').onclick();
    else await h.run('stop()');
    assert.equal(h.get('profile').value, 'stable');
    assert.equal(h.get('profile').disabled, false);
    assert.equal(h.get('echo-guard').disabled, false);
    assert.equal(h.get('monitor-profile').textContent, '');
    assert.equal(socket.sent.at(-1), 'stop');
    assert.equal(socket.readyState, 3);
    assert.equal(h.tracks.at(-1).stopped, 1);
    assert.equal(h.contexts.at(-1).closed, 1);
    assert.equal(h.openTimers.size, 0);
    assert.equal(processor.port.onmessage, null);
    assert.equal(processor.port.closed, true);
  }
  assert.equal(h.get('monitor-comparison').children.length, 4);
  assert.equal(h.tracks.length, 3);
  assert.equal(h.processors.length, 3);
  assert.equal(h.sockets.length, 3);

  // Restore while connecting, before any PCM exists.
  h.get('profile').value = 'low-latency';
  const connecting = h.run('startMic()');
  await new Promise(resolve => setImmediate(resolve));
  h.get('restore-stable').onclick();
  await connecting;
  assert.equal(h.sockets.at(-1).readyState, 3);
  assert.equal(h.contexts.at(-1).closed, 1);
  assert.equal(h.tracks.at(-1).stopped, 1);

  // Permission may resolve after the old owner has been cancelled.
  let resolveMedia;
  h.environment.navigator.mediaDevices.getUserMedia = () => new Promise(resolve => { resolveMedia = resolve; });
  const pending = h.run('startMic()');
  await new Promise(resolve => setImmediate(resolve));
  await h.run('stop()');
  let lateStops = 0;
  resolveMedia({getTracks: () => [{stop() { lateStops++; }}]});
  await pending;
  assert.equal(lateStops, 1);
  assert.equal(h.run('normalBusy'), false);

  // Reject a nonconforming acquisition before opening any playback session.
  const tracks = Array.from({length: 2}, () => ({stopped: false, stop() { this.stopped = true; }}));
  h.environment.navigator.mediaDevices.getUserMedia = async () => ({
    getTracks: () => tracks, getAudioTracks: () => tracks});
  const socketCount = h.sockets.length;
  await h.run('startMic()');
  assert.equal(h.sockets.length, socketCount);
  assert.ok(tracks.every(track => track.stopped));

  // The Vivobook and the active Bluetooth output have different presets.
  // Manual changes are kept during activation on the same output only.
  const presets = harness(output => {
    const analog = output === 'analog';
    return {route: (analog ? 'a' : 'b').repeat(64), saved: analog,
      settings: {profile: analog ? 'low-latency' : 'balanced', volume_percent: analog ? 60 : 50,
        mic_gain_percent: 55, echo_guard: true, chrome_echo: true, chrome_noise: true,
        pulse_buffer: 'standard', experimental: !analog}};
  });
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(presets.get('profile').value, 'low-latency');
  assert.equal(presets.get('speaker-volume').value, '60');
  assert.equal(presets.get('monitor-preset').hidden, false);
  presets.get('output').value = 'default';
  await presets.get('output').onchange();
  assert.equal(presets.get('profile').value, 'balanced');
  assert.equal(presets.get('speaker-volume').value, '50');
  assert.equal(presets.get('monitor-preset').hidden, true);
  presets.get('speaker-volume').value = '65';
  const trial = presets.run('startMic()');
  await new Promise(resolve => setImmediate(resolve));
  const bt = presets.sockets.at(-1);
  assert.equal(bt.address.searchParams.get('volume'), '65');
  assert.equal(bt.address.searchParams.get('route'), 'b'.repeat(64));
  bt.accept();
  await trial;
  await presets.run('stop()');
  presets.get('output').value = 'analog';
  await presets.get('output').onchange();
  assert.equal(presets.get('speaker-volume').value, '60');
  assert.equal(presets.tracks[0].stopped, 1);
  assert.equal(presets.processors[0].port.closed, true);
  console.log('PASS: PCM 10/20 ms, piste et Worklet uniques, séquences, reconnexion, protection et retour Stable');
}

main().catch(error => { console.error(error); process.exitCode = 1; });
