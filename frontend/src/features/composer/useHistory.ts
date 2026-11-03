/** Prompt history recall with ArrowUp / ArrowDown, scoped to a project directory. */
import { useCallback, useRef } from "react";
import { api } from "@/api/client";
import type { HistoryEntry } from "@/api/types";

export function usePromptHistory(projectId: string | null) {
  const entries = useRef<string[] | null>(null);
  const loadedFor = useRef<string | null>(null);
  const index = useRef(-1);
  const stash = useRef("");

  const ensure = useCallback(async () => {
    if (entries.current && loadedFor.current === projectId) return entries.current;
    try {
      const res = await api<{ history: HistoryEntry[] }>("history", { query: { project: projectId ?? undefined, limit: 200 } });
      entries.current = res.history.map((h) => h.text);
    } catch {
      entries.current = [];
    }
    loadedFor.current = projectId;
    return entries.current;
  }, [projectId]);

  const reset = useCallback(() => {
    index.current = -1;
    entries.current = null;
  }, []);

  /** Returns the text to show, or null when there is nothing further. */
  const step = useCallback(
    async (direction: 1 | -1, current: string): Promise<string | null> => {
      const list = await ensure();
      if (!list.length) return null;
      if (index.current === -1 && direction === 1) stash.current = current;
      const next = index.current + direction;
      if (next < -1 || next >= list.length) return null;
      index.current = next;
      return next === -1 ? stash.current : list[next];
    },
    [ensure],
  );

  return { step, reset, prefetch: ensure };
}
