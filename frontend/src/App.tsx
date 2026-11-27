import { LoaderCircle } from "lucide-react";
import { useEffect } from "react";
import { setUnauthorizedHandler } from "@/api/client";
import { socket } from "@/api/socket";
import { Mascot } from "@/components/Mascot";
import { Toaster } from "@/components/Toaster";
import { UpdateBanner } from "@/components/UpdateBanner";
import { LoginPage } from "@/features/auth/LoginPage";
import { ChatView } from "@/features/chat/ChatView";
import { DollSlot } from "@/features/doll/DollSlot";
import { FileViewer } from "@/features/files/FileViewer";
import { ImageViewer } from "@/components/ImageViewer";
import { HomeView, ProjectView } from "@/features/home/HomeView";
import { AppShell } from "@/features/layout/AppShell";
import { NewProjectDialog } from "@/features/sessions/NewProjectDialog";
import { SubagentViewer } from "@/features/chat/SubagentViewer";
import { NotesWindow } from "@/features/notes/NotesWindow";
import { startNotes, useNotes } from "@/features/notes/store";
import { NodesDialog } from "@/features/nodes/NodesDialog";
import { startNodes, useNodes } from "@/features/nodes/store";
import { ScheduleDialog } from "@/features/schedules/ScheduleDialog";
import { startSchedules, useSchedules } from "@/features/schedules/store";
import { NewSessionDialog } from "@/features/sessions/NewSessionDialog";
import { RenameDialog } from "@/features/sessions/RenameDialog";
import { ServerBrowser } from "@/features/browser/ServerBrowser";
import { SettingsDialog } from "@/features/settings/SettingsDialog";
import { preloadHarnesses } from "@/lib/harnesses";
import { setupPresence } from "@/lib/presence";
import { syncPush } from "@/lib/push";
import { useApp } from "@/store/app";
import { useAuth } from "@/store/auth";
import { useOrganize } from "@/store/organize";
import { useSessions } from "@/store/sessions";
import { useUi } from "@/store/ui";

function useLiveData(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const app = useApp.getState();
    let cancelled = false;
    let configRetryTimer: number | undefined;
    // Each (re)start of the retries is a new generation; an older chain still awaiting a request stops there.
    let configGen = 0;
    const ensureConfig = async (delay = 2000, gen = ++configGen) => {
      if (cancelled || gen !== configGen) return;
      let retry: boolean;
      try {
        await useApp.getState().loadConfig();
        retry = useApp.getState().config?.runner === false;
      } catch {
        retry = true;
      }
      if (retry && !cancelled && gen === configGen) {
        configRetryTimer = window.setTimeout(() => void ensureConfig(Math.min(delay * 2, 15000), gen), delay);
      }
    };
    void ensureConfig();
    void app.loadProjects().catch(() => undefined);
    void app.loadSessions().catch(() => undefined);
    preloadHarnesses();
    void useOrganize.getState().load().catch(() => undefined);
    const offEvent = socket.onEvent((event) => {
      useApp.getState().handleEvent(event);
      useSessions.getState().handleEvent(event);
    });
    let wasClosed = false;
    const offState = socket.onState((state) => {
      useApp.getState().setSocketState(state);
      if (state === "closed") wasClosed = true;
      if (state === "open" && wasClosed) {
        // Catch up on anything missed while disconnected.
        window.clearTimeout(configRetryTimer);
        void ensureConfig();
        void useApp.getState().loadSessions().catch(() => undefined);
        void useApp.getState().loadProjects().catch(() => undefined);
        void useSchedules.getState().load().catch(() => undefined);
        void useNotes.getState().load().catch(() => undefined);
        void useNodes.getState().load().catch(() => undefined);
      }
    });
    socket.start();
    socket.subscribeGlobal();
    const stopPresence = setupPresence();
    void syncPush();
    startSchedules();
    startNotes();
    startNodes();
    const timer = window.setInterval(() => void useApp.getState().loadProjects().catch(() => undefined), 60000);
    return () => {
      cancelled = true;
      window.clearTimeout(configRetryTimer);
      offEvent();
      offState();
      stopPresence();
      socket.stop();
      window.clearInterval(timer);
    };
  }, [enabled]);
}

function Router() {
  const route = useUi((s) => s.route);
  const subagent = useUi((s) => s.subagent);
  if (subagent) {
    return <SubagentViewer key={`${subagent.sessionId}:${subagent.agentId ?? subagent.toolUseId ?? ""}`} target={subagent} />;
  }
  if (route.view === "session") return <ChatView key={route.sessionId} sessionId={route.sessionId} />;
  if (route.view === "project") return <ProjectView key={route.projectId} projectId={route.projectId} />;
  return <HomeView />;
}

export function App() {
  const status = useAuth((s) => s.status);
  const refresh = useAuth((s) => s.refresh);
  const syncFromLocation = useUi((s) => s.syncFromLocation);

  useEffect(() => {
    void refresh();
    setUnauthorizedHandler(() => useAuth.getState().markLoggedOut());
    const onPop = () => syncFromLocation();
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [refresh, syncFromLocation]);

  useLiveData(status === "authenticated");

  if (status === "loading") {
    return (
      <div className="rz-boot">
        <Mascot pose="icon" size={72} />
        <LoaderCircle size={18} className="spin" />
      </div>
    );
  }
  if (status !== "authenticated") return <><LoginPage /><Toaster /><UpdateBanner /></>;
  return (
    <>
      <AppShell>
        <Router />
      </AppShell>
      <FileViewer />
      <ImageViewer />
      <ScheduleDialog />
      <ServerBrowser />
      <SettingsDialog />
      <NewSessionDialog />
      <NewProjectDialog />
      <RenameDialog />
      <NotesWindow />
      <NodesDialog />
      <DollSlot />
      <Toaster />
      <UpdateBanner />
    </>
  );
}
