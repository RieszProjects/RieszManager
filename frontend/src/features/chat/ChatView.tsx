/** A conversation: header, transcript, live activity, pending prompts and the composer. */
import { IconButton, ListItemIcon, Menu, MenuItem } from "@mui/material";
import {
  Archive, ArchiveRestore, Copy, EllipsisVertical, GitFork, Hand, LoaderCircle, MonitorUp, Pencil, Power, SquareTerminal, TriangleAlert, X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api } from "@/api/client";
import { socket } from "@/api/socket";
import type { QueueEntry, SessionState } from "@/api/types";
import { Mascot } from "@/components/Mascot";
import { StatusDot } from "@/components/StatusDot";
import { copyText } from "@/lib/clipboard";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { useOrganize } from "@/store/organize";
import { useSessions } from "@/store/sessions";
import { toast, useUi } from "@/store/ui";
import { Composer } from "../composer/Composer";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";
import { MessageList } from "./MessageList";
import { PendingPrompts } from "./PermissionCard";
import { SessionContext } from "./SessionContext";
import { EditableTitle } from "./EditableTitle";
import { ActivityLine, StreamingView } from "./StreamingView";
import { turnStatus, waitingLabel } from "./turnStatus";

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
          <span className="rz-tag">{q.state === "held" ? "等待空闲" : "排队中"}</span>
          <span className="clamp-1">{q.preview}</span>
          <button type="button" aria-label="撤回" onClick={() => void cancel(q.uuid)}><X size={13} /></button>
        </div>
      ))}
      {visible.some((q) => q.state !== "held") && <div className="rz-queue-hint">按 <kbd>Esc</kbd> 中断当前任务并立即发送</div>}
    </div>
  );
}

function SessionMenu({ sessionId, state, cwd, title }: { sessionId: string; state: SessionState | null; cwd: string | null; title: string }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const navigate = useUi((s) => s.navigate);
  const setRenameTarget = useUi((s) => s.setRenameTarget);
  const archived = useOrganize((s) => s.archived.session.has(sessionId));
  const setArchived = useOrganize((s) => s.setArchived);
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
        <MenuItem onClick={() => { close(); setRenameTarget({ sessionId, title }); }}>
          <ListItemIcon><Pencil size={16} /></ListItemIcon>重命名
        </MenuItem>
        <MenuItem onClick={() => { close(); void setArchived("session", sessionId, !archived); }}>
          <ListItemIcon>{archived ? <ArchiveRestore size={16} /> : <Archive size={16} />}</ListItemIcon>
          {archived ? "取消归档" : "归档"}
        </MenuItem>
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
        {state?.external && state.alive && !state.busy && (
          <MenuItem onClick={() => run(async () => {
            await api(`sessions/${sessionId}/takeover`, { body: {} });
            socket.resubscribe(sessionId);
          })}>
            <ListItemIcon><Hand size={16} /></ListItemIcon>从终端转到这里
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
  const isMobile = useIsMobile();
  const dropRef = useRef<((files: File[]) => void) | null>(null);
  const [dragging, setDragging] = useState(false);

  useEffect(() => socket.subscribeSession(sessionId), [sessionId]);

  const projectId = summary?.project ?? null;
  useEffect(() => {
    if (projectId) setContextProject(projectId);
  }, [projectId, setContextProject]);

  const state = data?.state ?? null;
  const items = data?.items ?? [];
  const project = projects.find((p) => p.id === projectId);
  const turn = useMemo(() => turnStatus(items, state), [items, state]);
  const ctx = useMemo(
    () => ({ sessionId, cwd: summary?.cwd ?? null, projectId, pendingTools: turn.pendingTools }),
    [sessionId, summary?.cwd, projectId, turn.pendingTools],
  );
  const hostedHere = Boolean(state?.hosted && state.alive);
  const waitingSince = turn.waitingSince ? Date.parse(turn.waitingSince) : null;
  const background = turn.backgroundTasks;
  const waiting = Boolean(state?.permissions?.length);

  useEffect(() => {
    document.title = `${sessionTitle(summary)} · Riesz`;
    return () => {
      document.title = "Riesz";
    };
  }, [summary]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragging(false);
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
      <ActivityLine
        busy={turn.working}
        since={turn.waitingTool && waitingSince ? waitingSince : data?.turnStartedAt ?? null}
        status={state?.status}
        waiting={waiting}
        label={turn.waitingTool ? waitingLabel(turn.waitingTool) : null}
        interruptible={hostedHere}
      />
      {!turn.working && background.length > 0 && (
        <ActivityLine
          busy
          since={Math.min(...background.map((t) => t.startedAt)) * 1000}
          waiting={false}
          label={`等待后台任务${background.length > 1 ? `（${background.length} 个）` : ""}：${background[0].description || background[0].type}`}
          interruptible={false}
        />
      )}
      {turn.terminated && <div className="rz-terminated">已终止 · 上次运行没有完成</div>}
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
            <div className="rz-chat-title-text"><EditableTitle sessionId={sessionId} title={sessionTitle(summary)} /></div>
            <div className="rz-chat-sub">
              <StatusDot live={live} size={7} />
              {project && <span className="clamp-1">{project.name}</span>}
              {summary?.gitBranch && <span className="dim mono clamp-1">⎇ {summary.gitBranch}</span>}
            </div>
          </div>
          <SessionMenu sessionId={sessionId} state={state} cwd={summary?.cwd ?? null} title={sessionTitle(summary)} />
          {isMobile ? <MobileHeaderButtons side="right" /> : <RightPanelButton />}
        </header>
        <div className="rz-chat-row">
        <div className="rz-chat-col">
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
          <QueueBar sessionId={sessionId} queue={state?.queue ?? []} />
          <Composer
            sessionId={sessionId}
            projectId={projectId}
            state={state}
            defaultModel={summary?.model}
            defaultMode={summary?.permissionMode}
            autoFocus
            dropRef={dropRef}
          />
        </div>
        </div>
        <RightDock />
        </div>
        {dragging && <div className="rz-dropzone">松开以添加附件</div>}
      </div>
    </SessionContext.Provider>
  );
}
