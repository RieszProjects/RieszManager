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

/** The current time in unix seconds, updated on each tick. */
export function useNowSeconds(): number {
  return useSyncExternalStore(subscribe, () => Math.floor(now / 1000));
}

/** A value computed from the current time (unix seconds), recomputed on each tick; re-renders only when it changes.
 * `compute` must return a primitive. */
export function useTick<T extends string | number | boolean>(compute: (nowSeconds: number) => T): T {
  return useSyncExternalStore(subscribe, () => compute(Math.floor(now / 1000)));
}
