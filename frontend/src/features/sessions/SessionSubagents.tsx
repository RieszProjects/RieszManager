/** A session's subagents, listed under its row in the session lists; click one to open its conversation.
 *
 * Shown: the running subagents and those that finished in the last 10 minutes ("active"); older ones are
 * folded into one line. The number on the row's button is how many are active, and the list opens by
 * itself while there are any (or when one of its subagents is currently selected), unless the user closed it. */
import { Bot, ChevronRight, Globe, LoaderCircle, Workflow } from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { create } from "zustand";
import { api } from "@/api/client";
import type { LiveInfo, SessionSummary } from "@/api/types";
import { relativeTime } from "@/lib/format";
import { useNowSeconds, useTick } from "@/lib/useNow";
import { useApp } from "@/store/app";
import { type Route, type SubagentTarget, useUi } from "@/store/ui";

export interface SubagentEntry {
  agentId: string;
  toolUseId: string | null;
  description: string;
  agentType: string;
  background: boolean;
  finished: boolean;
  mtime: number;
  /** A session started as this one's subagent (a browser task): opened as a session. */
  sessionId?: string;
  running?: boolean;
  /** How it was started: the harness's own subagent (absent), Riesz Manager's subagent_start, or a browser task. */
  via?: "riesz" | "browser";
  harness?: string;
}

/** The icon for how a subagent was started. */
function ViaIcon({ a }: { a: SubagentEntry }) {
  if (a.via === "browser") return <Globe size={12} />;
  if (a.via === "riesz") return <Workflow size={12} />;
  return <Bot size={12} />;
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

const isRunning = (a: SubagentEntry, live: LiveInfo | undefined) =>
  a.running ?? Boolean(a.toolUseId && live?.agents?.includes(a.toolUseId));

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

/** Whether `entry` is the currently viewed subagent. */
function isEntrySelected(a: SubagentEntry, current: SubagentTarget | null, route: Route): boolean {
  if (a.sessionId) return !current && route.view === "session" && route.sessionId === a.sessionId;
  return Boolean(current && (current.agentId ? current.agentId === a.agentId : current.toolUseId === a.toolUseId));
}

/** Walk handoff chain to see if `childSid` belongs to `parentSid`. */
function isChildOf(childSid: string, parentSid: string, sessions: Record<string, SessionSummary>): boolean {
  const seen = new Set<string>();
  let cur: string | null | undefined = childSid;
  while (cur && !seen.has(cur)) {
    seen.add(cur);
    const s: SessionSummary | undefined = sessions[cur];
    if (!s) break;
    if (s.subagentOf === parentSid) return true;
    cur = s.handoffFrom;
  }
  return false;
}

/** Key identifying the currently selected subagent if it belongs to `parentSessionId`, else null. */
function useSelectedSubagentKey(parentSessionId: string, list: SubagentEntry[] | undefined): string | null {
  const subagent = useUi((s) => s.subagent);
  const route = useUi((s) => s.route);
  const sessions = useApp((s) => s.sessions);
  if (subagent) {
    if (subagent.sessionId !== parentSessionId) return null;
    return `b:${subagent.agentId ?? subagent.toolUseId ?? ""}`;
  }
  if (route.view !== "session") return null;
  if (isChildOf(route.sessionId, parentSessionId, sessions) || Boolean(list?.some((a) => a.sessionId === route.sessionId))) {
    return `r:${route.sessionId}`;
  }
  return null;
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
  const selectedKey = useSelectedSubagentKey(sessionId, list);
  const hasSelected = Boolean(selectedKey);
  const choice = useUi((s) => s.subagentsOpen[sessionId]);
  const setOpen = useUi((s) => s.setSubagentsOpen);
  // A subagent starting is new activity: drop an earlier "closed" so the list shows it.
  const wasRunning = useRef(runningNow > 0);
  useEffect(() => {
    if (runningNow > 0 && !wasRunning.current && choice === false) setOpen(sessionId, undefined);
    wasRunning.current = runningNow > 0;
  }, [runningNow, choice, setOpen, sessionId]);
  // Selecting a subagent of this session reveals its list once per selection change.
  const revealedKey = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedKey) {
      revealedKey.current = null;
      return;
    }
    if (revealedKey.current !== selectedKey) {
      revealedKey.current = selectedKey;
      if (choice !== true) setOpen(sessionId, true);
    }
  }, [selectedKey, choice, setOpen, sessionId]);
  return { has, active, running: runningNow, hasSelected, open: has && (choice ?? (active > 0 || hasSelected)) };
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
  const navigate = useUi((s) => s.navigate);
  const route = useUi((s) => s.route);
  const current = useUi((s) => (s.subagent?.sessionId === sessionId ? s.subagent : null));
  const olderChoice = useUi((s) => s.subagentsOlderOpen[sessionId]);
  const setOlderOpen = useUi((s) => s.setSubagentsOlderOpen);
  const setOpen = useUi((s) => s.setSubagentsOpen);
  const now = useNowSeconds();
  const { running, recent, older } = useMemo(() => split(list ?? [], live, now), [list, live, now]);
  const selectedKey = useSelectedSubagentKey(sessionId, list);
  const selectedInOlder = useMemo(() => older.some((a) => isEntrySelected(a, current, route)), [older, current, route]);
  const olderShown = olderChoice ?? selectedInOlder;

  // When a historical subagent inside "已完成 N 个" is selected, ensure that fold is expanded.
  const revealedOlderKey = useRef<string | null>(null);
  useEffect(() => {
    if (!selectedKey || !selectedInOlder) {
      if (!selectedKey) revealedOlderKey.current = null;
      return;
    }
    if (revealedOlderKey.current !== selectedKey) {
      revealedOlderKey.current = selectedKey;
      if (olderChoice !== true) setOlderOpen(sessionId, true);
    }
  }, [selectedKey, selectedInOlder, olderChoice, setOlderOpen, sessionId]);

  const selectedRef = useRef<HTMLButtonElement | null>(null);
  useEffect(() => {
    if (selectedKey && selectedRef.current) {
      selectedRef.current.scrollIntoView({ block: "nearest" });
    }
  }, [selectedKey, olderShown, list]);

  if (!list) {
    return <div className="rz-subagent-rows"><div className="rz-subagent-row dim"><LoaderCircle size={12} className="spin" /></div></div>;
  }
  const row = (a: SubagentEntry, active: boolean, inOlder = false) => {
    const selected = isEntrySelected(a, current, route);
    return (
      <button
        key={a.agentId}
        ref={selected ? selectedRef : undefined}
        type="button"
        className={`rz-subagent-row${active ? " running" : ""}${selected ? " selected" : ""}`}
        title={[a.description, a.agentType].filter(Boolean).join(" · ")}
        onClick={() => {
          setOpen(sessionId, true);
          if (inOlder) setOlderOpen(sessionId, true);
          if (a.sessionId) {
            navigate({ view: "session", sessionId: a.sessionId });
          } else {
            openSubagent({
              sessionId,
              toolUseId: a.toolUseId,
              agentId: a.agentId,
              title: a.description,
              cwd: session.cwd,
              projectId: session.project,
            });
          }
        }}
      >
        {active ? <LoaderCircle size={12} className="spin" /> : <ViaIcon a={a} />}
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
        <button
          type="button"
          className="rz-subagent-row rz-subagent-rest"
          aria-expanded={olderShown}
          onClick={() => setOlderOpen(sessionId, !olderShown)}
        >
          <ChevronRight size={11} className={`rz-subagent-chevron${olderShown ? " open" : ""}`} />
          已完成 {older.length} 个
        </button>
      )}
      {olderShown && older.map((a) => row(a, false, true))}
    </div>
  );
}
