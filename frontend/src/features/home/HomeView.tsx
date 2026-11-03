/** Landing view (nothing selected) and the per-project "new session" view. */
import { ChevronRight } from "lucide-react";
import { useEffect, useMemo } from "react";
import { Mascot } from "@/components/Mascot";
import { useOrganizeView } from "@/lib/organize";
import { useIsMobile } from "@/lib/useIsMobile";
import { useApp } from "@/store/app";
import { useUi } from "@/store/ui";
import { SessionRow } from "../sessions/SessionRow";
import { Composer } from "../composer/Composer";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "这么晚了还不睡吗";
  if (h < 11) return "早上好";
  if (h < 14) return "中午好";
  if (h < 18) return "下午好";
  return "晚上好";
}

const HOME_RECENT = 3;

/** Phones have no sidebar on screen: offer the latest few sessions on the landing view. */
function HomeRecent() {
  const sessions = useApp((s) => s.sessions);
  const setLeftOpen = useUi((s) => s.setLeftOpen);
  const setSidebarTab = useUi((s) => s.setSidebarTab);
  const view = useOrganizeView();
  const recent = useMemo(
    () => Object.values(sessions).filter((s) => view.sessionVisible(s)).sort((a, b) => b.mtime - a.mtime).slice(0, HOME_RECENT),
    [sessions, view],
  );
  if (!recent.length) return null;
  return (
    <section className="rz-home-recent">
      <div className="rz-home-recent-head">
        <span className="rz-side-label">最近会话</span>
        <button
          type="button"
          className="rz-home-recent-all"
          onClick={() => {
            setSidebarTab("recent");
            setLeftOpen(true);
          }}
        >
          全部 <ChevronRight size={14} />
        </button>
      </div>
      {recent.map((s) => <SessionRow key={s.sessionId} session={s} showProject />)}
    </section>
  );
}

export function HomeView() {
  const setContextProject = useUi((s) => s.setContextProject);
  const isMobile = useIsMobile();
  useEffect(() => setContextProject(null), [setContextProject]);

  return (
    <div className="rz-home">
      {isMobile && (
        <header className="rz-chat-head plain">
          <MobileHeaderButtons side="left" />
          <div className="rz-spacer" />
        </header>
      )}
      <div className="rz-home-stage">
        <Mascot pose="sleepy" className="rz-home-mascot" />
        <h1 className="rz-home-title rz-home-greeting">{greeting()}</h1>
      </div>
      {isMobile && <HomeRecent />}
    </div>
  );
}

export function ProjectView({ projectId }: { projectId: string }) {
  const project = useApp((s) => s.projects.find((p) => p.id === projectId));
  const projectsLoaded = useApp((s) => s.projectsLoaded);
  const navigate = useUi((s) => s.navigate);
  const setContextProject = useUi((s) => s.setContextProject);
  const isMobile = useIsMobile();
  useEffect(() => setContextProject(projectId), [projectId, setContextProject]);

  return (
    <div className="rz-chat">
      <header className="rz-chat-head">
        {isMobile && <MobileHeaderButtons side="left" />}
        <div className="rz-chat-title">
          <div className="clamp-1 rz-chat-title-text">{project?.name ?? (projectsLoaded ? "未知项目" : "")}</div>
          <div className="rz-chat-sub dim mono clamp-1">{project?.path}</div>
        </div>
        {isMobile ? <MobileHeaderButtons side="right" /> : <RightPanelButton />}
      </header>
      <div className="rz-chat-row">
      <div className="rz-chat-col">
      <div className="rz-chat-body">
        <div className="rz-project-empty">
          <Mascot pose="working" size={150} />
          <h2 className="rz-home-title small">在 {project?.name ?? "此项目"} 中开始新会话</h2>
          <p className="dim">工作目录：<span className="mono">{project?.path}</span></p>
        </div>
      </div>
      <div className="rz-chat-bottom">
        <Composer
          sessionId={null}
          projectId={projectId}
          state={null}
          autoFocus
          placeholder="描述你想做的事，/ 打开命令，@ 引用文件"
          onCreated={(sid) => navigate({ view: "session", sessionId: sid })}
        />
      </div>
      </div>
      <RightDock />
      </div>
    </div>
  );
}
