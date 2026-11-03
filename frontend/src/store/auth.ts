import { create } from "zustand";
import { api } from "@/api/client";
import type { AuthState } from "@/api/types";

type Status = "loading" | "anonymous" | "totp" | "authenticated";

interface AuthStore {
  status: Status;
  username: string | null;
  totpEnabled: boolean;
  totpSetupRequired: boolean;
  refresh: () => Promise<void>;
  apply: (state: AuthState) => void;
  login: (username: string, password: string, remember: boolean) => Promise<void>;
  verify: (code: string, recovery: boolean) => Promise<void>;
  logout: () => Promise<void>;
  markLoggedOut: () => void;
}

export const useAuth = create<AuthStore>((set, get) => ({
  status: "loading",
  username: null,
  totpEnabled: false,
  totpSetupRequired: false,

  apply: (state) =>
    set({
      status: state.authenticated ? "authenticated" : state.pendingTotp ? "totp" : "anonymous",
      username: state.username,
      totpEnabled: Boolean(state.totpEnabled),
      totpSetupRequired: state.totpSetupRequired,
    }),

  refresh: async () => {
    try {
      get().apply(await api<AuthState>("auth/state"));
    } catch {
      set({ status: "anonymous" });
    }
  },

  login: async (username, password, remember) => {
    get().apply(await api<AuthState>("auth/login", { body: { username, password, remember } }));
  },

  verify: async (code, recovery) => {
    get().apply(await api<AuthState>("auth/totp", { body: { code, recovery } }));
  },

  logout: async () => {
    try {
      await api("auth/logout", { method: "POST" });
    } finally {
      set({ status: "anonymous", username: null });
      await get().refresh();
    }
  },

  markLoggedOut: () => {
    if (get().status === "authenticated") set({ status: "anonymous", username: null });
  },
}));
