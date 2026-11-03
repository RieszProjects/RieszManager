import { LoaderCircle } from "lucide-react";
import { useEffect } from "react";
import { setUnauthorizedHandler } from "@/api/client";
import { socket } from "@/api/socket";
import { Mascot } from "@/components/Mascot";
import { Toaster } from "@/components/Toaster";
import { UpdateBanner } from "@/components/UpdateBanner";
import { LoginPage } from "@/features/auth/LoginPage";
import { ChatView } from "@/features/chat/ChatView";
import { FileViewer } from "@/features/files/FileViewer";
import { HomeView, ProjectView } from "@/features/home/HomeView";
import { AppShell } from "@/features/layout/AppShell";
import { NewSessionDialog } from "@/features/sessions/NewSessionDialog";
import { RenameDialog } from "@/features/sessions/RenameDialog";
import { SettingsDialog } from "@/features/settings/SettingsDialog";
import { useApp } from "@/store/app";
import { useAuth } from "@/store/auth";
import { useOrganize } from "@/store/organize";
import { useSessions } from "@/store/sessions";
import { useUi } from "@/store/ui";

function useLiveData(enabled: boolean) {
  useEffect(() => {
    if (!enabled) return;
    const app = useApp.getState();
    void app.loadConfig().catch(() => undefined);
    void app.loadProjects().catch(() => undefined);
    void app.loadSessions().catch(() => undefined);
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
        void useApp.getState().loadSessions().catch(() => undefined);
        void useApp.getState().loadProjects().catch(() => undefined);
      }
    });
    socket.start();
    socket.subscribeGlobal();
    const timer = window.setInterval(() => void useApp.getState().loadProjects().catch(() => undefined), 60000);
    return () => {
      offEvent();
      offState();
      socket.stop();
      window.clearInterval(timer);
    };
  }, [enabled]);
}

function Router() {
  const route = useUi((s) => s.route);
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
      <SettingsDialog />
      <NewSessionDialog />
      <RenameDialog />
      <Toaster />
      <UpdateBanner />
    </>
  );
}
