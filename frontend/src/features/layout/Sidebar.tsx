/** Left sidebar: brand, new-session entry, search, recent sessions and projects. */
import { IconButton, InputBase, Tooltip } from "@mui/material";
import { Folder, FolderGit2, PanelLeftClose, Plus, Search, Settings, Wifi, WifiOff } from "lucide-react";
import { useMemo, useState } from "react";
import type { SessionSummary } from "@/api/types";
import { Mascot } from "@/components/Mascot";
import { StatusDot } from "@/components/StatusDot";
import { dateBucket, relativeTime, type DateBucket } from "@/lib/format";
import { sessionTitle, useApp } from "@/store/app";
import { useUi } from "@/store/ui";
import { SessionRow } from "../sessions/SessionRow";

const RECENT_LIMIT = 150;

function matches(s: SessionSummary, q: string): boolean {
  if (!q) return true;
  const hay = `${sessionTitle(s)} ${s.firstPrompt ?? ""} ${s.lastPrompt ?? ""} ${s.cwd ?? ""}`.toLowerCase();
  return hay.includes(q);
}

function RecentList({ query }: { query: string }) {
  const sessions = useApp((s) => s.sessions);
  const live = useApp((s) => s.live);
  const route = useUi((s) => s.route);
  const [limit, setLimit] = useState(RECENT_LIMIT);
  const activeId = route.view === "session" ? route.sessionId : null;
  const q = query.trim().toLowerCase();

  const { running, groups, total } = useMemo(() => {
    const all = Object.values(sessions).filter((s) => matches(s, q)).sort((a, b) => b.mtime - a.mtime);
    const runningList = all.filter((s) => live[s.sessionId]);
    const rest = all.filter((s) => !live[s.sessionId]).slice(0, limit);
    const grouped = new Map<DateBucket, SessionSummary[]>();
    for (const s of rest) {
      const bucket = dateBucket(s.mtime);
      grouped.set(bucket, [...(grouped.get(bucket) ?? []), s]);
    }
    return { running: runningList, groups: Array.from(grouped.entries()), total: all.length - runningList.length };
  }, [sessions, live, q, limit]);

  return (
    <div className="rz-side-list">
      {running.length > 0 && (
        <section>
          <div className="rz-side-label">运行中</div>
          {running.map((s) => <SessionRow key={s.sessionId} session={s} showProject active={s.sessionId === activeId} />)}
        </section>
      )}
      {groups.map(([bucket, list]) => (
        <section key={bucket}>
          <div className="rz-side-label">{bucket}</div>
          {list.map((s) => <SessionRow key={s.sessionId} session={s} showProject active={s.sessionId === activeId} />)}
        </section>
      ))}
      {total > limit && (
        <button type="button" className="rz-side-more" onClick={() => setLimit(limit + RECENT_LIMIT)}>显示更多</button>
      )}
      {total === 0 && running.length === 0 && <div className="rz-side-empty">{q ? "没有匹配的会话" : "还没有会话"}</div>}
    </div>
  );
}

function ProjectList({ query }: { query: string }) {
  const projects = useApp((s) => s.projects);
  const route = useUi((s) => s.route);
  const contextProject = useUi((s) => s.contextProject);
  const navigate = useUi((s) => s.navigate);
  const q = query.trim().toLowerCase();
  const list = projects.filter((p) => !q || p.name.toLowerCase().includes(q) || p.path.toLowerCase().includes(q));
  const workspace = list.filter((p) => !p.external);
  const external = list.filter((p) => p.external);
  const activeId = route.view === "project" ? route.projectId : contextProject;
  const row = (p: (typeof projects)[number]) => (
    <button
      key={p.id}
      type="button"
      className={`rz-project-row${p.id === activeId ? " active" : ""}`}
      onClick={() => navigate({ view: "project", projectId: p.id })}
      title={p.path}
    >
      {p.isGit ? <FolderGit2 size={15} /> : <Folder size={15} />}
      <span className="clamp-1 rz-project-name">{p.external ? p.path.replace(/^\/home\/[^/]+/, "~") : p.name}</span>
      {p.liveCount > 0 && <StatusDot live={{ hosted: true, busy: true, pid: 0 }} size={6} />}
      <span className="dim rz-project-count">{p.sessionCount || ""}</span>
    </button>
  );
  return (
    <div className="rz-side-list">
      <section>
        <div className="rz-side-label">工作区</div>
        {workspace.map(row)}
        {workspace.length === 0 && <div className="rz-side-empty">没有项目</div>}
      </section>
      {external.length > 0 && (
        <section>
          <div className="rz-side-label">其他目录</div>
          {external.map(row)}
        </section>
      )}
    </div>
  );
}

export function Sidebar({ onCollapse }: { onCollapse?: () => void }) {
  const [query, setQuery] = useState("");
  const tab = useUi((s) => s.sidebarTab);
  const setTab = useUi((s) => s.setSidebarTab);
  const navigate = useUi((s) => s.navigate);
  const contextProject = useUi((s) => s.contextProject);
  const route = useUi((s) => s.route);
  const setNewSessionOpen = useUi((s) => s.setNewSessionOpen);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const socketState = useApp((s) => s.socketState);
  const runnerOnline = useApp((s) => s.runnerOnline);
  const sessionsCount = useApp((s) => Object.keys(s.sessions).length);
  const lastActivity = useApp((s) => Math.max(0, ...Object.values(s.sessions).map((x) => x.mtime)));

  const newSession = () => {
    const pid = route.view === "project" ? route.projectId : contextProject;
    if (pid) navigate({ view: "project", projectId: pid });
    else setNewSessionOpen(true);
  };

  const online = socketState === "open" && runnerOnline;

  return (
    <aside className="rz-sidebar">
      <div className="rz-brand">
        <button type="button" className="rz-brand-main" onClick={() => navigate({ view: "home" })}>
          <Mascot pose="icon" size={34} />
          <span className="rz-brand-name">RieszManager</span>
        </button>
        {onCollapse && (
          <Tooltip title="收起侧栏">
            <IconButton size="small" onClick={onCollapse}><PanelLeftClose size={17} /></IconButton>
          </Tooltip>
        )}
      </div>
      <button type="button" className="rz-new" onClick={newSession}>
        <Plus size={16} /> 新会话
      </button>
      <div className="rz-search">
        <Search size={14} />
        <InputBase placeholder="搜索会话或项目" value={query} onChange={(e) => setQuery(e.target.value)} fullWidth />
      </div>
      <div className="rz-side-tabs" role="tablist">
        <button type="button" role="tab" aria-selected={tab === "recent"} className={tab === "recent" ? "on" : ""} onClick={() => setTab("recent")}>最近</button>
        <button type="button" role="tab" aria-selected={tab === "projects"} className={tab === "projects" ? "on" : ""} onClick={() => setTab("projects")}>项目</button>
      </div>
      {tab === "recent" ? <RecentList query={query} /> : <ProjectList query={query} />}
      <div className="rz-side-foot">
        <Tooltip title={online ? `已连接 · ${sessionsCount} 个会话 · 最近活动 ${relativeTime(lastActivity)}` : runnerOnline ? "正在重新连接…" : "宿主机运行器未连接"}>
          <span className={`rz-conn${online ? " on" : ""}`}>{online ? <Wifi size={14} /> : <WifiOff size={14} />}{online ? "已连接" : "未连接"}</span>
        </Tooltip>
        <div className="rz-spacer" />
        <Tooltip title="设置">
          <IconButton size="small" onClick={() => setSettingsOpen(true)}><Settings size={17} /></IconButton>
        </Tooltip>
      </div>
    </aside>
  );
}
