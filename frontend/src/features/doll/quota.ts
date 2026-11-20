/**
 * Every quota the doll can show, alike: subscription windows (Claude's from usage.ts, the others'
 * from the runner's limits.py) and API balances (balances.ts). The current session's route comes first.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "@/api/client";
import { harnessMeta } from "@/lib/harnesses";
import type { Balance, Balances } from "./balances";
import type { Usage, UsageWindow } from "./usage";

export const CLAUDE_ROUTE = "anthropic:subscription";

export interface SubscriptionLimits {
  id: string;
  label: string;
  plan: string | null;
  windows: UsageWindow[];
  limited: boolean;
  error: string | null;
}

export interface Quota {
  /** The route it belongs to (an ApiRoute id), or the balance's own id for services no harness runs on. */
  id: string;
  route: string | null;
  label: string;
  /** Subscriptions: their limit windows (null while loading). */
  windows?: UsageWindow[] | null;
  /** API keys: their balances. */
  balances?: Balance[];
  limited?: boolean;
  error?: string | null;
}

/** Which route each balance check stands for (balances.py types). */
const BALANCE_ROUTES: Record<string, string> = { deepseek: "deepseek:key" };

/** The other subscriptions' limits (an older runner has none). */
export function useLimits() {
  const [limits, setLimits] = useState<SubscriptionLimits[] | null>(null);
  const inflight = useRef(false);
  const load = useCallback(async (refresh = false) => {
    if (inflight.current) return;
    inflight.current = true;
    try {
      const res = await api<{ limits: SubscriptionLimits[] }>("limits", { query: refresh ? { refresh: 1 } : {} });
      setLimits(res.limits ?? []);
    } catch {
      /* keep the last value */
    } finally {
      inflight.current = false;
    }
  }, []);
  useEffect(() => {
    void load();
  }, [load]);
  return { limits, reload: load };
}

export function quotas(usage: Usage | null, limits: SubscriptionLimits[] | null, balances: Balances | null): Quota[] {
  const out: Quota[] = [];
  if (!usage) out.push({ id: CLAUDE_ROUTE, route: CLAUDE_ROUTE, label: "Claude 订阅", windows: null });
  else if (usage.plan !== "api")
    out.push({ id: CLAUDE_ROUTE, route: CLAUDE_ROUTE, label: "Claude 订阅", windows: usage.windows, limited: Boolean(usage.limited),
               error: usage.windows.length ? null : usage.error ?? "没有拿到用量数据" });
  for (const l of limits ?? []) out.push({ id: l.id, route: l.id, label: l.label, windows: l.windows, limited: l.limited, error: l.error });
  for (const p of balances?.providers ?? [])
    out.push({ id: `balance:${p.id}`, route: BALANCE_ROUTES[p.id] ?? null, label: p.label, balances: p.balances, error: p.error });
  return out;
}

/** The route a session runs on: its own, else its harness's own login (the harness's first route). */
export function sessionRoute(api: string | null | undefined, harness: string | null | undefined): string | null {
  if (api) return api;
  return harnessMeta(harness)?.routes?.[0]?.id ?? (harness && harness !== "claude" ? null : CLAUDE_ROUTE);
}

/** The current route's quotas first, the rest as they come. */
export function ordered(list: Quota[], current: string | null): Quota[] {
  if (!current) return list;
  return [...list.filter((q) => q.route === current), ...list.filter((q) => q.route !== current)];
}

export function peakOf(windows: UsageWindow[] | null | undefined): UsageWindow | null {
  if (!windows?.length) return null;
  return windows.reduce((a, b) => (b.percent > a.percent ? b : a));
}
