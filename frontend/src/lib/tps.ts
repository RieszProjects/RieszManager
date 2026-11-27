/**
 * Per-session output throughput (tokens per second) tracker.
 *
 * - Streaming harnesses (Claude Code via `stream` deltas; Antigravity, Gemini, Codex via
 *   growing `items`): estimates tokens per chunk in a sliding window over the current
 *   contiguous generation burst (gaps > BURST_GAP_MS start a fresh burst so tool execution
 *   and TTFT pauses do not drag down TPS).
 * - Harnesses that report exact `usage.output_tokens` (Claude Code, dsh): refines or
 *   computes the finalized step TPS from the exact output token count. For Claude Code the
 *   span starts at the message's first content block: thinking may stream no text at all
 *   (its tokens are in output_tokens, so its time must be in the span too).
 * - A message or step already under way when the page joined (snapshot) cannot be timed:
 *   it keeps the estimate and is not given an exact figure.
 */

export interface TpsSample {
  t: number;
  tokens: number;
}

export interface TpsState {
  samples: TpsSample[];
  burstStart: number | null;
  lastChunkAt: number | null;
  burstTokens: number;
  stepStartedAt: number | null;
  /** When the current message's first content block (thinking included) began streaming. */
  firstBlockAt: number | null;
  /** The current message or step was already under way when the page joined: no exact figure for it. */
  joinedMidway: boolean;
  tps: number | null;
  live: boolean;
  exact: boolean;
}

const WINDOW_MS = 3500;
export const BURST_GAP_MS = 3000;
const MIN_WINDOW_MS = 250;
const MIN_EXACT_BURST_MS = 300;
const MIN_STEP_MS = 200;
const MAX_STEP_MS = 600_000;

export function emptyTps(): TpsState {
  return {
    samples: [],
    burstStart: null,
    lastChunkAt: null,
    burstTokens: 0,
    stepStartedAt: null,
    firstBlockAt: null,
    joinedMidway: false,
    tps: null,
    live: false,
    exact: false,
  };
}

/** Approximate token count for mixed ASCII/code and CJK text. */
export function estimateTokens(text: string): number {
  if (!text) return 0;
  let tokens = 0;
  for (let i = 0; i < text.length; i++) {
    const code = text.charCodeAt(i);
    if (code < 0x80) {
      tokens += 0.25;
    } else if (
      (code >= 0x3000 && code <= 0x30ff) ||
      (code >= 0x3400 && code <= 0x9fff) ||
      (code >= 0xac00 && code <= 0xd7af) ||
      (code >= 0xf900 && code <= 0xfaff) ||
      (code >= 0xff00 && code <= 0xffef)
    ) {
      tokens += 0.65;
    } else {
      tokens += 0.5;
    }
  }
  return tokens;
}

export function onTurnStart(prev: TpsState, now: number): TpsState {
  return {
    ...prev,
    samples: [],
    burstStart: null,
    lastChunkAt: null,
    burstTokens: 0,
    stepStartedAt: now,
    firstBlockAt: null,
    joinedMidway: false,
    live: false,
  };
}

/** A content block of the streaming message began (thinking blocks too, even when they carry no text). */
export function onBlockStart(prev: TpsState, now: number): TpsState {
  return prev.firstBlockAt === null ? { ...prev, firstBlockAt: now } : prev;
}

/** The page joined while a message or step was in progress (a snapshot of a busy session). */
export function onJoinMidway(prev: TpsState, now: number): TpsState {
  return { ...prev, stepStartedAt: prev.stepStartedAt ?? now, joinedMidway: true };
}

/** Whether the throughput shown is of output arriving right now (not of a burst that paused or ended). */
export function isLive(state: TpsState, now: number): boolean {
  return state.live && state.lastChunkAt !== null && now - state.lastChunkAt <= BURST_GAP_MS;
}

export function onStepReset(prev: TpsState, now: number): TpsState {
  return {
    ...prev,
    stepStartedAt: now,
  };
}

export function onTurnEnd(prev: TpsState): TpsState {
  if (!prev.live && prev.burstStart === null && prev.stepStartedAt === null && prev.samples.length === 0) {
    return prev;
  }
  return {
    ...prev,
    samples: [],
    burstStart: null,
    lastChunkAt: null,
    burstTokens: 0,
    stepStartedAt: null,
    firstBlockAt: null,
    joinedMidway: false,
    live: false,
  };
}

export function recordChunk(prev: TpsState, text: string, now: number): TpsState {
  const tokens = estimateTokens(text);
  if (tokens <= 0) return prev;

  const isNewBurst = prev.lastChunkAt === null || now - prev.lastChunkAt > BURST_GAP_MS;
  if (isNewBurst) {
    return {
      ...prev,
      samples: [{ t: now, tokens }],
      burstStart: now,
      lastChunkAt: now,
      burstTokens: tokens,
      live: true,
    };
  }

  const cutoff = now - WINDOW_MS;
  const nextSamples = prev.samples.filter((s) => s.t >= cutoff);
  if (nextSamples.length === 0 && prev.samples.length > 0) {
    nextSamples.push(prev.samples[prev.samples.length - 1]);
  }
  nextSamples.push({ t: now, tokens });

  const oldest = nextSamples[0];
  const spanMs = now - oldest.t;
  let tps = prev.tps;
  let exact = prev.exact;
  if (spanMs >= MIN_WINDOW_MS && nextSamples.length >= 2) {
    let windowTokens = 0;
    for (let i = 1; i < nextSamples.length; i++) windowTokens += nextSamples[i].tokens;
    const raw = windowTokens / (spanMs / 1000);
    if (Number.isFinite(raw) && raw > 0) {
      tps = prev.live && prev.tps !== null && !prev.exact ? prev.tps * 0.35 + raw * 0.65 : raw;
      exact = false;
    }
  }

  return {
    ...prev,
    samples: nextSamples,
    lastChunkAt: now,
    burstTokens: prev.burstTokens + tokens,
    tps,
    live: true,
    exact,
  };
}

export function recordUsage(prev: TpsState, outputTokens: number, now: number): TpsState {
  if (!Number.isFinite(outputTokens) || outputTokens <= 0) return prev;
  if (prev.joinedMidway) {
    // Part of this output came before the page was watching: no span to divide it by. The next one is timed.
    return { ...prev, joinedMidway: false, stepStartedAt: now };
  }

  // Streaming harness: refine with exact output_tokens over the span the message streamed in.
  if (prev.burstStart !== null && prev.lastChunkAt !== null) {
    const start = prev.firstBlockAt !== null ? Math.min(prev.firstBlockAt, prev.burstStart) : prev.burstStart;
    const burstMs = Math.max(prev.lastChunkAt, now) - start;
    if (burstMs >= MIN_EXACT_BURST_MS) {
      const rate = outputTokens / (burstMs / 1000);
      if (Number.isFinite(rate) && rate > 0) {
        return {
          ...prev,
          tps: rate,
          exact: true,
          stepStartedAt: now,
        };
      }
    }
    return prev;
  }

  // Non-streaming harness (e.g. dsh): compute throughput across the step duration.
  if (prev.stepStartedAt !== null) {
    const stepMs = now - prev.stepStartedAt;
    if (stepMs >= MIN_STEP_MS && stepMs <= MAX_STEP_MS) {
      const rate = outputTokens / (stepMs / 1000);
      if (Number.isFinite(rate) && rate > 0) {
        return {
          ...prev,
          tps: rate,
          live: false,
          exact: true,
          stepStartedAt: now,
        };
      }
    }
  }

  return prev;
}

export function formatTps(tps: number | null): string | null {
  if (tps === null || !Number.isFinite(tps) || tps < 0.1) return null;
  return tps >= 100 ? `${Math.round(tps)} tok/s` : `${tps.toFixed(1)} tok/s`;
}
