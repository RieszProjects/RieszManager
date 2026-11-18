/** Other harnesses (Codex, DeepSeek Harness) the runner can start sessions of. */
import { useEffect, useState } from "react";
import { api } from "@/api/client";
import type { HarnessInfo, ModelInfo } from "@/api/types";

const SHORT: Record<string, string> = { claude: "Claude Code", codex: "Codex", dsh: "DeepSeek" };

/** A short name for tags and placeholders. */
export function harnessLabel(id: string | null | undefined): string {
  return SHORT[id ?? "claude"] ?? id ?? "Claude Code";
}

let listed: Promise<HarnessInfo[]> | null = null;
let listedAt = 0;
const TTL = 5 * 60 * 1000;

function fetchHarnesses(): Promise<HarnessInfo[]> {
  if (!listed || Date.now() - listedAt > TTL) {
    listedAt = Date.now();
    listed = api<{ harnesses: HarnessInfo[] }>("harnesses").then((r) => r.harnesses ?? []).catch(() => {
      listed = null;
      return [];
    });
  }
  return listed;
}

export function useHarnesses(): HarnessInfo[] {
  const [list, setList] = useState<HarnessInfo[]>([]);
  useEffect(() => {
    let cancelled = false;
    void fetchHarnesses().then((l) => !cancelled && setList(l));
    return () => {
      cancelled = true;
    };
  }, []);
  return list;
}

const models = new Map<string, { at: number; list: ModelInfo[] }>();

/** The models a harness offers, before a session of it exists. */
export function useHarnessModels(harness: string | null): ModelInfo[] {
  const [list, setList] = useState<ModelInfo[]>(() => (harness && models.get(harness)?.list) || []);
  useEffect(() => {
    if (!harness) {
      setList([]);
      return;
    }
    const hit = models.get(harness);
    setList(hit?.list ?? []);
    if (hit && Date.now() - hit.at < TTL) return;
    let cancelled = false;
    api<{ models: ModelInfo[] }>(`harnesses/${harness}/models`)
      .then((r) => {
        models.set(harness, { at: Date.now(), list: r.models ?? [] });
        if (!cancelled) setList(r.models ?? []);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [harness]);
  return list;
}
