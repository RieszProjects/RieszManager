/** Landing view (nothing selected) and the per-project "new session" view. */
import { FolderGit2 } from "lucide-react";
import { useEffect, useMemo } from "react";
import { Mascot } from "@/components/Mascot";
import { relativeTime } from "@/lib/format";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { useUi } from "@/store/ui";
import { Composer } from "../composer/Composer";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "这么晚了还不睡吗";
  if (h < 11) return "早上好";
  if (h < 14) return "中午好";
  if (h < 18) return "下午好";
  return "晚上好";
}

export function HomeView() {
  const sessions = useApp((s) => s.sessions);
  const projects = useApp((s) => s.projects);
  const navigate = useUi((s) => s.navigate);
  const setContextProject = useUi((s) => s.setContextProject);
  const isMobile = useIsMobile();
  const recent = useMemo(() => Object.values(sessions).sort((a, b) => b.mtime - a.mtime).slice(0, 6), [sessions]);
  const topProjects = useMemo(
    () => projects.filter((p) => !p.external).sort((a, b) => (b.lastActivity ?? 0) - (a.lastActivity ?? 0)).slice(0, 6),
    [projects],
  );
  useEffect(() => setContextProject(null), [setContextProject]);

  return (
    <div className="rz-home">
      {isMobile && (
        <header className="rz-chat-head plain">
          <MobileHeaderButtons side="left" />
          <div className="rz-spacer" />
        </header>
      )}
      <div className="rz-home-inner">
        <div className="rz-home-hero">
          <Mascot pose="sleepy" className="rz-home-mascot" />
          <div>
            <h1 className="rz-home-title">{greeting()}</h1>
            <p className="rz-home-sub">选一个项目开始新会话，或者继续之前的工作。</p>
          </div>
        </div>
        <div className="rz-home-grid">
          <section>
            <div className="rz-side-label">项目</div>
            {topProjects.map((p) => (
              <button key={p.id} type="button" className="rz-home-card" onClick={() => navigate({ view: "project", projectId: p.id })}>
                <FolderGit2 size={16} />
                <span className="clamp-1">{p.name}</span>
                <span className="dim">{p.sessionCount ? `${p.sessionCount} 个会话` : ""}</span>
              </button>
            ))}
          </section>
          <section>
            <div className="rz-side-label">最近会话</div>
            {recent.map((s) => (
              <button key={s.sessionId} type="button" className="rz-home-card" onClick={() => navigate({ view: "session", sessionId: s.sessionId })}>
                <span className="clamp-1">{sessionTitle(s)}</span>
                <span className="dim">{relativeTime(s.mtime)}</span>
              </button>
            ))}
            {recent.length === 0 && <div className="rz-side-empty">还没有会话</div>}
          </section>
        </div>
      </div>
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
        {isMobile && <MobileHeaderButtons side="right" />}
      </header>
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
  );
}
