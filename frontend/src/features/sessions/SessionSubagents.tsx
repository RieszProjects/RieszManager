/** A session's subagents, listed under its row in the session lists; click one to open its conversation. */
import { Bot, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { create } from "zustand";
import { api } from "@/api/client";
import type { LiveInfo, SessionSummary } from "@/api/types";
import { relativeTime } from "@/lib/format";
import { useUi } from "@/store/ui";

export interface SubagentEntry {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
  background: boolean;
  mtime: number;
}

/** Finished subagents shown before "the rest". */
const RECENT = 5;
/** A terminal session's subagent counts as running while its transcript changed this recently. */
const ACTIVE_SECONDS = 20;

interface ListsStore {
  lists: Record<string, SubagentEntry[]>;
  load: (sessionId: string) => Promise<void>;
}

const inFlight = new Set<string>();

export const useSubagentLists = create<ListsStore>((set) => ({
  lists: {},
  load: async (sessionId) => {
    if (inFlight.has(sessionId)) return;
    inFlight.add(sessionId);
    try {
      const res = await api<{ subagents: SubagentEntry[] }>(`sessions/${sessionId}/subagents`);
      set((s) => ({ lists: { ...s.lists, [sessionId]: res.subagents } }));
    } finally {
      inFlight.delete(sessionId);
    }
  },
}));

function isRunning(a: SubagentEntry, live: LiveInfo | undefined): boolean {
  if (!live) return false;
  if (live.hosted) return Boolean(a.toolUseId && live.agents?.includes(a.toolUseId));
  return Boolean(live.busy) && Date.now() / 1000 - a.mtime < ACTIVE_SECONDS;
}

/** Whether the session's subagents are listed: the user's choice; a running subagent opens the list (and it stays open). */
export function useSubagentsShown(session: SessionSummary, live: LiveInfo | undefined): { has: boolean; open: boolean } {
  const choice = useUi((s) => s.subagentsOpen[session.sessionId]);
  const setOpen = useUi((s) => s.setSubagentsOpen);
  const running = (live?.agents?.length ?? 0) > 0;
  const has = (session.subagents ?? 0) > 0 || running;
  useEffect(() => {
    if (running && choice === undefined) setOpen(session.sessionId, true);
  }, [running, choice, setOpen, session.sessionId]);
  return { has, open: has && (choice ?? running) };
}

/** The toggle at the bottom right of a session row: number of subagents, opens or closes the list. */
export function SubagentToggle({ session, live, open }: { session: SessionSummary; live: LiveInfo | undefined; open: boolean }) {
  const setOpen = useUi((s) => s.setSubagentsOpen);
  const loaded = useSubagentLists((s) => s.lists[session.sessionId]?.length);
  const count = Math.max(session.subagents ?? 0, loaded ?? 0);
  const running = live?.agents?.length ?? 0;
  return (
    <button
      type="button"
      className={`rz-subagent-toggle${open ? " open" : ""}${running ? " running" : ""}`}
      aria-label={open ? "收起子代理" : "展开子代理"}
      aria-expanded={open}
      onClick={(e) => {
        e.stopPropagation();
        setOpen(session.sessionId, !open);
      }}
    >
      {running ? <LoaderCircle size={11} className="spin" /> : <Bot size={12} />}
      <span>{count || running}</span>
      <ChevronRight size={11} className="rz-subagent-chevron" />
    </button>
  );
}

export function SessionSubagents({ session, live }: { session: SessionSummary; live: LiveInfo | undefined }) {
  const sessionId = session.sessionId;
  const list = useSubagentLists((s) => s.lists[sessionId]);
  const load = useSubagentLists((s) => s.load);
  const openSubagent = useUi((s) => s.openSubagent);
  const current = useUi((s) => (s.subagent?.sessionId === sessionId ? s.subagent : null));
  const projectId = session.project;
  const [all, setAll] = useState(false);
  const runningKey = (live?.agents ?? []).join(",");

  // Reload when a subagent starts or finishes, or the session reports more of them.
  useEffect(() => {
    void load(sessionId).catch(() => undefined);
  }, [load, sessionId, session.subagents, runningKey, live?.busy]);

  const { running, finished } = useMemo(() => {
    const sorted = [...(list ?? [])].sort((a, b) => b.mtime - a.mtime);
    return { running: sorted.filter((a) => isRunning(a, live)), finished: sorted.filter((a) => !isRunning(a, live)) };
  }, [list, live]);

  if (!list) {
    return <div className="rz-subagent-rows"><div className="rz-subagent-row dim"><LoaderCircle size={12} className="spin" /></div></div>;
  }
  const shown = all ? finished : finished.slice(0, RECENT);
  const rest = finished.length - shown.length;
  const row = (a: SubagentEntry, active: boolean) => {
    const selected = current ? (current.agentId ? current.agentId === a.agentId : current.toolUseId === a.toolUseId) : false;
    return (
      <button
        key={a.agentId}
        type="button"
        className={`rz-subagent-row${active ? " running" : ""}${selected ? " selected" : ""}`}
        title={[a.description, a.agentType].filter(Boolean).join(" · ")}
        onClick={() => openSubagent({ sessionId, toolUseId: a.toolUseId, agentId: a.agentId, title: a.description,
          cwd: session.cwd, projectId })}
      >
        {active ? <LoaderCircle size={12} className="spin" /> : <Bot size={12} />}
        <span className="rz-subagent-row-title clamp-1">{a.description || a.agentType || "子代理"}</span>
        <span className="rz-subagent-row-time">{active ? "运行中" : relativeTime(a.mtime)}</span>
      </button>
    );
  };
  return (
    <div className="rz-subagent-rows">
      {running.map((a) => row(a, true))}
      {shown.map((a) => row(a, false))}
      {rest > 0 && (
        <button type="button" className="rz-subagent-row rz-subagent-rest" onClick={() => setAll(true)}>
          其余 {rest} 个
        </button>
      )}
      {all && finished.length > RECENT && (
        <button type="button" className="rz-subagent-row rz-subagent-rest" onClick={() => setAll(false)}>收起</button>
      )}
    </div>
  );
}
