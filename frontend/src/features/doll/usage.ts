/** Plan usage (subscription limit windows) as reported by the host's Claude Code. */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { socket } from "@/api/socket";

export interface UsageWindow {
  label: string;
  kind: "session" | "week" | "day" | "other";
  percent: number;
  resetsAt: number | null;
  resetText: string | null;
}

export interface Usage {
  windows: UsageWindow[];
  plan: "subscription" | "api" | null;
  fetchedAt: number;
  limited?: { resetsAt: number | null; type: string | null };
  error?: string;
}

const POLL_MS = 3 * 60 * 1000;
const EVENT_DEBOUNCE_MS = 20 * 1000;

/** Short Chinese name of a window, e.g. "Current week (all models)" → "本周". */
export function windowName(w: UsageWindow): string {
  const scope = /\(([^)]+)\)/.exec(w.label)?.[1];
  const base = w.kind === "session" ? "5 小时" : w.kind === "week" ? "本周" : w.kind === "day" ? "今日" : w.label;
  if (!scope || /all models/i.test(scope)) return base;
  return `${base} · ${scope}`;
}

/** "今天 17:00" / "明天 03:00" / "11月10日 03:00" */
export function resetLabel(ts: number | null, fallback: string | null): string | null {
  if (!ts) return fallback;
  const d = new Date(ts * 1000);
  const now = new Date();
  const hm = `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  const dayDiff = Math.round((new Date(d).setHours(0, 0, 0, 0) - new Date(now).setHours(0, 0, 0, 0)) / 86400000);
  if (dayDiff === 0) return `今天 ${hm}`;
  if (dayDiff === 1) return `明天 ${hm}`;
  return `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

/** The fullest window: what the doll worries about. */
export function peakWindow(usage: Usage | null): UsageWindow | null {
  if (!usage?.windows.length) return null;
  return usage.windows.reduce((a, b) => (b.percent > a.percent ? b : a));
}

export function useUsage() {
  const [usage, setUsage] = useState<Usage | null>(null);
  const [loading, setLoading] = useState(false);
  const inflight = useRef(false);

  const load = useCallback(async (refresh = false) => {
    if (inflight.current) return;
    inflight.current = true;
    setLoading(true);
    try {
      setUsage(await api<Usage>("usage", { query: refresh ? { refresh: 1 } : {} }));
    } catch {
      /* keep the last value; the doll simply shows nothing new */
    } finally {
      inflight.current = false;
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const poll = window.setInterval(() => document.visibilityState === "visible" && void load(), POLL_MS);
    let debounce: number | undefined;
    const off = socket.onEvent((event) => {
      if (event.ev !== "usage_changed" || debounce) return;
      debounce = window.setTimeout(() => {
        debounce = undefined;
        void load();
      }, EVENT_DEBOUNCE_MS);
    });
    const onVisible = () => document.visibilityState === "visible" && void load();
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(poll);
      window.clearTimeout(debounce);
      off();
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [load]);

  return { usage, loading, reload: load };
}
