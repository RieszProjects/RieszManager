/** Sidebar project tree: each project expands to its sessions; clicking a project opens it. */
import { ChevronRight, Folder, FolderGit2 } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import type { Project, SessionSummary } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { storage } from "@/lib/storage";
import { sessionTitle, useApp } from "@/store/app";
import { useUi } from "@/store/ui";
import { SessionRow } from "./SessionRow";

const PAGE = 8;
const EXPANDED_KEY = "expandedProjects";

function matches(s: SessionSummary, q: string): boolean {
  const hay = `${sessionTitle(s)} ${s.firstPrompt ?? ""} ${s.lastPrompt ?? ""}`.toLowerCase();
  return hay.includes(q);
}

interface RowProps {
  project: Project;
  sessions: SessionSummary[];
  expanded: boolean;
  forceOpen: boolean;
  onToggle: (id: string) => void;
}

function ProjectNode({ project, sessions, expanded, forceOpen, onToggle }: RowProps) {
  const route = useUi((s) => s.route);
  const navigate = useUi((s) => s.navigate);
  const live = useApp((s) => s.live);
  const [limit, setLimit] = useState(PAGE);
  const open = expanded || forceOpen;
  const activeSession = route.view === "session" ? route.sessionId : null;
  const activeProject = route.view === "project" && route.projectId === project.id;
  const running = sessions.filter((s) => live[s.sessionId]);
  const runningInfo = running.some((s) => live[s.sessionId]?.waiting)
    ? { hosted: true, busy: true, pid: 0, waiting: true }
    : running.some((s) => live[s.sessionId]?.busy)
      ? { hosted: true, busy: true, pid: 0 }
      : running.length
        ? { hosted: true, busy: false, pid: 0 }
        : null;
  const Icon = project.isGit ? FolderGit2 : Folder;
  const label = project.external ? project.path.replace(/^\/home\/[^/]+/, "~") : project.name;

  return (
    <div className={`rz-tree-project${open ? " open" : ""}`}>
      <div className={`rz-project-row${activeProject ? " active" : ""}`} title={project.path}>
        <button type="button" className="rz-project-toggle" aria-label={open ? "收起" : "展开"} onClick={() => onToggle(project.id)}>
          <ChevronRight size={14} />
        </button>
        <button type="button" className="rz-project-open" onClick={() => navigate({ view: "project", projectId: project.id })}>
          <Icon size={15} />
          <span className="clamp-1 rz-project-name">{label}</span>
          <StatusDot live={runningInfo} size={6} />
          <span className="dim rz-project-count">{sessions.length || ""}</span>
        </button>
      </div>
      {open && (
        <div className="rz-project-sessions">
          {sessions.slice(0, limit).map((s) => (
            <SessionRow key={s.sessionId} session={s} active={s.sessionId === activeSession} />
          ))}
          {sessions.length > limit && (
            <button type="button" className="rz-side-more" onClick={() => setLimit(limit + 20)}>
              还有 {sessions.length - limit} 个会话
            </button>
          )}
          {sessions.length === 0 && <div className="rz-project-none">没有会话</div>}
        </div>
      )}
    </div>
  );
}

export function ProjectTree({ query }: { query: string }) {
  const projects = useApp((s) => s.projects);
  const sessions = useApp((s) => s.sessions);
  const contextProject = useUi((s) => s.contextProject);
  const route = useUi((s) => s.route);
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(storage.get<string[]>(EXPANDED_KEY, [])));
  const q = query.trim().toLowerCase();

  const toggle = useCallback((id: string) => {
    setExpanded((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      storage.set(EXPANDED_KEY, Array.from(next));
      return next;
    });
  }, []);

  // Reveal the project of the session being viewed.
  useEffect(() => {
    if (route.view !== "session" || !contextProject) return;
    setExpanded((cur) => {
      if (cur.has(contextProject)) return cur;
      const next = new Set(cur).add(contextProject);
      storage.set(EXPANDED_KEY, Array.from(next));
      return next;
    });
  }, [route, contextProject]);

  const grouped = useMemo(() => {
    const map = new Map<string, SessionSummary[]>();
    for (const s of Object.values(sessions)) {
      if (!s.project) continue;
      if (q && !matches(s, q)) continue;
      const list = map.get(s.project) ?? [];
      list.push(s);
      map.set(s.project, list);
    }
    for (const list of map.values()) list.sort((a, b) => b.mtime - a.mtime);
    return map;
  }, [sessions, q]);

  const visible = projects.filter((p) => !q || p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q) || grouped.has(p.id));
  const workspace = visible.filter((p) => !p.external);
  const external = visible.filter((p) => p.external);

  const render = (p: Project) => (
    <ProjectNode
      key={p.id}
      project={p}
      sessions={grouped.get(p.id) ?? []}
      expanded={expanded.has(p.id)}
      forceOpen={Boolean(q) && (grouped.get(p.id)?.length ?? 0) > 0}
      onToggle={toggle}
    />
  );

  return (
    <div className="rz-side-list">
      <section>
        <div className="rz-side-label">工作区</div>
        {workspace.map(render)}
        {workspace.length === 0 && <div className="rz-side-empty">{q ? "没有匹配的项目或会话" : "没有项目"}</div>}
      </section>
      {external.length > 0 && (
        <section>
          <div className="rz-side-label">其他目录</div>
          {external.map(render)}
        </section>
      )}
    </div>
  );
}
