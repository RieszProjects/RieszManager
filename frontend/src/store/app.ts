/** Global data: config, projects, session summaries and live process info. */
import { create } from "zustand";
import { api } from "@/api/client";
import type { AppConfig, LiveInfo, LiveProcess, Project, ServerEvent, SessionSummary } from "@/api/types";

interface AppStore {
  config: AppConfig | null;
  projects: Project[];
  projectsLoaded: boolean;
  sessions: Record<string, SessionSummary>;
  sessionsLoaded: boolean;
  live: Record<string, LiveInfo>;
  socketState: "connecting" | "open" | "closed";
  runnerOnline: boolean;

  loadConfig: () => Promise<void>;
  loadProjects: () => Promise<void>;
  loadSessions: () => Promise<void>;
  upsertSession: (s: SessionSummary) => void;
  renameSession: (sessionId: string, title: string) => Promise<void>;
  handleEvent: (event: ServerEvent) => void;
  setSocketState: (s: AppStore["socketState"]) => void;
}

function liveFromProcesses(list: LiveProcess[], previous: Record<string, LiveInfo>): Record<string, LiveInfo> {
  const out: Record<string, LiveInfo> = {};
  for (const p of list) {
    const prev = previous[p.sessionId];
    out[p.sessionId] = {
      hosted: p.hosted,
      busy: p.hosted && prev ? prev.busy : p.status === "busy",
      pid: p.pid,
      waiting: p.hosted ? prev?.waiting : false,
      kind: p.kind,
      entrypoint: p.entrypoint,
      status: p.status,
    };
  }
  // Hosted sessions announced via session_live but not yet registered on disk.
  for (const [sid, info] of Object.entries(previous)) {
    if (info.hosted && !out[sid]) out[sid] = info;
  }
  return out;
}

export const useApp = create<AppStore>((set, get) => ({
  config: null,
  projects: [],
  projectsLoaded: false,
  sessions: {},
  sessionsLoaded: false,
  live: {},
  socketState: "connecting",
  runnerOnline: true,

  loadConfig: async () => {
    const config = await api<AppConfig>("config");
    set({ config, runnerOnline: config.runner });
  },

  loadProjects: async () => {
    const res = await api<{ projects: Project[] }>("projects");
    set({ projects: res.projects, projectsLoaded: true, runnerOnline: true });
  },

  loadSessions: async () => {
    const res = await api<{ sessions: SessionSummary[] }>("sessions", { query: { limit: 1000 } });
    const sessions: Record<string, SessionSummary> = {};
    const live: Record<string, LiveInfo> = { ...get().live };
    for (const s of res.sessions) {
      sessions[s.sessionId] = s;
      if (s.live && !live[s.sessionId]) live[s.sessionId] = s.live;
    }
    set({ sessions, sessionsLoaded: true, live });
  },

  upsertSession: (s) => set((st) => ({ sessions: { ...st.sessions, [s.sessionId]: s } })),

  renameSession: async (sessionId, title) => {
    const res = await api<{ session: SessionSummary }>(`sessions/${sessionId}/rename`, { body: { title } });
    set((st) => ({ sessions: { ...st.sessions, [sessionId]: { ...st.sessions[sessionId], ...res.session } } }));
  },

  setSocketState: (socketState) => set({ socketState }),

  handleEvent: (event) => {
    switch (event.ev) {
      case "live":
        set((st) => ({ live: liveFromProcesses(event.live, st.live), runnerOnline: true }));
        break;
      case "session_live":
        set((st) => {
          const live = { ...st.live };
          if (event.live) live[event.sessionId] = event.live;
          else delete live[event.sessionId];
          return { live };
        });
        break;
      case "sessions":
        set((st) => {
          const sessions = { ...st.sessions };
          for (const s of event.sessions) sessions[s.sessionId] = { ...sessions[s.sessionId], ...s };
          for (const sid of event.removed) delete sessions[sid];
          return { sessions };
        });
        break;
      case "snapshot":
        if (event.session) get().upsertSession(event.session);
        break;
      case "runner_unavailable":
        set({ runnerOnline: false });
        break;
      default:
        break;
    }
  },
}));

export function sessionTitle(s: SessionSummary | undefined | null): string {
  if (!s) return "新会话";
  return s.title || s.firstPrompt || "未命名会话";
}
