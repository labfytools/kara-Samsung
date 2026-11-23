// Real-time microphone capture. Each packet is 20ms at the native AudioContext rate.
class KaraMicroWorklet extends AudioWorkletProcessor {
  constructor() {
    super();
    this.gain = 0.9;
    this.packet = new Int16Array(Math.round(sampleRate / 50));
    this.used = 0;
    this.port.onmessage = event => {
      if (event.data && Number.isFinite(event.data.gain))
        this.gain = Math.max(0, Math.min(1, event.data.gain));
    };
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    const samples = inputs[0] && inputs[0][0];
    if (!samples) return true;
    for (let i = 0; i < samples.length; i++) {
      const x = Math.max(-1, Math.min(1, samples[i] * this.gain));
      this.packet[this.used++] = Math.round(x * 32767);
      if (this.used === this.packet.length) {
        const payload = this.packet.buffer;
        // Audio clock timestamp: end of packet, not the main-thread callback.
        this.port.postMessage({pcm: payload, endSec: currentTime + samples.length / sampleRate}, [payload]);
        this.packet = new Int16Array(Math.round(sampleRate / 50));
        this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor('kara-micro', KaraMicroWorklet);
