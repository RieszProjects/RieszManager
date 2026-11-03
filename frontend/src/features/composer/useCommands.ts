/** Slash commands and models available for a session (or a project, before a session exists). */
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { ModelInfo, SlashCommand } from "@/api/types";

export interface ModelDefaults {
  model: string | null;
  resolvedModel: string | null;
  effort: string | null;
}

interface Result {
  commands: SlashCommand[];
  models: ModelInfo[];
  defaults: ModelDefaults | null;
}

const cache = new Map<string, { at: number; data: Result }>();
const TTL = 5 * 60 * 1000;

export function useCommands(sessionId: string | null, projectId: string | null): Result {
  const key = sessionId ? `s:${sessionId}` : `p:${projectId ?? ""}`;
  const [data, setData] = useState<Result>(() => cache.get(key)?.data ?? { commands: [], models: [], defaults: null });

  useEffect(() => {
    let cancelled = false;
    const cached = cache.get(key);
    if (cached) setData(cached.data);
    if (cached && Date.now() - cached.at < TTL) return;
    const path = sessionId ? `sessions/${sessionId}/commands` : "commands";
    api<Result>(path, { query: sessionId ? {} : { project: projectId ?? undefined } })
      .then((res) => {
        const value = { commands: res.commands ?? [], models: res.models ?? [], defaults: res.defaults ?? null };
        cache.set(key, { at: Date.now(), data: value });
        if (!cancelled) setData(value);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [key, sessionId, projectId]);

  return data;
}

export function invalidateCommands(sessionId: string): void {
  cache.delete(`s:${sessionId}`);
}
