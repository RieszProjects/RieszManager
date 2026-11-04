/**
 * The doll's squeak: a short recording of a real squeaky toy (CC0, see
 * public/doll/sounds/CREDITS.md), cut into the squeak of pressing and the
 * squeak of letting go. Both are decoded once and played through the Web Audio
 * API with a slightly random pitch, so repeated squeezes do not sound identical.
 *
 * A quick tap lifts the finger long before the first squeak has finished; the
 * second one is then scheduled to follow the first instead of being laid over it,
 * with about the same gap as in the original recording.
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

type Part = "press" | "release";

const VOLUME = 0.7;
/** The release squeak may start this long before the press squeak has finished playing. */
const RELEASE_LEAD = 0.02;
const buffers = new Map<Part, Promise<AudioBuffer | null>>();
/** When the current press squeak ends, on the audio clock. */
let pressEndsAt = 0;

function sample(part: Part): Promise<AudioBuffer | null> {
  let pending = buffers.get(part);
  if (!pending) {
    pending = (async () => {
      const c = audio();
      if (!c) return null;
      try {
        const res = await fetch(`/doll/sounds/${part}.wav`);
        if (!res.ok) return null;
        return await c.decodeAudioData(await res.arrayBuffer());
      } catch {
        return null;
      }
    })();
    pending.then((buffer) => buffer || buffers.delete(part)); // retry later after a failed fetch
    buffers.set(part, pending);
  }
  return pending;
}

async function play(part: Part): Promise<void> {
  const c = audio();
  const buffer = await sample(part);
  if (!c || !buffer) return;
  const src = c.createBufferSource();
  src.buffer = buffer;
  const rate = 0.94 + Math.random() * 0.12;
  src.playbackRate.value = rate;
  const gain = c.createGain();
  gain.gain.value = VOLUME;
  src.connect(gain).connect(c.destination);
  const now = c.currentTime;
  const when = part === "release" ? Math.max(now, pressEndsAt - RELEASE_LEAD) : now;
  if (part === "press") pressEndsAt = now + buffer.duration / rate;
  src.start(when);
}

export const squeak = {
  /** Fetch and decode ahead of time, so the first squeeze sounds at once. */
  preload(): void {
    void sample("press");
    void sample("release");
  },
  press: () => void play("press"),
  release: () => void play("release"),
};
