/** Scheduled wake-ups kept by the runner (runner/riesz_runner/schedules.py). */
import { create } from "zustand";
import { api } from "@/api/client";
import { socket } from "@/api/socket";

export interface Schedule {
  id: string;
  sessionId: string;
  name: string;
  prompt: string;
  /** "HH:MM" in `tz`, on `days` (0 = Monday; empty = every day); or `every` minutes. */
  times: string[];
  days: number[];
  every: number | null;
  tz: string;
  /** Random delay, minutes. */
  jitter: number;
  catchUp: boolean;
  enabled: boolean;
  next: number | null;
  last: number | null;
  lastResult: string | null;
  rule: string;
}

export type ScheduleInput = Partial<Pick<Schedule, "name" | "prompt" | "times" | "days" | "every" | "tz" | "jitter" | "catchUp" | "enabled">>;

interface SchedulesStore {
  items: Schedule[];
  timezone: string;
  loaded: boolean;
  load: () => Promise<void>;
  create: (sessionId: string, input: ScheduleInput) => Promise<void>;
  update: (id: string, input: ScheduleInput) => Promise<void>;
  remove: (id: string) => Promise<void>;
  runNow: (id: string) => Promise<void>;
}

export const useSchedules = create<SchedulesStore>((set, get) => ({
  items: [],
  timezone: "",
  loaded: false,
  load: async () => {
    const res = await api<{ schedules: Schedule[]; timezone: string }>("schedules");
    set({ items: res.schedules, timezone: res.timezone, loaded: true });
  },
  create: async (sessionId, input) => {
    await api("schedules", { body: { sessionId, ...input } });
    await get().load();
  },
  update: async (id, input) => {
    await api(`schedules/${id}/update`, { body: input });
    await get().load();
  },
  remove: async (id) => {
    await api(`schedules/${id}/delete`, { body: {} });
    await get().load();
  },
  runNow: async (id) => {
    await api(`schedules/${id}/run`, { body: {} });
    await get().load();
  },
}));

let started = false;

/** Load the schedules and keep them current (the runner announces every change). */
export function startSchedules(): void {
  if (started) return;
  started = true;
  const load = () => void useSchedules.getState().load().catch(() => undefined);
  load();
  socket.onEvent((event) => {
    if ((event as { ev?: string }).ev === "schedules") load();
  });
}

export const DAY_LABELS = ["一", "二", "三", "四", "五", "六", "日"];
