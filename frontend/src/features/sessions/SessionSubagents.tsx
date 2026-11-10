/** A session's subagents, listed under its row in the session lists; click one to open its conversation.
 *
 * Shown: the running subagents and those that finished in the last 10 minutes ("active"); older ones are
 * folded into one line. The number on the row's button is how many are active, and the list opens by
 * itself while there are any, unless the user opened or closed it. */
import { Bot, ChevronRight, LoaderCircle } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { create } from "zustand";
import { api } from "@/api/client";
import type { LiveInfo, SessionSummary } from "@/api/types";
import { relativeTime } from "@/lib/format";
import { useNowSeconds, useTick } from "@/lib/useNow";
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

/** Newest first: running, finished in the last 10 minutes, older. */
function split(list: SubagentEntry[], live: LiveInfo | undefined, now: number) {
  const sorted = [...list].sort((a, b) => b.mtime - a.mtime);
  const running = sorted.filter((a) => isRunning(a, live));
  const rest = sorted.filter((a) => !isRunning(a, live));
  return {
    running,
    recent: rest.filter((a) => now - a.mtime < RECENT_SECONDS),
    older: rest.filter((a) => now - a.mtime >= RECENT_SECONDS),
  };
}

/** For a session row: whether it has subagents, how many are active, and whether the list under it is open. */
export function useSessionSubagents(session: SessionSummary, live: LiveInfo | undefined) {
  const sessionId = session.sessionId;
  const runningNow = live?.agents?.length ?? 0;
  const has = (session.subagents ?? 0) > 0 || runningNow > 0;
  const list = useSubagentLists((s) => s.lists[sessionId]);
  const load = useSubagentLists((s) => s.load);
  const runningKey = (live?.agents ?? []).join(",");
  // Load the list (and reload it when a subagent starts or finishes, or the session reports new activity).
  useEffect(() => {
    if (has) void load(sessionId).catch(() => undefined);
  }, [has, load, sessionId, session.subagents, session.subagentsActive, runningKey]);
  const active = useTick((now) => {
    if (!list) return runningNow;
    const g = split(list, live, now);
    return g.running.length + g.recent.length;
  });
  const choice = useUi((s) => s.subagentsOpen[sessionId]);
  const setOpen = useUi((s) => s.setSubagentsOpen);
  // A subagent starting is new activity: drop an earlier "closed" so the list shows it.
  const wasRunning = useRef(runningNow > 0);
  useEffect(() => {
    if (runningNow > 0 && !wasRunning.current && choice === false) setOpen(sessionId, undefined);
    wasRunning.current = runningNow > 0;
  }, [runningNow, choice, setOpen, sessionId]);
  return { has, active, running: runningNow, open: has && (choice ?? active > 0) };
}

/** The button at the bottom right of a session row: how many subagents are active; opens or closes the list. */
export function SubagentToggle({ sessionId, active, running, open }: { sessionId: string; active: number; running: number; open: boolean }) {
  const setOpen = useUi((s) => s.setSubagentsOpen);
  return (
    <button
      type="button"
      className={`rz-subagent-toggle${open ? " open" : ""}${running ? " running" : ""}`}
      aria-label={open ? "收起子代理" : "展开子代理"}
      aria-expanded={open}
      onClick={(e) => {
        e.stopPropagation();
        setOpen(sessionId, !open);
      }}
    >
      {running ? <LoaderCircle size={11} className="spin" /> : <Bot size={12} />}
      <span>{active}</span>
      <ChevronRight size={11} className="rz-subagent-chevron" />
    </button>
  );
}

export function SessionSubagents({ session, live }: { session: SessionSummary; live: LiveInfo | undefined }) {
  const sessionId = session.sessionId;
  const list = useSubagentLists((s) => s.lists[sessionId]);
  const openSubagent = useUi((s) => s.openSubagent);
  const current = useUi((s) => (s.subagent?.sessionId === sessionId ? s.subagent : null));
  const now = useNowSeconds();
  const [olderShown, setOlderShown] = useState(false);
  const { running, recent, older } = useMemo(() => split(list ?? [], live, now), [list, live, now]);

  if (!list) {
    return <div className="rz-subagent-rows"><div className="rz-subagent-row dim"><LoaderCircle size={12} className="spin" /></div></div>;
  }
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
      {older.length > 0 && (
        <button type="button" className="rz-subagent-row rz-subagent-rest" aria-expanded={olderShown} onClick={() => setOlderShown(!olderShown)}>
          <ChevronRight size={11} className={`rz-subagent-chevron${olderShown ? " open" : ""}`} />
          已完成 {older.length} 个
        </button>
      )}
      {olderShown && older.map((a) => row(a, false))}
    </div>
  );
}
