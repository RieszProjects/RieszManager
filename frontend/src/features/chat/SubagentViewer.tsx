/** Read-only conversation adapter for a harness-native subagent, reusing ConversationShell, MessageList and Composer. */
import { IconButton, Tooltip } from "@mui/material";
import { ArrowLeft, GitBranch, LoaderCircle } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/api/client";
import type { Item, LiveInfo, SessionState } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { useHarnessLabel, useRouteTag } from "@/lib/harnesses";
import { sessionTitle, useApp } from "@/store/app";
import { useSessions } from "@/store/sessions";
import { type SubagentTarget, useUi } from "@/store/ui";
import { Composer } from "../composer/Composer";
import { NodeApprovalBanner } from "../nodes/NodeApprovalBanner";
import { NodesButton } from "../nodes/NodesButton";
import { NotesButton } from "../notes/NotesButton";
import { ConversationShell } from "./ConversationShell";
import { MessageList } from "./MessageList";
import type { SessionContextValue } from "./SessionContext";
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
  const parentSummary = useApp((s) => s.sessions[target.sessionId]);
  const projects = useApp((s) => s.projects);
  const state = useSessions((s) => s.data[target.sessionId]?.state as SessionState | undefined);
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

  const projectId = target.projectId ?? parentSummary?.project ?? null;
  useEffect(() => {
    if (projectId) setContextProject(projectId);
  }, [projectId, setContextProject]);

  const toolUseId = target.toolUseId ?? agent?.toolUseId ?? null;
  const task = useMemo(
    () => (toolUseId ? (state?.tasks ?? []).find((t) => t.toolUseId === toolUseId) : undefined),
    [state?.tasks, toolUseId],
  );
  const liveRunning = useApp((s) => Boolean(toolUseId && s.live[target.sessionId]?.agents?.includes(toolUseId)));
  const running = liveStatus ?? agent?.running ?? (Boolean(task && state?.alive) || liveRunning);

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

  const context = useMemo<SessionContextValue>(() => {
    const pending = new Set<string>();
    if (running) {
      for (const it of items) if (it.kind === "assistant") for (const b of it.blocks) if (b.type === "tool" && !b.result) pending.add(b.id);
    }
    return {
      sessionId: target.sessionId,
      cwd: target.cwd ?? parentSummary?.cwd ?? null,
      projectId,
      pendingTools: pending,
      tasks: [],
      hosted: false,
      harness,
      capabilities: state?.capabilities ?? null,
    };
  }, [items, running, target.sessionId, target.cwd, parentSummary?.cwd, projectId, harness, state?.capabilities]);

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
  const modelInherited = !transcriptModel && !explicitModel && Boolean(parentModel);

  const isResearchSandbox = agent?.agentType === "research";
  const effectiveMode = isResearchSandbox ? "plan" : (state?.permissionMode ?? parentSummary?.permissionMode ?? "default");
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

  return (
    <ConversationShell
      context={context}
      headStart={
        <Tooltip title={`返回父会话：${parentName}`}>
          <IconButton size="small" onClick={backToParent} aria-label="返回父会话">
            <ArrowLeft size={18} />
          </IconButton>
        </Tooltip>
      }
      title={title}
      subtitle={
        <>
          <StatusDot live={liveDot} size={7} />
          <span className={`rz-tag harness harness-${harness}`}>{harnessName}</span>
          {apiTag && <span className="rz-tag harness">{apiTag}</span>}
          {project && <span className="clamp-1">{project.name}</span>}
          {parentSummary?.gitBranch && <span className="dim mono clamp-1">⎇ {parentSummary.gitBranch}</span>}
          {agent?.agentType && <span className="rz-tag">{agent.agentType}</span>}
          <span className="dim clamp-1">父会话：{parentName}</span>
        </>
      }
      headActions={
        <>
          <NodesButton sessionId={target.sessionId} />
          <NotesButton sessionId={target.sessionId} />
        </>
      }
      body={
        error ? (
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
            footer={
              <ActivityLine
                busy={running}
                since={task?.startedAt ? task.startedAt * 1000 : null}
                waiting={false}
                label="子代理运行中"
                interruptible={false}
              />
            }
            emptyState={
              <div className="rz-chat-empty">
                <div className="dim">{running ? "子代理已启动，等待输出…" : "暂无消息记录"}</div>
              </div>
            }
          />
        )
      }
      bottom={
        <>
          <NodeApprovalBanner sessionId={target.sessionId} />
          <Composer
            sessionId={target.sessionId}
            projectId={projectId}
            state={state ?? null}
            defaultModel={effectiveModel}
            defaultMode={effectiveMode}
            harness={harness}
            api={state?.api ?? parentSummary?.api}
            readOnly
            placeholder="内置子代理由父会话驱动，当前为只读模式"
            readOnlyModeLabel={isResearchSandbox ? "只读沙箱" : undefined}
            readOnlyModeTier={isResearchSandbox ? "readonly" : undefined}
            readOnlyModeTitle={isResearchSandbox ? "研究型子代理运行在只读沙箱中" : "权限模式（继承父会话）"}
            readOnlyModelTitle={effectiveModel ? (modelInherited ? `模型（继承父会话）：${effectiveModel}` : `模型：${effectiveModel}`) : undefined}
            extraChips={
              workspaceTag ? (
                <button type="button" className="rz-chip" disabled title={`工作区隔离模式：${workspaceTag}`}>
                  <GitBranch size={13} />
                  <span>{workspaceTag}</span>
                </button>
              ) : undefined
            }
            rightNote={statsParts.length > 0 ? <span className="rz-composer-readonly-stat dim">{statsParts.join(" · ")}</span> : undefined}
          />
        </>
      }
    />
  );
}
