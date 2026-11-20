/** Harnesses (Claude Code, Codex, DeepSeek Harness), the model APIs each can run on, and their models. */
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ApiRoute, HarnessInfo, ModelInfo } from "@/api/types";

/** What the runner said about each harness (runner/riesz_runner/harnesses.py: HarnessSpec), by id. */
const known = new Map<string, HarnessInfo>();

/** The harness as the runner described it (undefined until the list has arrived). */
export function harnessMeta(id: string | null | undefined): HarnessInfo | undefined {
  return known.get(id ?? "claude");
}

/** A short name for tags and placeholders. */
export function harnessLabel(id: string | null | undefined): string {
  const meta = known.get(id ?? "claude");
  return meta?.label ?? meta?.name ?? (id && id !== "claude" ? id : "Claude Code");
}

/** The terminal command that continues a session, or null when the harness has none by session id. */
export function resumeCommand(harness: string | null | undefined, sessionId: string): string | null {
  const template = known.get(harness ?? "claude")?.resumeCommand ?? (harness ? null : "claude --resume {id}");
  return template ? template.replace("{id}", sessionId) : null;
}

const TTL = 5 * 60 * 1000;
let listed: Promise<HarnessInfo[]> | null = null;
let listedAt = 0;
const listeners = new Set<(list: HarnessInfo[]) => void>();

function fetchHarnesses(force = false): Promise<HarnessInfo[]> {
  if (force || !listed || Date.now() - listedAt > TTL) {
    listedAt = Date.now();
    listed = api<{ harnesses: HarnessInfo[] }>("harnesses").then((r) => {
      for (const h of r.harnesses ?? []) known.set(h.id, h);
      return r.harnesses ?? [];
    }).catch(() => {
      listed = null;
      return [];
    });
    void listed.then((l) => listeners.forEach((fn) => fn(l)));
  }
  return listed;
}

/** Ask again (after API keys changed in the settings). */
export function refreshHarnesses(): void {
  routeModels.clear();
  void fetchHarnesses(true);
}

/** Every harness the runner offers, Claude Code first (empty until the runner answers, or with an older runner). */
export function useHarnesses(): HarnessInfo[] {
  const [list, setList] = useState<HarnessInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    const update = (l: HarnessInfo[]) => !cancelled && setList(l);
    listeners.add(update);
    void fetchHarnesses().then(update);
    return () => {
      cancelled = true;
      listeners.delete(update);
    };
  }, []);
  return list;
}

const routeModels = new Map<string, { at: number; list: ModelInfo[] }>();

/** Each available route of a harness with its models, before a session of it exists. */
export function useRouteModels(harness: string | null, routes: ApiRoute[] | undefined): ApiRoute[] {
  const [, setTick] = useState(0);
  const key = (routes ?? []).filter((r) => r.available).map((r) => r.id).join(",");
  useEffect(() => {
    if (!harness || !key) return;
    let cancelled = false;
    for (const id of key.split(",")) {
      const k = `${harness}|${id}`;
      const hit = routeModels.get(k);
      if (hit && Date.now() - hit.at < TTL) continue;
      api<{ models: ModelInfo[] }>(`harnesses/${harness}/models`, { query: { route: id } })
        .then((r) => {
          routeModels.set(k, { at: Date.now(), list: r.models ?? [] });
          if (!cancelled) setTick((t) => t + 1);
        })
        .catch(() => undefined);
    }
    return () => {
      cancelled = true;
    };
  }, [harness, key]);
  return (routes ?? []).map((r) => ({ ...r, models: r.available ? routeModels.get(`${harness}|${r.id}`)?.list ?? [] : [] }));
}

/** The route a new session takes: the first available one (a subscription before keys). */
export function defaultRoute(routes: ApiRoute[] | undefined): string | null {
  return routes?.find((r) => r.available)?.id ?? null;
}

const PROVIDER_NAMES: Record<string, string> = { anthropic: "Anthropic", openai: "OpenAI", gemini: "Gemini", deepseek: "DeepSeek" };

/** A tag for a session on an API key ("DeepSeek API"); null on its harness's own login or provider. */
export function routeTag(route: string | null | undefined, harness?: string | null): string | null {
  if (!route || route.endsWith(":subscription")) return null;
  const provider = route.split(":")[0];
  if (known.get(harness ?? "claude")?.routes?.[0]?.id === route) return null;   // the harness's own default route
  return `${PROVIDER_NAMES[provider] ?? provider} API`;
}
