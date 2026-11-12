/* AudioWorklet for live voice input: microphone audio in, 24 kHz mono PCM16 out, in chunks of about 100 ms.
 * Loaded by src/features/composer/useVoiceInput.ts. Resamples from the context's rate by averaging
 * (when it is higher) or repeating samples (when it is lower). */
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.step = 24000 / sampleRate; // output samples per input sample
    this.phase = 0;
    this.sum = 0;
    this.count = 0;
    this.out = new Int16Array(2400);
    this.n = 0;
  }

  emit(value) {
    const v = Math.max(-1, Math.min(1, value));
    this.out[this.n++] = v < 0 ? v * 0x8000 : v * 0x7fff;
    if (this.n === this.out.length) {
      this.port.postMessage(this.out.buffer, [this.out.buffer]);
      this.out = new Int16Array(2400);
      this.n = 0;
    }
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (!channel) return true;
    for (let i = 0; i < channel.length; i++) {
      this.sum += channel[i];
      this.count++;
      this.phase += this.step;
      if (this.phase >= 1) {
        const value = this.sum / this.count;
        this.sum = 0;
        this.count = 0;
        while (this.phase >= 1) {
          this.phase -= 1;
          this.emit(value);
        }
      }
    }
    return true;
  }
}

registerProcessor("pcm-capture", PcmCapture);
