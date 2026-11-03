/**
 * Squeaky-toy sounds, synthesised with the Web Audio API (no audio files).
 *
 * A squeak is a short tone whose pitch glides (up when pressed, down when
 * released), wobbled by a fast vibrato and shaped by a band-pass filter, which
 * gives the rubbery "squeeze" character. A little filtered noise adds the puff
 * of air.
 */
let ctx: AudioContext | null = null;

function audio(): AudioContext | null {
  try {
    ctx ??= new AudioContext();
    if (ctx.state === "suspended") void ctx.resume();
    return ctx;
  } catch {
    return null;
  }
}

interface Squeak {
  from: number;
  to: number;
  duration: number;
  volume: number;
  wobble: number;
}

const PRESS: Squeak = { from: 620, to: 1180, duration: 0.15, volume: 0.22, wobble: 34 };
const RELEASE: Squeak = { from: 1050, to: 560, duration: 0.2, volume: 0.17, wobble: 26 };

function play(s: Squeak): void {
  const c = audio();
  if (!c) return;
  const t = c.currentTime + 0.005;
  const end = t + s.duration;
  // Small random detune so repeated squeezes do not sound identical.
  const detune = 1 + (Math.random() - 0.5) * 0.12;

  const osc = c.createOscillator();
  osc.type = "triangle";
  osc.frequency.setValueAtTime(s.from * detune, t);
  osc.frequency.exponentialRampToValueAtTime(s.to * detune, t + s.duration * 0.75);

  const vibrato = c.createOscillator();
  vibrato.frequency.value = s.wobble;
  const vibratoDepth = c.createGain();
  vibratoDepth.gain.value = s.from * 0.06;
  vibrato.connect(vibratoDepth).connect(osc.frequency);

  const band = c.createBiquadFilter();
  band.type = "bandpass";
  band.frequency.value = 1500;
  band.Q.value = 0.9;

  const env = c.createGain();
  env.gain.setValueAtTime(0.0001, t);
  env.gain.exponentialRampToValueAtTime(s.volume, t + 0.012);
  env.gain.exponentialRampToValueAtTime(s.volume * 0.6, t + s.duration * 0.6);
  env.gain.exponentialRampToValueAtTime(0.0001, end);
  osc.connect(band).connect(env).connect(c.destination);

  // Puff of air.
  const frames = Math.floor(c.sampleRate * s.duration);
  const buffer = c.createBuffer(1, frames, c.sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < frames; i++) data[i] = (Math.random() * 2 - 1) * (1 - i / frames);
  const noise = c.createBufferSource();
  noise.buffer = buffer;
  const hiss = c.createBiquadFilter();
  hiss.type = "highpass";
  hiss.frequency.value = 2400;
  const hissGain = c.createGain();
  hissGain.gain.value = s.volume * 0.12;
  noise.connect(hiss).connect(hissGain).connect(c.destination);

  osc.start(t);
  vibrato.start(t);
  noise.start(t);
  osc.stop(end + 0.02);
  vibrato.stop(end + 0.02);
  noise.stop(end);
}

export const squeak = {
  press: () => play(PRESS),
  release: () => play(RELEASE),
};
