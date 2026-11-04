/** Per-device preferences of the doll widget (stored in localStorage). */
import { create } from "zustand";
import { storage } from "@/lib/storage";
import type { Voice } from "./sound";

export type DollSize = "s" | "m" | "l";
export type DollSide = "left" | "right";

export interface DollPrefs {
  enabled: boolean;
  sound: boolean;
  /** Which squeak plays when she is squeezed (see sound.ts). */
  voice: Voice;
  size: DollSize;
  /** Corner (desktop) or edge (phone) the doll sits at. */
  side: DollSide;
  /** Phones: vertical position of the doll's centre, as a fraction of the viewport height. */
  y: number;
}

const KEY = "doll.v1";
const DEFAULTS: DollPrefs = { enabled: false, sound: true, voice: "classic", size: "m", side: "right", y: 0.58 };

interface DollPrefsStore extends DollPrefs {
  update: (patch: Partial<DollPrefs>) => void;
}

export const useDollPrefs = create<DollPrefsStore>((set, get) => ({
  ...DEFAULTS,
  ...storage.get<Partial<DollPrefs>>(KEY, {}),
  update: (patch) => {
    set(patch);
    const { enabled, sound, voice, size, side, y } = { ...get(), ...patch };
    storage.set(KEY, { enabled, sound, voice, size, side, y });
  },
}));
