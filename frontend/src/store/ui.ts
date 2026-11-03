/** UI state: routing, panels, dialogs and toasts. */
import { create } from "zustand";
import { storage } from "@/lib/storage";

export type Route =
  | { view: "home" }
  | { view: "project"; projectId: string }
  | { view: "session"; sessionId: string };

export function parseRoute(pathname: string): Route {
  const parts = pathname.split("/").filter(Boolean);
  if (parts[0] === "s" && parts[1]) return { view: "session", sessionId: parts[1] };
  if (parts[0] === "p" && parts[1]) return { view: "project", projectId: decodeURIComponent(parts[1]) };
  return { view: "home" };
}

export function routePath(route: Route): string {
  if (route.view === "session") return `/s/${route.sessionId}`;
  if (route.view === "project") return `/p/${encodeURIComponent(route.projectId)}`;
  return "/";
}

export interface Toast {
  id: number;
  message: string;
  severity: "info" | "success" | "error" | "warning";
}

export interface FileTarget {
  project: string;
  path: string;
}

interface UiStore {
  route: Route;
  /** Project shown in the right panel (follows the current session/project). */
  contextProject: string | null;
  leftOpen: boolean;
  rightOpen: boolean;
  /** Right panel shown as an overlay drawer (phones and medium widths). */
  rightOverlay: boolean;
  desktopLeftCollapsed: boolean;
  rightTab: "sessions" | "files";
  sidebarTab: "recent" | "projects";
  file: FileTarget | null;
  settingsOpen: boolean;
  newSessionOpen: boolean;
  toasts: Toast[];

  navigate: (route: Route, replace?: boolean) => void;
  syncFromLocation: () => void;
  setContextProject: (id: string | null) => void;
  setLeftOpen: (open: boolean) => void;
  setRightOpen: (open: boolean) => void;
  setRightOverlay: (open: boolean) => void;
  toggleDesktopLeft: () => void;
  setRightTab: (tab: UiStore["rightTab"]) => void;
  setSidebarTab: (tab: UiStore["sidebarTab"]) => void;
  openFile: (target: FileTarget | null) => void;
  setSettingsOpen: (open: boolean) => void;
  setNewSessionOpen: (open: boolean) => void;
  toast: (message: string, severity?: Toast["severity"]) => void;
  dismissToast: (id: number) => void;
}

let toastId = 0;

export const useUi = create<UiStore>((set) => ({
  route: parseRoute(window.location.pathname),
  contextProject: null,
  leftOpen: false,
  rightOpen: storage.get("rightOpen", true),
  rightOverlay: false,
  desktopLeftCollapsed: storage.get("leftCollapsed", false),
  rightTab: "sessions",
  sidebarTab: storage.get("sidebarTab", "recent"),
  file: null,
  settingsOpen: false,
  newSessionOpen: false,
  toasts: [],

  navigate: (route, replace = false) => {
    const path = routePath(route);
    if (path !== window.location.pathname) {
      if (replace) window.history.replaceState(null, "", path);
      else window.history.pushState(null, "", path);
    }
    set({ route, leftOpen: false, rightOverlay: false });
  },
  syncFromLocation: () => set({ route: parseRoute(window.location.pathname) }),
  setContextProject: (contextProject) => set({ contextProject }),
  setLeftOpen: (leftOpen) => set({ leftOpen }),
  setRightOpen: (rightOpen) => {
    storage.set("rightOpen", rightOpen);
    set({ rightOpen });
  },
  setRightOverlay: (rightOverlay) => set({ rightOverlay }),
  toggleDesktopLeft: () =>
    set((s) => {
      storage.set("leftCollapsed", !s.desktopLeftCollapsed);
      return { desktopLeftCollapsed: !s.desktopLeftCollapsed };
    }),
  setRightTab: (rightTab) => set({ rightTab }),
  setSidebarTab: (sidebarTab) => {
    storage.set("sidebarTab", sidebarTab);
    set({ sidebarTab });
  },
  openFile: (file) => set({ file }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
  setNewSessionOpen: (newSessionOpen) => set({ newSessionOpen }),
  toast: (message, severity = "info") =>
    set((s) => ({ toasts: [...s.toasts, { id: ++toastId, message, severity }] })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

export const toast = (message: string, severity: Toast["severity"] = "info") => useUi.getState().toast(message, severity);
