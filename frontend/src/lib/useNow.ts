/** A coarse shared clock for things that change with time (e.g. "recently"): one timer, ticks every 30 seconds. */
import { useSyncExternalStore } from "react";

const TICK_MS = 30_000;
const listeners = new Set<() => void>();
let now = Date.now();
let timer: number | null = null;

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  if (timer === null) {
    now = Date.now();
    timer = window.setInterval(() => {
      now = Date.now();
      listeners.forEach((l) => l());
    }, TICK_MS);
  }
  return () => {
    listeners.delete(listener);
    if (!listeners.size && timer !== null) {
      window.clearInterval(timer);
      timer = null;
    }
  };
}

/** Whether `seconds` (unix seconds) is less than `window` seconds ago; re-evaluated on each tick. */
export function useWithin(seconds: number | null | undefined, window: number): boolean {
  return useSyncExternalStore(subscribe, () => seconds != null && now / 1000 - seconds < window);
}

/** The current time in unix seconds, updated on each tick. */
export function useNowSeconds(): number {
  return useSyncExternalStore(subscribe, () => Math.floor(now / 1000));
}
