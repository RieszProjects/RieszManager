/** localStorage wrapper that never throws (private mode, quota, disabled storage). */
const PREFIX = "riesz:";

export const storage = {
  get<T>(key: string, fallback: T): T {
    try {
      const raw = window.localStorage.getItem(PREFIX + key);
      return raw === null ? fallback : (JSON.parse(raw) as T);
    } catch {
      return fallback;
    }
  },
  set(key: string, value: unknown): void {
    try {
      window.localStorage.setItem(PREFIX + key, JSON.stringify(value));
    } catch {
      /* ignore */
    }
  },
  remove(key: string): void {
    try {
      window.localStorage.removeItem(PREFIX + key);
    } catch {
      /* ignore */
    }
  },
};
