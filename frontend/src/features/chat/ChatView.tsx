/** A conversation: header, transcript, live activity, pending prompts and the composer. */
import { IconButton, ListItemIcon, Menu, MenuItem } from "@mui/material";
import {
  AlarmClock, Archive, ArchiveRestore, ArrowLeftRight, Check, Copy, EllipsisVertical, GitFork, Hand, LoaderCircle, MonitorUp, Pencil, Pin, Power, RotateCcw, SquareTerminal, TriangleAlert, X,
} from "lucide-react";
import { useSchedules } from "@/features/schedules/store";
import { useEffect, useMemo, useRef, useState, type DragEvent } from "react";
import { api } from "@/api/client";
import { setKeepAlive } from "@/lib/keepAlive";
import { socket } from "@/api/socket";
import type { QueueEntry, SessionState } from "@/api/types";
import { Mascot } from "@/components/Mascot";
import { StatusDot } from "@/components/StatusDot";
import { copyText } from "@/lib/clipboard";
import { clockTime } from "@/lib/format";
import { handOff } from "@/lib/handoff";
import { harnessLabel, resumeCommand, useHarnessLabel, useHarnesses, useRouteTag } from "@/lib/harnesses";
import { sessionTitle, useApp } from "@/store/app";
import { useOrganize } from "@/store/organize";
import { useSessions } from "@/store/sessions";
import { toast, useUi } from "@/store/ui";
import { Composer } from "../composer/Composer";
import { NotesButton } from "../notes/NotesButton";
import { NodesButton } from "../nodes/NodesButton";
import { NodeApprovalBanner } from "../nodes/NodeApprovalBanner";
import { ConversationShell } from "./ConversationShell";
import { MessageList } from "./MessageList";
import { PendingPrompts } from "./PermissionCard";
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

/** "Continue in another tool": the conversation, without thinking, rewritten in that tool's own history. */
function HandoffMenu({ sessionId, harness, anchor, onClose }: { sessionId: string; harness: string; anchor: HTMLElement | null; onClose: () => void }) {
  const harnesses = useHarnesses();
  const [busy, setBusy] = useState<string | null>(null);
  const targets = harnesses.filter((h) => h.id !== harness);
  const go = async (target: string) => {
    setBusy(target);
    try {
      await handOff(sessionId, target);
      onClose();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setBusy(null);
    }
  };
  return (
    <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={onClose} anchorOrigin={{ vertical: "bottom", horizontal: "right" }}
      transformOrigin={{ vertical: "top", horizontal: "right" }}>
      {targets.map((h) => (
        <MenuItem key={h.id} disabled={!h.available || Boolean(busy)} onClick={() => void go(h.id)}>
          <ListItemIcon>{busy === h.id ? <LoaderCircle size={16} className="spin" /> : <ArrowLeftRight size={16} />}</ListItemIcon>
          <span className="rz-menu-grow">转到 {h.name} 继续</span>
        </MenuItem>
      ))}
    </Menu>
  );
}

function SessionMenu({ sessionId, state, cwd, title, keepAlive, harness }: {
  sessionId: string; state: SessionState | null; cwd: string | null; title: string; keepAlive: boolean; harness?: string;
}) {
  const [handoffAnchor, setHandoffAnchor] = useState<HTMLElement | null>(null);
  const setScheduleSession = useUi((s) => s.setScheduleSession);
  const scheduleCount = useSchedules((s) => s.items.filter((x) => x.sessionId === sessionId && x.enabled).length);
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
        <MenuItem onClick={() => { close(); setScheduleSession(sessionId); }}>
          <ListItemIcon><AlarmClock size={16} /></ListItemIcon>
          <span className="rz-menu-grow">定时唤醒</span>
          {scheduleCount > 0 && <span className="dim">{scheduleCount}</span>}
        </MenuItem>
        <MenuItem onClick={() => run(() => setKeepAlive(sessionId, !keepAlive))}>
          <ListItemIcon><Pin size={16} /></ListItemIcon>
          <span className="rz-menu-grow">保持运行</span>
          {keepAlive && <Check size={15} className="rz-menu-check" />}
        </MenuItem>
        {state?.hosted && state.alive && (
          <MenuItem onClick={() => run(async () => {
            const res = await api<{ keepAliveCleared?: boolean }>(`sessions/${sessionId}/stop`, { body: {} });
            toast(res.keepAliveCleared ? "已结束后台进程，并关闭了保持运行" : "已结束后台进程，下次发送消息时会自动恢复", "success");
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
        <MenuItem onClick={() => { setHandoffAnchor(anchor); close(); }}>
          <ListItemIcon><ArrowLeftRight size={16} /></ListItemIcon>转到其他工具继续
        </MenuItem>
        <MenuItem onClick={() => run(async () => {
          if (await copyText(sessionId)) toast("已复制会话 ID", "success");
        })}>
          <ListItemIcon><Copy size={16} /></ListItemIcon>复制会话 ID
        </MenuItem>
        {resumeCommand(harness, sessionId) && (
          <MenuItem onClick={() => run(async () => {
            const cmd = `${cwd ? `cd ${JSON.stringify(cwd)} && ` : ""}${resumeCommand(harness, sessionId)}`;
            if (await copyText(cmd)) toast("已复制终端恢复命令", "success");
          })}>
            <ListItemIcon><SquareTerminal size={16} /></ListItemIcon>复制终端恢复命令
          </MenuItem>
        )}
      </Menu>
      <HandoffMenu sessionId={sessionId} harness={harness ?? "claude"} anchor={handoffAnchor} onClose={() => setHandoffAnchor(null)} />
    </>
  );
}

/** Browsers repeat `dragover` at least about every half second while a drag is above an element. */
const DRAG_IDLE_MS = 700;

export function ChatView({ sessionId }: { sessionId: string }) {
  const summary = useApp((s) => s.sessions[sessionId]);
  const navigate = useUi((s) => s.navigate);
  // A session continued in another harness leads to the session that carries it on.
  const handedOffTo = summary?.handedOffTo;
  useEffect(() => {
    if (handedOffTo) navigate({ view: "session", sessionId: handedOffTo }, true);
  }, [handedOffTo, navigate]);
  const live = useApp((s) => s.live[sessionId]);
  const projects = useApp((s) => s.projects);
  const data = useSessions((s) => s.data[sessionId]);
  const loadOlder = useSessions((s) => s.loadOlder);
  const setContextProject = useUi((s) => s.setContextProject);
  const setScheduleSession = useUi((s) => s.setScheduleSession);
  /** Soonest next run of the session's enabled schedules (Infinity: none planned); undefined without any. */
  const nextWake = useSchedules((s) => {
    const mine = s.items.filter((x) => x.sessionId === sessionId && x.enabled);
    return mine.length ? Math.min(...mine.map((x) => x.next ?? Infinity)) : undefined;
  });
  const dropRef = useRef<((files: File[]) => void) | null>(null);
  const [dragging, setDragging] = useState(false);
  // The drop hint. A drag that ends elsewhere (dropped outside, left the window, cancelled) sends no reliable
  // "it is over" event here, but `dragover` keeps coming while it is above us: the hint goes when that stops.
  const dragTimer = useRef<number | undefined>(undefined);
  const endDrag = () => {
    window.clearTimeout(dragTimer.current);
    setDragging(false);
  };
  useEffect(() => () => window.clearTimeout(dragTimer.current), []);

  useEffect(() => socket.subscribeSession(sessionId), [sessionId]);

  const projectId = summary?.project ?? null;
  useEffect(() => {
    if (projectId) setContextProject(projectId);
  }, [projectId, setContextProject]);

  const state = data?.state ?? null;
  const harness = state?.harness ?? summary?.harness;
  const harnessName = useHarnessLabel(harness);
  const apiTag = useRouteTag(state?.api ?? summary?.api, harness);
  const keepAlive = Boolean(state?.keepAlive ?? summary?.keepAlive);
  const items = data?.items ?? [];
  const project = projects.find((p) => p.id === projectId);
  const turn = useMemo(() => turnStatus(items, state), [items, state]);
  const ctx = useMemo(
    () => ({
      sessionId, cwd: summary?.cwd ?? null, projectId, pendingTools: turn.pendingTools, settledTools: turn.settledTools,
      tasks: state?.tasks ?? [], hosted: Boolean(state?.hosted && state?.alive),
      harness: harness ?? null, capabilities: state?.capabilities ?? null,
    }),
    [sessionId, summary?.cwd, projectId, turn.pendingTools, turn.settledTools, state?.tasks, state?.hosted, state?.alive, harness, state?.capabilities],
  );
  const hostedHere = Boolean(state?.hosted && state.alive);
  const waitingSince = turn.waitingSince ? Date.parse(turn.waitingSince) : null;
  const background = turn.backgroundTasks;
  const waiting = Boolean(state?.permissions?.length);

  useEffect(() => {
    document.title = `${sessionTitle(summary)} · Riesz Manager`;
    return () => {
      document.title = "Riesz Manager";
    };
  }, [summary]);

  const onDrop = (e: DragEvent) => {
    e.preventDefault();
    endDrag();
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
          <TriangleAlert size={14} /> {harness ? harnessLabel(harness) : "Claude"} 进程异常退出（代码 {data.exited.code}）
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
      {state?.autoResume && (
        <div className="rz-auto-resume">
          <RotateCcw size={13} />
          <span className="rz-auto-resume-text">
            {state.autoResume.sentAt
              ? "正在尝试自动继续…"
              : state.autoResume.nextAt
                ? `${state.autoResume.label}，预计 ${clockTime(state.autoResume.nextAt)} 自动继续`
                : `${state.autoResume.label}，等待自动继续`}
          </span>
          <button
            type="button"
            className="rz-linklike"
            onClick={() => void api(`sessions/${sessionId}/cancel-auto-resume`, { body: {} }).catch((err: Error) => toast(err.message, "error"))}
          >
            取消
          </button>
        </div>
      )}
      {turn.terminated && !state?.autoResume && <div className="rz-terminated">已终止 · 上次运行没有完成</div>}
    </>
  );

  return (
    <ConversationShell
      context={ctx}
      onDragOver={(e) => {
        if (e.dataTransfer.types.includes("Files")) {
          e.preventDefault();
          setDragging(true);
          window.clearTimeout(dragTimer.current);
          dragTimer.current = window.setTimeout(() => setDragging(false), DRAG_IDLE_MS);
        }
      }}
      onDragLeave={(e) => {
        // Leaving a child for another fires this too: only when the pointer is outside the whole area.
        const r = e.currentTarget.getBoundingClientRect();
        if (e.clientX <= r.left || e.clientX >= r.right || e.clientY <= r.top || e.clientY >= r.bottom) endDrag();
      }}
      onDrop={onDrop}
      title={<EditableTitle sessionId={sessionId} title={sessionTitle(summary)} />}
      subtitle={
        <>
          <StatusDot live={live} size={7} />
          {harness && <span className={`rz-tag harness harness-${harness}`}>{harnessName}</span>}
          {apiTag && <span className="rz-tag harness">{apiTag}</span>}
          {project && <span className="clamp-1">{project.name}</span>}
          {summary?.gitBranch && <span className="dim mono clamp-1">⎇ {summary.gitBranch}</span>}
          {keepAlive && <span className="rz-tag keep" title="保持运行：空闲时不会被关闭"><Pin size={10} />常驻</span>}
          {nextWake !== undefined && (
            <button
              type="button"
              className="rz-tag keep rz-tag-btn"
              title={Number.isFinite(nextWake) ? `下次 ${clockTime(nextWake)}` : "定时唤醒"}
              onClick={() => setScheduleSession(sessionId)}
            >
              <AlarmClock size={10} />定时
            </button>
          )}
        </>
      }
      headActions={
        <>
          <NodesButton sessionId={sessionId} />
          <NotesButton sessionId={sessionId} />
          <SessionMenu sessionId={sessionId} state={state} cwd={summary?.cwd ?? null} title={sessionTitle(summary)} keepAlive={keepAlive}
            harness={harness} />
        </>
      }
      body={
        !data?.loaded ? (
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
        )
      }
      bottom={
        <>
          <NodeApprovalBanner sessionId={sessionId} />
          <QueueBar sessionId={sessionId} queue={state?.queue ?? []} />
          <Composer
            sessionId={sessionId}
            projectId={projectId}
            state={state}
            defaultModel={summary?.model}
            defaultMode={summary?.permissionMode}
            harness={harness}
            api={state?.api ?? summary?.api}
            autoFocus
            dropRef={dropRef}
          />
        </>
      }
      overlay={dragging && <div className="rz-dropzone">松开以添加附件</div>}
    />
  );
}
