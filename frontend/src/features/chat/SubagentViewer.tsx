/** Main-area full-screen read-only view for a harness-native subagent's conversation. Refreshes while running. */
import { Button, IconButton, Tooltip } from "@mui/material";
import { ArrowLeft, Eye, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Item, SessionState } from "@/api/types";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { useSessions } from "@/store/sessions";
import { type SubagentTarget, useUi } from "@/store/ui";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";
import { ItemView } from "./MessageItems";
import { SessionContext, type SessionContextValue } from "./SessionContext";

const POLL_MS = 3000;
const PAGE = 160;

interface SubagentInfo {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
  running?: boolean;
}

interface Page {
  items: Item[];
  hasMore: boolean;
  running?: boolean;
}

export function SubagentViewer({ target }: { target: SubagentTarget }) {
  const openSubagent = useUi((s) => s.openSubagent);
  const navigate = useUi((s) => s.navigate);
  const setContextProject = useUi((s) => s.setContextProject);
  const isMobile = useIsMobile();
  const parentSummary = useApp((s) => s.sessions[target.sessionId]);
  const state = useSessions((s) => s.data[target.sessionId]?.state as SessionState | undefined);
  const [agent, setAgent] = useState<SubagentInfo | null>(null);
  const [liveStatus, setLiveStatus] = useState<boolean | undefined>(undefined);
  const [items, setItems] = useState<Item[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const body = useRef<HTMLDivElement>(null);
  const stick = useRef(true);
  /** Earlier pages were loaded by hand: refreshes of the latest page leave "load earlier" alone. */
  const olderLoaded = useRef(false);

  useEffect(() => {
    const pid = target.projectId ?? parentSummary?.project ?? null;
    if (pid) setContextProject(pid);
  }, [target.projectId, parentSummary?.project, setContextProject]);

  const toolUseId = target.toolUseId ?? agent?.toolUseId ?? null;
  const task = useMemo(
    () => (toolUseId ? (state?.tasks ?? []).find((t) => t.toolUseId === toolUseId) : undefined),
    [state?.tasks, toolUseId],
  );
  const liveRunning = useApp((s) => Boolean(toolUseId && s.live[target.sessionId]?.agents?.includes(toolUseId)));
  const running = liveStatus ?? agent?.running ?? (Boolean(task && state?.alive) || liveRunning);

  // Find the subagent of this tool call / agentId.
  useEffect(() => {
    setAgent(null);
    setLiveStatus(undefined);
    setItems([]);
    setHasMore(false);
    setError(null);
    stick.current = true;
    olderLoaded.current = false;
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
    if (!agent) return;
    const page = await api<Page>(`sessions/${target.sessionId}/subagents/${agent.agentId}/items`, { query: { limit: PAGE } });
    setItems((prev) => {
      const first = page.items[0]?.seq;
      const older = first === undefined ? [] : prev.filter((it) => it.seq < first);
      return [...older, ...page.items];
    });
    if (!olderLoaded.current) setHasMore(page.hasMore);
    if (typeof page.running === "boolean") setLiveStatus(page.running);
  }, [target.sessionId, agent]);

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
    if (!agent || !items.length) return;
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
    return {
      sessionId: target.sessionId,
      cwd: target.cwd ?? parentSummary?.cwd ?? null,
      projectId: target.projectId ?? parentSummary?.project ?? null,
      pendingTools: pending,
      tasks: [],
      hosted: false,
    };
  }, [items, running, target, parentSummary?.cwd, parentSummary?.project]);

  const backToParent = () => {
    openSubagent(null);
    navigate({ view: "session", sessionId: target.sessionId });
  };

  const parentName = parentSummary ? sessionTitle(parentSummary) : target.sessionId.slice(0, 8);
  const p = task?.progress;
  const metaParts = [
    agent?.agentType,
    running ? "运行中" : "已完成",
    p?.toolUses != null ? `${p.toolUses} 次工具调用` : null,
    p?.tokens != null ? `${Math.round(p.tokens / 1000)}k tokens` : null,
  ].filter(Boolean);

  return (
    <SessionContext.Provider value={context}>
      <div className="rz-chat">
        <header className="rz-chat-head">
          {isMobile && <MobileHeaderButtons side="left" />}
          <Tooltip title={`返回父会话：${parentName}`}>
            <IconButton size="small" onClick={backToParent} aria-label="返回父会话">
              <ArrowLeft size={18} />
            </IconButton>
          </Tooltip>
          <div className="rz-chat-title">
            <div className="clamp-1 rz-chat-title-text">{target.title || agent?.description || "子代理"}</div>
            <div className="rz-chat-sub">
              <span className="rz-tag"><Eye size={10} />内置子代理 · 只读</span>
              <span className="dim clamp-1">父会话：{parentName}</span>
              {metaParts.length > 0 && <span className="dim clamp-1">· {metaParts.join(" · ")}</span>}
            </div>
          </div>
          {isMobile ? <MobileHeaderButtons side="right" /> : <RightPanelButton />}
        </header>
        <div className="rz-chat-row">
          <div className="rz-chat-col">
            <div
              ref={body}
              className="rz-chat-body rz-subagent-body"
              onScroll={(e) => {
                const el = e.currentTarget;
                stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
              }}
            >
              {error ? (
                <div className="rz-center dim">{error}</div>
              ) : loading && !items.length ? (
                <div className="rz-center dim"><LoaderCircle size={20} className="spin" /></div>
              ) : (
                <div className="rz-msg-list">
                  {hasMore && (
                    <div className="rz-subagent-more">
                      <Button size="small" onClick={() => void loadEarlier()}>加载更早的内容</Button>
                    </div>
                  )}
                  {items.map((item) => (
                    <div key={item.key} className="rz-msg-row"><ItemView item={item} /></div>
                  ))}
                  {running && (
                    <div className="rz-msg-row rz-subagent-running dim">
                      <LoaderCircle size={13} className="spin" /> 运行中
                    </div>
                  )}
                </div>
              )}
            </div>
            <div className="rz-chat-bottom">
              <div className="rz-subagent-readonly">
                <Eye size={14} />
                <span>内置子代理由父会话驱动，当前为实时只读视图。</span>
                <Button size="small" variant="outlined" onClick={backToParent} sx={{ ml: "auto", flexShrink: 0 }}>
                  返回父会话
                </Button>
              </div>
            </div>
          </div>
          <RightDock />
        </div>
      </div>
    </SessionContext.Provider>
  );
}
