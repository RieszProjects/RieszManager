/** Quick phrases the doll can put into the composer; stored on the server, shared by all devices. */
import { create } from "zustand";
import { api } from "@/api/client";

export interface Phrase {
  id: number;
  text: string;
}

interface PhraseStore {
  phrases: Phrase[];
  loaded: boolean;
  load: () => Promise<void>;
  save: (texts: string[]) => Promise<void>;
}

export const usePhrases = create<PhraseStore>((set) => ({
  phrases: [],
  loaded: false,
  load: async () => {
    const res = await api<{ phrases: Phrase[] }>("phrases");
    set({ phrases: res.phrases, loaded: true });
  },
  save: async (texts) => {
    const res = await api<{ phrases: Phrase[] }>("phrases", { body: { phrases: texts } });
    set({ phrases: res.phrases, loaded: true });
  },
}));

/** First line of a phrase, for buttons and lists. */
export function phraseLabel(text: string): string {
  const line = text.split("\n").find((l) => l.trim()) ?? text;
  return line.trim();
}
