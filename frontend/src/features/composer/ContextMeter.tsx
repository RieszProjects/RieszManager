/**
 * A small pie next to the send button showing how full the conversation's
 * context is, measured against the point where Claude Code compacts it
 * automatically (window size minus its reserved buffer). Hover (or tap on a
 * phone) for the numbers.
 *
 * The size comes from the token usage of the latest reply in the transcript:
 * what was sent to the model plus what it wrote back, which is what the next
 * request starts from. The window and buffer come from the runner.
 */
import { ClickAwayListener, Tooltip } from "@mui/material";
import { useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { api } from "@/api/client";
import type { Item } from "@/api/types";
import { useSessions } from "@/store/sessions";

interface Limits {
  maxTokens: number;
  autocompactBuffer: number;
  liveTokens?: number | null;
}

const cache = new Map<string, Limits>();
const EMPTY: Item[] = [];
const noHover = () => window.matchMedia?.("(hover: none)").matches ?? false;

function contextSize(items: Item[]): { tokens: number | null; compactions: number; model: string | null } {
  let compactions = 0;
  for (const it of items) if (it.kind === "compact") compactions++;
  for (let i = items.length - 1; i >= 0; i--) {
    const it = items[i];
    if (it.kind === "compact") return { tokens: it.postTokens ?? null, compactions, model: null };
    if (it.kind === "assistant" && !it.synthetic && it.usage) {
      const u = it.usage;
      const tokens = (u.input_tokens ?? 0) + (u.cache_read_input_tokens ?? 0) + (u.cache_creation_input_tokens ?? 0) + (u.output_tokens ?? 0);
      return { tokens, compactions, model: it.model };
    }
  }
  return { tokens: null, compactions, model: null };
}

function fmt(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(2)}M`;
  if (n >= 1000) return `${Math.round(n / 1000)}k`;
  return String(n);
}

export function ContextMeter({ sessionId, model, hosted }: { sessionId: string; model?: string | null; hosted: boolean }) {
  const items = useSessions((s) => s.data[sessionId]?.items ?? EMPTY);
  const size = useMemo(() => contextSize(items), [items]);
  const usedModel = model || size.model;
  const key = `${sessionId}|${usedModel ?? ""}|${hosted ? `h${size.compactions}` : ""}`;
  const [limits, setLimits] = useState<Limits | null>(() => cache.get(key) ?? null);
  const [open, setOpen] = useState(false);
  const touch = useRef(noHover());

  useEffect(() => {
    const hit = cache.get(key);
    if (hit) {
      setLimits(hit);
      return;
    }
    let cancelled = false;
    api<Limits>(`sessions/${sessionId}/context-limits`, { query: { model: size.model ?? undefined } })
      .then((res) => {
        if (!res.maxTokens) return;
        cache.set(key, res);
        if (!cancelled) setLimits(res);
      })
      .catch(() => undefined);
    return () => {
      cancelled = true;
    };
    // size.model only matters for the first fetch of a key, so it is not a dependency.
  }, [key, sessionId]);

  if (!limits) return null;
  const threshold = Math.max(1, limits.maxTokens - limits.autocompactBuffer);
  const justCompacted = size.tokens === null && size.compactions > 0; // compacted, size not recorded
  const used = size.tokens ?? limits.liveTokens ?? null;
  if (used === null && !justCompacted) return null;
  const ratio = used === null ? 0 : Math.min(1, used / threshold);
  const pct = Math.round(ratio * 100);
  const level = ratio >= 0.9 ? "high" : ratio >= 0.7 ? "mid" : "";

  const title = (
    <div className="rz-ctx-tip">
      {used === null ? (
        <div>刚压缩过上下文</div>
      ) : (
        <>
          <div><b>上下文已用 {pct}%</b></div>
          <div className="dim">{fmt(used)} / {fmt(threshold)} tokens</div>
          <div className="dim">{ratio >= 1 ? "即将自动压缩" : `距离自动压缩还有 ${fmt(threshold - used)}`}</div>
        </>
      )}
    </div>
  );

  return (
    <ClickAwayListener onClickAway={() => setOpen(false)}>
      <span className="rz-ctx-wrap">
        <Tooltip
          title={title}
          placement="top"
          open={open}
          onOpen={() => !touch.current && setOpen(true)}
          onClose={() => setOpen(false)}
          disableTouchListener
          disableFocusListener={touch.current}
          disableHoverListener={touch.current}
        >
          <button
            type="button"
            className={`rz-ctx-meter ${level}`}
            aria-label={used === null ? "刚压缩过上下文" : `上下文已用 ${pct}%`}
            onClick={() => touch.current && setOpen((v) => !v)}
            style={{ "--p": ratio } as CSSProperties}
          >
            <span />
          </button>
        </Tooltip>
      </span>
    </ClickAwayListener>
  );
}
