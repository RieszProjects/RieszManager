/** A subagent's own conversation, opened from its tool card or a session list. Refreshes while the subagent runs. */
import { Button, Dialog, IconButton } from "@mui/material";
import { LoaderCircle, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Item, SessionState } from "@/api/types";
import { useIsMobile } from "@/lib/useIsMobile";
import { useApp } from "@/store/app";
import { useSessions } from "@/store/sessions";
import { useUi } from "@/store/ui";
import { ItemView } from "./MessageItems";
import { SessionContext, type SessionContextValue } from "./SessionContext";

const POLL_MS = 3000;
const PAGE = 160;

interface SubagentInfo {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
}

interface Page {
  items: Item[];
  hasMore: boolean;
}

export function SubagentViewer() {
  const target = useUi((s) => s.subagent);
  const openSubagent = useUi((s) => s.openSubagent);
  const isMobile = useIsMobile();
  const state = useSessions((s) => (target ? (s.data[target.sessionId]?.state as SessionState | undefined) : undefined));
  const [agent, setAgent] = useState<SubagentInfo | null>(null);
  const [items, setItems] = useState<Item[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  /** Earlier pages were loaded by hand: refreshes of the latest page leave "load earlier" alone. */
  const olderLoaded = useRef(false);

  const toolUseId = target?.toolUseId ?? agent?.toolUseId ?? null;
  const task = useMemo(
    () => (toolUseId ? (state?.tasks ?? []).find((t) => t.toolUseId === toolUseId) : undefined),
    [state?.tasks, toolUseId],
  );
  const liveRunning = useApp((s) => Boolean(target && toolUseId && s.live[target.sessionId]?.agents?.includes(toolUseId)));
  const running = Boolean(task && state?.alive) || liveRunning;

  // Find the subagent of this tool call.
  useEffect(() => {
    setAgent(null);
    setItems([]);
    setHasMore(false);
    setError(null);
    stick.current = true;
    olderLoaded.current = false;
    if (!target) return;
    let cancelled = false;
    setLoading(true);
    api<{ subagents: SubagentInfo[] }>(`sessions/${target.sessionId}/subagents`)
      .then((res) => {
        if (cancelled) return;
        const found = res.subagents.find((a) => (target.agentId ? a.agentId === target.agentId : a.toolUseId === target.toolUseId));
        if (found) setAgent(found);
        else setError("没有找到这个子代理的记录");
      })
      .catch((e: Error) => !cancelled && setError(e.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [target]);

  const loadLatest = useCallback(async () => {
    if (!target || !agent) return;
    const page = await api<Page>(`sessions/${target.sessionId}/subagents/${agent.agentId}/items`, { query: { limit: PAGE } });
    setItems((prev) => {
      // Keep older pages already loaded; replace the latest page.
      const first = page.items[0]?.seq;
      const older = first === undefined ? [] : prev.filter((it) => it.seq < first);
      return [...older, ...page.items];
    });
    if (!olderLoaded.current) setHasMore(page.hasMore);
  }, [target, agent]);

  useEffect(() => {
    if (!agent) return;
    void loadLatest().catch((e: Error) => setError(e.message));
  }, [agent, loadLatest]);

  useEffect(() => {
    if (!agent || !running) return;
    const timer = window.setInterval(() => void loadLatest().catch(() => undefined), POLL_MS);
    return () => window.clearInterval(timer);
  }, [agent, running, loadLatest]);

  const loadEarlier = async () => {
    if (!target || !agent || !items.length) return;
    const page = await api<Page>(`sessions/${target.sessionId}/subagents/${agent.agentId}/items`,
      { query: { limit: PAGE, before: items[0].seq } });
    stick.current = false;
    olderLoaded.current = true;
    setItems((prev) => [...page.items, ...prev]);
    setHasMore(page.hasMore);
  };

  // Follow new steps while scrolled to the bottom.
  useEffect(() => {
    const el = body.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [items]);

  const context = useMemo<SessionContextValue>(() => {
    const pending = new Set<string>();
    if (running) {
      for (const it of items) if (it.kind === "assistant") for (const b of it.blocks) if (b.type === "tool" && !b.result) pending.add(b.id);
    }
    return { sessionId: target?.sessionId ?? "", cwd: target?.cwd ?? null, projectId: target?.projectId ?? null,
      pendingTools: pending, tasks: [], hosted: false };
  }, [items, running, target]);

  const close = () => openSubagent(null);
  const p = task?.progress;

  return (
    <Dialog open={Boolean(target)} onClose={close} fullScreen={isMobile} maxWidth={false}
      slotProps={{ paper: { className: "rz-fileviewer rz-subagent" } }}>
      {target && (
        <>
          <div className="rz-fileviewer-head">
            <div className="rz-fileviewer-title">
              <div className="clamp-1">{target.title || agent?.description || "子代理"}</div>
              <div className="dim">
                {[agent?.agentType, running ? "运行中" : null,
                  p?.toolUses != null ? `${p.toolUses} 次工具调用` : null,
                  p?.tokens != null ? `${Math.round(p.tokens / 1000)}k tokens` : null].filter(Boolean).join(" · ")}
              </div>
            </div>
            <IconButton size="small" onClick={close} aria-label="关闭"><X size={18} /></IconButton>
          </div>
          <div
            ref={body}
            className="rz-fileviewer-body rz-subagent-body"
            onScroll={(e) => {
              const el = e.currentTarget;
              stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
            }}
          >
            {error ? (
              <div className="rz-center dim">{error}</div>
            ) : loading && !items.length ? (
              <div className="rz-center dim"><LoaderCircle size={18} className="spin" /></div>
            ) : (
              <SessionContext.Provider value={context}>
                <div className="rz-msg-list">
                  {hasMore && (
                    <div className="rz-subagent-more">
                      <Button size="small" onClick={() => void loadEarlier()}>加载更早的内容</Button>
                    </div>
                  )}
                  {items.map((item) => (
                    <div key={item.key} className="rz-msg-row"><ItemView item={item} /></div>
                  ))}
                  {running && <div className="rz-msg-row rz-subagent-running dim"><LoaderCircle size={13} className="spin" /> 运行中</div>}
                </div>
              </SessionContext.Provider>
            )}
          </div>
        </>
      )}
    </Dialog>
  );
}
