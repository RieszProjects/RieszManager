/** UI state: routing, panels, dialogs and toasts. */
import { create } from "zustand";
import { takeOverlayEntry } from "@/lib/backStack";
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

/** Images shown in the full-screen viewer: all images of one message, starting at `index`. */
export interface ImageGallery {
  urls: string[];
  index: number;
}

export interface FileTarget {
  project: string;
  path: string;
}

export interface SubagentTarget {
  sessionId: string;
  /** The Agent tool call that started the subagent. */
  toolUseId: string;
  title: string;
  cwd: string | null;
  projectId: string | null;
}

interface UiStore {
  route: Route;
  /** Project shown in the right panel (follows the current session/project). */
  contextProject: string | null;
  leftOpen: boolean;
  /** Right panel shown as an overlay (phones and medium widths); wide screens always dock it. */
  rightOverlay: boolean;
  desktopLeftCollapsed: boolean;
  rightTab: "sessions" | "files";
  sidebarTab: "recent" | "projects";
  file: FileTarget | null;
  /** The subagent whose conversation is open (see SubagentViewer). */
  subagent: SubagentTarget | null;
  /** Session whose scheduled wake-ups are being managed (see ScheduleDialog). */
  scheduleSession: string | null;
  gallery: ImageGallery | null;
  settingsOpen: boolean;
  newSessionOpen: boolean;
  newProjectOpen: boolean;
  renameTarget: { sessionId: string; title: string } | null;
  /** Show archived sessions and projects in lists (per device). */
  showArchived: boolean;
  toasts: Toast[];

  navigate: (route: Route, replace?: boolean) => void;
  syncFromLocation: () => void;
  setContextProject: (id: string | null) => void;
  setLeftOpen: (open: boolean) => void;
  setRightOverlay: (open: boolean) => void;
  toggleDesktopLeft: () => void;
  setRightTab: (tab: UiStore["rightTab"]) => void;
  setSidebarTab: (tab: UiStore["sidebarTab"]) => void;
  openFile: (target: FileTarget | null) => void;
  openSubagent: (target: SubagentTarget | null) => void;
  setScheduleSession: (sessionId: string | null) => void;
  openGallery: (gallery: ImageGallery | null) => void;
  setSettingsOpen: (open: boolean) => void;
  setNewSessionOpen: (open: boolean) => void;
  setNewProjectOpen: (open: boolean) => void;
  setRenameTarget: (target: UiStore["renameTarget"]) => void;
  setShowArchived: (show: boolean) => void;
  toast: (message: string, severity?: Toast["severity"]) => void;
  dismissToast: (id: number) => void;
}

let toastId = 0;

export const useUi = create<UiStore>((set) => ({
  route: parseRoute(window.location.pathname),
  contextProject: null,
  leftOpen: false,
  rightOverlay: false,
  desktopLeftCollapsed: storage.get("leftCollapsed", false),
  rightTab: "files",
  sidebarTab: storage.get("sidebarTab.v2", "projects"),
  file: null,
  subagent: null,
  scheduleSession: null,
  gallery: null,
  settingsOpen: false,
  newSessionOpen: false,
  newProjectOpen: false,
  renameTarget: null,
  showArchived: storage.get("showArchived", false),
  toasts: [],

  navigate: (route, replace = false) => {
    const path = routePath(route);
    if (path !== window.location.pathname) {
      if (replace || takeOverlayEntry()) window.history.replaceState(null, "", path);
      else window.history.pushState(null, "", path);
    }
    set({ route, leftOpen: false, rightOverlay: false });
  },
  // Back that only closed an overlay leaves the URL unchanged: keep the same route object.
  syncFromLocation: () => set((s) => (routePath(s.route) === window.location.pathname ? {} : { route: parseRoute(window.location.pathname) })),
  setContextProject: (contextProject) => set({ contextProject }),
  setLeftOpen: (leftOpen) => set({ leftOpen }),
  setRightOverlay: (rightOverlay) => set({ rightOverlay }),
  toggleDesktopLeft: () =>
    set((s) => {
      storage.set("leftCollapsed", !s.desktopLeftCollapsed);
      return { desktopLeftCollapsed: !s.desktopLeftCollapsed };
    }),
  setRightTab: (rightTab) => set({ rightTab }),
  setSidebarTab: (sidebarTab) => {
    storage.set("sidebarTab.v2", sidebarTab);
    set({ sidebarTab });
  },
  openFile: (file) => set({ file }),
  openSubagent: (subagent) => set({ subagent }),
  setScheduleSession: (scheduleSession) => set({ scheduleSession }),
  openGallery: (gallery) => set({ gallery }),
  setSettingsOpen: (settingsOpen) => set({ settingsOpen }),
  setNewSessionOpen: (newSessionOpen) => set({ newSessionOpen }),
  setNewProjectOpen: (newProjectOpen) => set({ newProjectOpen }),
  setRenameTarget: (renameTarget) => set({ renameTarget }),
  setShowArchived: (showArchived) => {
    storage.set("showArchived", showArchived);
    set({ showArchived });
  },
  toast: (message, severity = "info") =>
    set((s) => ({ toasts: [...s.toasts, { id: ++toastId, message, severity }] })),
  dismissToast: (id) => set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) })),
}));

export const toast = (message: string, severity: Toast["severity"] = "info") => useUi.getState().toast(message, severity);
