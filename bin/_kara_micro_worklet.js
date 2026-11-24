// Real-time microphone capture. Calibration always uses the 20ms default.
class KaraMicroWorklet extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.gain = 0.9;
    const monitorBlockMs = options?.processorOptions?.monitorBlockMs;
    const blockMs = monitorBlockMs === 10 ? 10 : 20;
    this.monitorTiming = monitorBlockMs === 10 || monitorBlockMs === 20;
    this.packetFrames = Math.round(sampleRate * blockMs / 1000);
    this.packet = new Int16Array(this.packetFrames);
    this.used = 0;
    this.nextFrame = null;
    this.sequence = 0;
    this.port.onmessage = event => {
      if (event.data && Number.isFinite(event.data.gain))
        this.gain = Math.max(0, Math.min(1, event.data.gain));
    };
  }
  process(inputs, outputs) {
    for (const output of outputs) for (const channel of output) channel.fill(0);
    const samples = inputs[0] && inputs[0][0];
    if (!samples) return true;
    // Never combine samples across a missing input quantum.
    if (this.nextFrame !== null && this.nextFrame !== currentFrame) this.used = 0;
    if (this.monitorTiming && this.used === 0) this.firstQuantumFrame = currentFrame;
    this.nextFrame = currentFrame + samples.length;
    for (let i = 0; i < samples.length; i++) {
      const x = Math.max(-1, Math.min(1, samples[i] * this.gain));
      this.packet[this.used++] = Math.round(x * 32767);
      if (this.used === this.packet.length) {
        const payload = this.packet.buffer;
        // Audio clock timestamp: end of packet, not the main-thread callback.
        const endFrame = currentFrame + i + 1;
        const packet = {pcm: payload, endFrame, endSec: endFrame / sampleRate,
          sequence: this.sequence++};
        // The input buffer is available at callback entry. These are logical
        // render positions, not physical ADC timestamps or wall-clock calls.
        if (this.monitorTiming) {
          packet.readySpanFrames = currentFrame - this.firstQuantumFrame;
          packet.quantumFrames = samples.length;
          this.firstQuantumFrame = currentFrame;
        }
        this.port.postMessage(packet, [payload]);
        this.packet = new Int16Array(this.packetFrames);
        this.used = 0;
      }
    }
    return true;
  }
}
registerProcessor('kara-micro', KaraMicroWorklet);
