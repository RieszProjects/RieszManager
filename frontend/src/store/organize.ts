/** Archive flags and manual order of sessions and projects (persisted in the backend). */
import { create } from "zustand";
import { api } from "@/api/client";
import { toast } from "@/store/ui";

export type ItemKind = "project" | "session";

interface OrganizeState {
  loaded: boolean;
  archived: Record<ItemKind, Set<string>>;
  positions: Record<ItemKind, Record<string, number>>;
  load: () => Promise<void>;
  setArchived: (kind: ItemKind, key: string, archived: boolean) => Promise<void>;
  setOrder: (kind: ItemKind, keys: string[]) => Promise<void>;
}

interface Payload {
  archived: Record<ItemKind, string[]>;
  positions: Record<ItemKind, Record<string, number>>;
}

export const useOrganize = create<OrganizeState>((set, get) => ({
  loaded: false,
  archived: { project: new Set(), session: new Set() },
  positions: { project: {}, session: {} },

  load: async () => {
    const res = await api<Payload>("organize");
    set({
      loaded: true,
      archived: { project: new Set(res.archived.project), session: new Set(res.archived.session) },
      positions: res.positions,
    });
  },

  setArchived: async (kind, key, archived) => {
    const previous = get().archived;
    const next = new Set(previous[kind]);
    if (archived) next.add(key);
    else next.delete(key);
    set({ archived: { ...previous, [kind]: next } });
    try {
      await api("organize/archive", { body: { kind, key, archived } });
    } catch (err) {
      set({ archived: previous });
      toast((err as Error).message, "error");
    }
  },

  setOrder: async (kind, keys) => {
    const previous = get().positions;
    const updated = { ...previous[kind] };
    keys.forEach((key, index) => {
      updated[key] = index;
    });
    set({ positions: { ...previous, [kind]: updated } });
    try {
      await api("organize/order", { body: { kind, keys } });
    } catch (err) {
      set({ positions: previous });
      toast((err as Error).message, "error");
    }
  },
}));
