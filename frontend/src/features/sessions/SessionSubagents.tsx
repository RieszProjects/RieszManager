/** A session's subagents, listed under its row in the session lists; click one to open its conversation.
 *
 * The list shows the running subagents and those that finished in the last few minutes; older ones are
 * folded into one line. It opens by itself while there is such activity, unless the user chose otherwise. */
import { Bot, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { create } from "zustand";
import { api } from "@/api/client";
import type { LiveInfo, SessionSummary } from "@/api/types";
import { relativeTime } from "@/lib/format";
import { useNowSeconds, useWithin } from "@/lib/useNow";
import { useUi } from "@/store/ui";

export interface SubagentEntry {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
  background: boolean;
  finished: boolean;
  mtime: number;
}

/** A finished subagent stays in the list this long (seconds). */
const RECENT_SECONDS = 600;

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

const isRunning = (a: SubagentEntry, live: LiveInfo | undefined) => Boolean(a.toolUseId && live?.agents?.includes(a.toolUseId));

/** Whether the row shows the subagent button, and whether the list under it is open. */
export function useSubagentsShown(session: SessionSummary, live: LiveInfo | undefined): { has: boolean; open: boolean } {
  const sessionId = session.sessionId;
  const choice = useUi((s) => s.subagentsOpen[sessionId]);
  const setOpen = useUi((s) => s.setSubagentsOpen);
  const running = (live?.agents?.length ?? 0) > 0;
  const recent = useWithin(session.subagentsActive, RECENT_SECONDS);
  const has = (session.subagents ?? 0) > 0 || running;
  // A subagent starting is new activity: drop an earlier "closed" so the list shows it.
  const wasRunning = useRef(running);
  useEffect(() => {
    if (running && !wasRunning.current && choice === false) setOpen(sessionId, undefined);
    wasRunning.current = running;
  }, [running, choice, setOpen, sessionId]);
  return { has, open: has && (choice ?? (running || recent)) };
}

/** The button at the bottom right of a session row: how many subagents are running; opens or closes the list. */
export function SubagentToggle({ session, live, open }: { session: SessionSummary; live: LiveInfo | undefined; open: boolean }) {
  const setOpen = useUi((s) => s.setSubagentsOpen);
  const running = live?.agents?.length ?? 0;
  return (
    <button
      type="button"
      className={`rz-subagent-toggle${open ? " open" : ""}${running ? " running" : ""}`}
      aria-label={open ? "收起子代理" : "展开子代理"}
      aria-expanded={open}
      title={`正在运行的子代理：${running}`}
      onClick={(e) => {
        e.stopPropagation();
        setOpen(session.sessionId, !open);
      }}
    >
      {running ? <LoaderCircle size={11} className="spin" /> : <Bot size={12} />}
      <span>{running}</span>
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
  const now = useNowSeconds();
  const [olderShown, setOlderShown] = useState<boolean | null>(null);
  const runningKey = (live?.agents ?? []).join(",");

  // Reload when a subagent starts or finishes, or the session reports new activity.
  useEffect(() => {
    void load(sessionId).catch(() => undefined);
  }, [load, sessionId, session.subagents, session.subagentsActive, runningKey]);

  const { running, recent, older } = useMemo(() => {
    const sorted = [...(list ?? [])].sort((a, b) => b.mtime - a.mtime);
    const run = sorted.filter((a) => isRunning(a, live));
    const rest = sorted.filter((a) => !isRunning(a, live));
    return {
      running: run,
      recent: rest.filter((a) => now - a.mtime < RECENT_SECONDS),
      older: rest.filter((a) => now - a.mtime >= RECENT_SECONDS),
    };
  }, [list, live, now]);

  if (!list) {
    return <div className="rz-subagent-rows"><div className="rz-subagent-row dim"><LoaderCircle size={12} className="spin" /></div></div>;
  }
  // Opened by hand with nothing active: show the finished ones right away.
  const showOlder = olderShown ?? (running.length + recent.length === 0);
  const row = (a: SubagentEntry, active: boolean) => {
    const selected = current ? (current.agentId ? current.agentId === a.agentId : current.toolUseId === a.toolUseId) : false;
    return (
      <button
        key={a.agentId}
        type="button"
        className={`rz-subagent-row${active ? " running" : ""}${selected ? " selected" : ""}`}
        title={[a.description, a.agentType].filter(Boolean).join(" · ")}
        onClick={() => openSubagent({ sessionId, toolUseId: a.toolUseId, agentId: a.agentId, title: a.description,
          cwd: session.cwd, projectId: session.project })}
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
      {recent.map((a) => row(a, false))}
      {older.length > 0 && !showOlder && (
        <button type="button" className="rz-subagent-row rz-subagent-rest" onClick={() => setOlderShown(true)}>
          已完成 {older.length} 个
        </button>
      )}
      {showOlder && older.map((a) => row(a, false))}
      {showOlder && older.length > 0 && running.length + recent.length > 0 && (
        <button type="button" className="rz-subagent-row rz-subagent-rest" onClick={() => setOlderShown(false)}>收起</button>
      )}
    </div>
  );
}
