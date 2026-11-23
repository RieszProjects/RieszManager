/** Main-area full-screen read-only view for a harness-native subagent's conversation. Refreshes while running. */
import { IconButton, Tooltip } from "@mui/material";
import { ArrowLeft, ArrowUp, Bot, Cpu, GitBranch, LoaderCircle, Paperclip, Shield } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Item, LiveInfo, SessionState } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { modeTier, modelLabel, permissionLabel } from "@/lib/format";
import { useHarnesses, useHarnessLabel, useRouteTag } from "@/lib/harnesses";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { useSessions } from "@/store/sessions";
import { type SubagentTarget, useUi } from "@/store/ui";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";
import { NodeApprovalBanner } from "../nodes/NodeApprovalBanner";
import { NodeBindChip } from "../nodes/NodeBindChip";
import { NodesButton } from "../nodes/NodesButton";
import { NotesButton } from "../notes/NotesButton";
import { MessageList } from "./MessageList";
import { SessionContext, type SessionContextValue } from "./SessionContext";
import { ActivityLine } from "./StreamingView";

const POLL_MS = 3000;
const PAGE = 160;

interface SubagentInfo {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
  harness?: string;
  model?: string;
  workspace?: string;
  running?: boolean;
}

interface Page {
  items: Item[];
  hasMore: boolean;
  running?: boolean;
}

const WORKSPACE_LABEL: Record<string, string> = {
  branch: "独立分支工作区",
  share: "共享仓库工作区",
};

export function SubagentViewer({ target }: { target: SubagentTarget }) {
  const openSubagent = useUi((s) => s.openSubagent);
  const navigate = useUi((s) => s.navigate);
  const setContextProject = useUi((s) => s.setContextProject);
  const isMobile = useIsMobile();
  const parentSummary = useApp((s) => s.sessions[target.sessionId]);
  const projects = useApp((s) => s.projects);
  const state = useSessions((s) => s.data[target.sessionId]?.state as SessionState | undefined);
  const harnesses = useHarnesses();
  const [agent, setAgent] = useState<SubagentInfo | null>(null);
  const [liveStatus, setLiveStatus] = useState<boolean | undefined>(undefined);
  const [items, setItems] = useState<Item[]>([]);
  const [hasMore, setHasMore] = useState(false);
  const [loaded, setLoaded] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);
  const [error, setError] = useState<string | null>(null);
  /** Earlier pages were loaded by hand: refreshes of the latest page leave "load earlier" alone. */
  const olderLoaded = useRef(false);
  const loadingOlderRef = useRef(false);

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
    setLoaded(false);
    setLoadingOlder(false);
    setError(null);
    olderLoaded.current = false;
    loadingOlderRef.current = false;
    let cancelled = false;
    api<{ subagents: SubagentInfo[] }>(`sessions/${target.sessionId}/subagents`)
      .then((res) => {
        if (cancelled) return;
        const found = res.subagents.find((a) => (target.agentId ? a.agentId === target.agentId : a.toolUseId === target.toolUseId));
        if (found) setAgent(found);
        else {
          setError("没有找到这个子代理的记录");
          setLoaded(true);
        }
      })
      .catch((e: Error) => {
        if (!cancelled) {
          setError(e.message);
          setLoaded(true);
        }
      });
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
    setLoaded(true);
  }, [target.sessionId, agent]);

  useEffect(() => {
    if (!agent) return;
    let cancelled = false;
    void loadLatest().catch((e: Error) => {
      if (!cancelled) {
        setError(e.message);
        setLoaded(true);
      }
    });
    return () => {
      cancelled = true;
    };
  }, [agent, loadLatest]);

  useEffect(() => {
    if (!agent || !running) return;
    const timer = window.setInterval(() => void loadLatest().catch(() => undefined), POLL_MS);
    return () => window.clearInterval(timer);
  }, [agent, running, loadLatest]);

  const loadEarlier = useCallback(async () => {
    if (!agent || !items.length || loadingOlderRef.current) return;
    loadingOlderRef.current = true;
    setLoadingOlder(true);
    try {
      const page = await api<Page>(`sessions/${target.sessionId}/subagents/${agent.agentId}/items`, {
        query: { limit: PAGE, before: items[0].seq },
      });
      olderLoaded.current = true;
      setItems((prev) => {
        const seen = new Set(prev.map((it) => it.seq));
        const fresh = page.items.filter((it) => !seen.has(it.seq));
        return fresh.length ? [...fresh, ...prev] : prev;
      });
      setHasMore(page.hasMore);
    } finally {
      loadingOlderRef.current = false;
      setLoadingOlder(false);
    }
  }, [agent, items, target.sessionId]);

  const harness = agent?.harness ?? parentSummary?.harness ?? "claude";
  const harnessName = useHarnessLabel(harness);
  const apiTag = useRouteTag(state?.api ?? parentSummary?.api, harness);
  const harnessSpec = harnesses.find((h) => h.id === harness);

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
      harness,
      capabilities: state?.capabilities ?? null,
    };
  }, [items, running, target, parentSummary?.cwd, parentSummary?.project, harness, state?.capabilities]);

  const backToParent = () => {
    openSubagent(null);
    navigate({ view: "session", sessionId: target.sessionId });
  };

  // Resolve subagent model: transcript assistant model > explicit subagent model > parent session model
  const transcriptModel = useMemo(() => {
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === "assistant" && it.model) return it.model;
    }
    return null;
  }, [items]);
  const explicitModel = agent?.model && agent.model !== "inherit" ? agent.model : null;
  const parentModel = state?.model ?? parentSummary?.model ?? null;
  const effectiveModel = transcriptModel ?? explicitModel ?? parentModel;
  const modelDisplay = effectiveModel ? modelLabel(effectiveModel) : null;
  const modelInherited = !transcriptModel && !explicitModel && Boolean(parentModel);

  // Resolve permission mode: research subagent runs read-only sandbox; otherwise inherits parent's permission mode
  const isResearchSandbox = agent?.agentType === "research";
  const rawPerm = isResearchSandbox ? "plan" : (state?.permissionMode ?? parentSummary?.permissionMode ?? "default");
  const modeOptions = state?.modes ?? harnessSpec?.modes ?? null;
  const permLabel = isResearchSandbox
    ? "只读沙箱"
    : (modeOptions?.find((m) => m.id === rawPerm)?.name ?? permissionLabel(rawPerm));
  const permTier = isResearchSandbox ? "readonly" : modeTier(rawPerm, modeOptions);

  const projectId = target.projectId ?? parentSummary?.project ?? null;
  const project = projectId ? projects.find((pr) => pr.id === projectId) : undefined;
  const parentName = parentSummary ? sessionTitle(parentSummary) : target.sessionId.slice(0, 8);
  const workspaceTag = agent?.workspace ? WORKSPACE_LABEL[agent.workspace] : null;
  const title = target.title || agent?.description || "子代理";

  useEffect(() => {
    document.title = `${title} · Riesz Manager`;
    return () => {
      document.title = "Riesz Manager";
    };
  }, [title]);

  const p = task?.progress;
  const statsParts = [
    p?.toolUses != null ? `${p.toolUses} 次工具调用` : null,
    p?.tokens != null ? `${Math.round(p.tokens / 1000)}k tokens` : null,
  ].filter(Boolean);

  const liveDot: LiveInfo | null = running ? { hosted: true, busy: true, pid: 0 } : null;
  const sessionKey = `subagent:${target.sessionId}:${agent?.agentId ?? target.agentId ?? target.toolUseId ?? ""}`;

  const chips = (
    <>
      <button type="button" className={`rz-chip harness-${harness}`} disabled title={`所属工具：${harnessName}`}>
        <Bot size={13} />
        <span>{harnessName}</span>
      </button>
      <button
        type="button"
        className={`rz-chip tier-${permTier}`}
        disabled
        title={isResearchSandbox ? "研究型子代理运行在只读沙箱中" : `权限模式（继承父会话）：${permLabel}`}
      >
        <Shield size={13} />
        <span>{permLabel}</span>
      </button>
      {modelDisplay && (
        <button
          type="button"
          className="rz-chip"
          disabled
          title={modelInherited ? `模型（继承父会话）：${effectiveModel}` : `模型：${effectiveModel}`}
        >
          <Cpu size={13} />
          <span>{modelDisplay}</span>
        </button>
      )}
      {workspaceTag && (
        <button type="button" className="rz-chip" disabled title={`工作区隔离模式：${workspaceTag}`}>
          <GitBranch size={13} />
          <span>{workspaceTag}</span>
        </button>
      )}
      <NodeBindChip sessionId={target.sessionId} />
    </>
  );

  const footer = (
    <ActivityLine
      busy={running}
      since={task?.startedAt ? task.startedAt * 1000 : null}
      waiting={false}
      label="子代理运行中"
      interruptible={false}
    />
  );

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
            <div className="clamp-1 rz-chat-title-text">{title}</div>
            <div className="rz-chat-sub">
              <StatusDot live={liveDot} size={7} />
              <span className={`rz-tag harness harness-${harness}`}>{harnessName}</span>
              {apiTag && <span className="rz-tag harness">{apiTag}</span>}
              {project && <span className="clamp-1">{project.name}</span>}
              {parentSummary?.gitBranch && <span className="dim mono clamp-1">⎇ {parentSummary.gitBranch}</span>}
              {agent?.agentType && <span className="rz-tag">{agent.agentType}</span>}
              <span className="dim clamp-1">父会话：{parentName}</span>
            </div>
          </div>
          <NodesButton sessionId={target.sessionId} />
          <NotesButton sessionId={target.sessionId} />
          {isMobile ? <MobileHeaderButtons side="right" /> : <RightPanelButton />}
        </header>
        <div className="rz-chat-row">
          <div className="rz-chat-col">
            <div className="rz-chat-body">
              {error ? (
                <div className="rz-center dim">{error}</div>
              ) : !loaded ? (
                <div className="rz-center"><LoaderCircle className="spin" size={22} /></div>
              ) : (
                <MessageList
                  sessionKey={sessionKey}
                  items={items}
                  hasMore={hasMore}
                  loadingOlder={loadingOlder}
                  onLoadOlder={() => void loadEarlier()}
                  footer={footer}
                  emptyState={
                    <div className="rz-chat-empty">
                      <div className="dim">{running ? "子代理已启动，等待输出…" : "暂无消息记录"}</div>
                    </div>
                  }
                />
              )}
            </div>
            <div className="rz-chat-bottom">
              <NodeApprovalBanner sessionId={target.sessionId} />
              <div className="rz-composer-wrap">
                {isMobile && <div className="rz-composer-chips rz-composer-readonly-chips">{chips}</div>}
                <div className="rz-composer readonly">
                  <textarea
                    rows={1}
                    disabled
                    value=""
                    placeholder="内置子代理由父会话驱动，当前为只读模式，无法直接发送消息"
                  />
                  <div className="rz-composer-bar">
                    <IconButton size="small" disabled aria-label="添加文件"><Paperclip size={17} /></IconButton>
                    {!isMobile && chips}
                    <div className="rz-spacer" />
                    {statsParts.length > 0 && <span className="rz-composer-readonly-stat dim">{statsParts.join(" · ")}</span>}
                    <Tooltip title="内置子代理只读，无法直接发送消息">
                      <span>
                        <IconButton className="rz-send" disabled aria-label="发送">
                          <ArrowUp size={18} />
                        </IconButton>
                      </span>
                    </Tooltip>
                  </div>
                </div>
              </div>
            </div>
          </div>
          <RightDock />
        </div>
      </div>
    </SessionContext.Provider>
  );
}
