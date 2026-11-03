/** A conversation: header, transcript, live activity, pending prompts and the composer. */
import { IconButton, ListItemIcon, Menu, MenuItem, Tooltip } from "@mui/material";
import {
  Copy, EllipsisVertical, GitFork, Hand, LoaderCircle, MonitorUp, PanelRight, Power, SquareTerminal, TriangleAlert, X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api } from "@/api/client";
import { socket } from "@/api/socket";
import type { QueueEntry, SessionState } from "@/api/types";
import { Mascot } from "@/components/Mascot";
import { StatusDot } from "@/components/StatusDot";
import { copyText } from "@/lib/clipboard";
import { useIsMobile, useIsWide } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { useSessions } from "@/store/sessions";
import { toast, useUi } from "@/store/ui";
import { Composer } from "../composer/Composer";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { MessageList } from "./MessageList";
import { PendingPrompts } from "./PermissionCard";
import { SessionContext } from "./SessionContext";
import { ActivityLine, StreamingView } from "./StreamingView";

function QueueBar({ sessionId, queue }: { sessionId: string; queue: QueueEntry[] }) {
  const visible = queue.filter((q) => q.preview);
  if (!visible.length) return null;
  const cancel = async (uuid: string) => {
    try {
      await api(`sessions/${sessionId}/cancel`, { body: { uuid } });
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };
  return (
    <div className="rz-queue">
      {visible.map((q) => (
        <div key={q.uuid} className="rz-queue-item">
          <span className="rz-tag">排队中</span>
          <span className="clamp-1">{q.preview}</span>
          <button type="button" aria-label="撤回" onClick={() => void cancel(q.uuid)}><X size={13} /></button>
        </div>
      ))}
      <div className="rz-queue-hint">按 <kbd>Esc</kbd> 中断当前任务并立即发送</div>
    </div>
  );
}

function ExternalBar({ sessionId, state }: { sessionId: string; state: SessionState }) {
  const navigate = useUi((s) => s.navigate);
  const [busy, setBusy] = useState(false);
  const fork = async () => {
    setBusy(true);
    try {
      const res = await api<{ sessionId: string }>(`sessions/${sessionId}/fork`, { body: {} });
      navigate({ view: "session", sessionId: res.sessionId });
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  const takeover = async () => {
    setBusy(true);
    try {
      await api(`sessions/${sessionId}/takeover`, { body: {} });
      socket.resubscribe(sessionId);
      toast("已接管该会话", "success");
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="rz-external">
      <div className="rz-external-text">
        <SquareTerminal size={16} />
        <div>
          <div>此会话正在终端中运行{state.busy ? "，正在工作" : "，当前空闲"}</div>
          <div className="dim">可以实时查看。要在这里继续，可分叉一份副本，或在空闲时接管（会关闭终端里的进程）。</div>
        </div>
      </div>
      <div className="rz-external-actions">
        <button type="button" className="rz-btn" disabled={busy} onClick={() => void fork()}><GitFork size={14} /> 分叉</button>
        <button type="button" className="rz-btn" disabled={busy || state.busy} onClick={() => void takeover()}><Hand size={14} /> 接管</button>
      </div>
    </div>
  );
}

function SessionMenu({ sessionId, state, cwd }: { sessionId: string; state: SessionState | null; cwd: string | null }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const navigate = useUi((s) => s.navigate);
  const close = () => setAnchor(null);
  const run = async (fn: () => Promise<void>) => {
    close();
    try {
      await fn();
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };
  return (
    <>
      <IconButton size="small" onClick={(e) => setAnchor(e.currentTarget)} aria-label="更多"><EllipsisVertical size={18} /></IconButton>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close} anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
        transformOrigin={{ vertical: "top", horizontal: "right" }}>
        <MenuItem onClick={() => run(async () => {
          const res = await api<{ sessionId: string }>(`sessions/${sessionId}/fork`, { body: {} });
          navigate({ view: "session", sessionId: res.sessionId });
        })}>
          <ListItemIcon><GitFork size={16} /></ListItemIcon>分叉为新会话
        </MenuItem>
        {state?.hosted && state.alive && (
          <MenuItem onClick={() => run(async () => {
            await api(`sessions/${sessionId}/stop`, { body: {} });
            toast("已结束后台进程，下次发送消息时会自动恢复", "success");
          })}>
            <ListItemIcon><Power size={16} /></ListItemIcon>结束后台进程
          </MenuItem>
        )}
        {!state?.alive && (
          <MenuItem onClick={() => run(async () => {
            await api(`sessions/${sessionId}/start`, { body: {} });
          })}>
            <ListItemIcon><MonitorUp size={16} /></ListItemIcon>预先启动进程
          </MenuItem>
        )}
        <MenuItem onClick={() => run(async () => {
          if (await copyText(sessionId)) toast("已复制会话 ID", "success");
        })}>
          <ListItemIcon><Copy size={16} /></ListItemIcon>复制会话 ID
        </MenuItem>
        <MenuItem onClick={() => run(async () => {
          const cmd = `${cwd ? `cd ${JSON.stringify(cwd)} && ` : ""}claude --resume ${sessionId}`;
          if (await copyText(cmd)) toast("已复制终端恢复命令", "success");
        })}>
          <ListItemIcon><SquareTerminal size={16} /></ListItemIcon>复制终端恢复命令
        </MenuItem>
      </Menu>
    </>
  );
}

export function ChatView({ sessionId }: { sessionId: string }) {
  const summary = useApp((s) => s.sessions[sessionId]);
  const live = useApp((s) => s.live[sessionId]);
  const projects = useApp((s) => s.projects);
  const data = useSessions((s) => s.data[sessionId]);
  const loadOlder = useSessions((s) => s.loadOlder);
  const setContextProject = useUi((s) => s.setContextProject);
  const rightOpen = useUi((s) => s.rightOpen);
  const setRightOpen = useUi((s) => s.setRightOpen);
  const setRightOverlay = useUi((s) => s.setRightOverlay);
  const isMobile = useIsMobile();
  const isWide = useIsWide();
  const dropRef = useRef<((files: File[]) => void) | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => socket.subscribeSession(sessionId), [sessionId]);

  const projectId = summary?.project ?? null;
  useEffect(() => {
    if (projectId) setContextProject(projectId);
  }, [projectId, setContextProject]);

  const state = data?.state ?? null;
  const items = data?.items ?? [];
  const external = Boolean(state?.external && state.alive && !state.hosted);
  const project = projects.find((p) => p.id === projectId);
  const ctx = useMemo(() => ({ sessionId, cwd: summary?.cwd ?? null, projectId }), [sessionId, summary?.cwd, projectId]);
  const waiting = Boolean(state?.permissions?.length);

  useEffect(() => {
    document.title = `${sessionTitle(summary)} · RieszManager`;
    return () => {
      document.title = "RieszManager";
    };
  }, [summary]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
    if (external) return;
    const files = Array.from(e.dataTransfer.files ?? []);
    if (files.length) dropRef.current?.(files);
  };

  const footer = (
    <>
      {data?.stream && <StreamingView stream={data.stream} />}
      {state?.hosted && <PendingPrompts requests={state.permissions ?? []} />}
      {data?.notices.map((n) => <div key={n.id} className={`rz-notice level-${n.level}`}>{n.text}</div>)}
      {data?.exited && data.exited.code !== 0 && !state?.alive && (
        <div className="rz-exit">
          <TriangleAlert size={14} /> Claude 进程异常退出（代码 {data.exited.code}）
          {data.exited.stderr.length > 0 && <pre>{data.exited.stderr.join("\n")}</pre>}
        </div>
      )}
      <ActivityLine busy={Boolean(state?.busy)} since={data?.turnStartedAt ?? null} status={state?.status} waiting={waiting} />
    </>
  );

  return (
    <SessionContext.Provider value={ctx}>
      <div
        className="rz-chat"
        onDragOver={(e) => {
          if (e.dataTransfer.types.includes("Files")) {
            e.preventDefault();
            setDragging(true);
          }
        }}
        onDragLeave={(e) => e.currentTarget === e.target && setDragging(false)}
        onDrop={onDrop}
      >
        <header className="rz-chat-head">
          {isMobile && <MobileHeaderButtons side="left" />}
          <div className="rz-chat-title">
            <div className="clamp-1 rz-chat-title-text">{sessionTitle(summary)}</div>
            <div className="rz-chat-sub">
              <StatusDot live={live} size={7} />
              {project && <span className="clamp-1">{project.name}</span>}
              {summary?.gitBranch && <span className="dim mono clamp-1">⎇ {summary.gitBranch}</span>}
            </div>
          </div>
          <SessionMenu sessionId={sessionId} state={state} cwd={summary?.cwd ?? null} />
          {isMobile ? (
            <MobileHeaderButtons side="right" />
          ) : (
            <Tooltip title={isWide && rightOpen ? "隐藏项目面板" : "显示项目面板"}>
              <IconButton size="small" onClick={() => (isWide ? setRightOpen(!rightOpen) : setRightOverlay(true))}><PanelRight size={18} /></IconButton>
            </Tooltip>
          )}
        </header>
        <div className="rz-chat-body">
          {!data?.loaded ? (
            <div className="rz-center"><LoaderCircle className="spin" size={22} /></div>
          ) : (
            <MessageList
              sessionKey={sessionId}
              items={items}
              hasMore={Boolean(data?.hasMore)}
              loadingOlder={Boolean(data?.loadingOlder)}
              onLoadOlder={() => void loadOlder(sessionId)}
              footer={footer}
              emptyState={
                <div className="rz-chat-empty">
                  <Mascot pose="sleepy" height={180} />
                  <div className="dim">会话已就绪</div>
                </div>
              }
            />
          )}
        </div>
        <div className="rz-chat-bottom">
          {state?.hosted && <QueueBar sessionId={sessionId} queue={state.queue ?? []} />}
          {external && state ? (
            <ExternalBar sessionId={sessionId} state={state} />
          ) : (
            <Composer
              sessionId={sessionId}
              projectId={projectId}
              state={state}
              defaultModel={summary?.model}
              defaultMode={summary?.permissionMode}
              autoFocus
              dropRef={dropRef}
            />
          )}
        </div>
        {dragging && <div className="rz-dropzone">松开以添加附件</div>}
      </div>
    </SessionContext.Provider>
  );
}
