/** Reader sticky notes persisted by the runner (runner/riesz_runner/notes.py). */
import { create } from "zustand";
import { api } from "@/api/client";
import { socket } from "@/api/socket";
import type { SessionSummary } from "@/api/types";
import { useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";

export type NoteColor = "amber" | "sage" | "slate" | "rose";

export interface Note {
  id: string;
  sessionId: string;
  title: string;
  content: string;
  color: NoteColor;
  createdAt: number;
  updatedAt: number;
}

export interface NoteInput {
  sessionId?: string;
  title?: string;
  content: string;
  color?: NoteColor;
  id?: string;
}

/** Session IDs connected to `sessionId` via handoffs, so notes stay with a continued session. */
export function sessionChain(sessions: Record<string, SessionSummary>, sessionId: string | null): Set<string> {
  const out = new Set<string>();
  if (!sessionId) return out;
  const queue = [sessionId];
  while (queue.length && out.size < 20) {
    const cur = queue.pop()!;
    if (out.has(cur)) continue;
    out.add(cur);
    const s = sessions[cur];
    if (s?.handedOffTo && !out.has(s.handedOffTo)) queue.push(s.handedOffTo);
    for (const other of Object.values(sessions)) {
      if (other.handedOffTo === cur && !out.has(other.sessionId)) queue.push(other.sessionId);
    }
  }
  return out;
}

export function notesForSession(items: Note[], sessions: Record<string, SessionSummary>, sessionId: string | null): Note[] {
  if (!sessionId) return [];
  const chain = sessionChain(sessions, sessionId);
  return items.filter((n) => chain.has(n.sessionId));
}

interface NotesStore {
  items: Note[];
  loaded: boolean;
  open: boolean;
  minimized: boolean;
  focusNoteId: string | null;
  pulse: number;

  load: () => Promise<void>;
  setOpen: (open: boolean) => void;
  toggleOpen: () => void;
  openNote: (noteId?: string | null) => void;
  clearFocus: () => void;
  setMinimized: (minimized: boolean) => void;
  create: (input: NoteInput) => Promise<Note>;
  update: (id: string, input: Partial<Pick<Note, "title" | "content" | "color">>) => Promise<void>;
  remove: (id: string) => Promise<void>;
  clear: (sessionId?: string) => Promise<void>;
}

export const useNotes = create<NotesStore>((set, get) => ({
  items: [],
  loaded: false,
  open: false,
  minimized: false,
  focusNoteId: null,
  pulse: 0,

  load: async () => {
    const res = await api<{ notes: Note[] }>("notes");
    set({ items: res.notes ?? [], loaded: true });
  },
  setOpen: (open) => set((s) => ({ open, minimized: open ? false : s.minimized })),
  toggleOpen: () =>
    set((s) => {
      if (s.open && !s.minimized) return { open: false };
      return { open: true, minimized: false };
    }),
  openNote: (noteId) =>
    set(() => ({
      open: true,
      minimized: false,
      focusNoteId: noteId ?? null,
    })),
  clearFocus: () => set({ focusNoteId: null }),
  setMinimized: (minimized) => set({ minimized }),
  create: async (input) => {
    const res = await api<{ note: Note }>("notes", { body: input });
    await get().load();
    return res.note;
  },
  update: async (id, input) => {
    await api(`notes/${id}/update`, { body: input });
    await get().load();
  },
  remove: async (id) => {
    set((s) => ({ items: s.items.filter((n) => n.id !== id) }));
    await api(`notes/${id}/delete`, { body: {} });
    await get().load();
  },
  clear: async (sessionId) => {
    await api("notes/clear", { body: sessionId ? { sessionId } : {} });
    await get().load();
  },
}));

let started = false;

/** Load sticky notes and keep them live over the global WebSocket. */
export function startNotes(): void {
  if (started) return;
  started = true;
  const load = () => void useNotes.getState().load().catch(() => undefined);
  load();
  socket.onEvent((raw) => {
    const event = raw as { ev?: string; action?: string; note?: Note };
    if (event.ev !== "notes") return;
    load();
    if (event.action === "create" && event.note) {
      const route = useUi.getState().route;
      const activeSid = route.view === "session" ? route.sessionId : null;
      if (!activeSid) return;
      const chain = sessionChain(useApp.getState().sessions, activeSid);
      if (!chain.has(event.note.sessionId)) return;
      const store = useNotes.getState();
      useNotes.setState({ pulse: Date.now() });
      if (!store.open || store.minimized) {
        const label = event.note.title || event.note.content.split("\n")[0].slice(0, 28);
        toast(`新便签：${label}`, "info");
      }
    }
  });
}
