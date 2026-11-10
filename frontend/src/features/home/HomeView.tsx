/** Landing view (nothing selected) and the per-project "new session" view. */
import { Button, Switch } from "@mui/material";
import { AlarmClock, ChevronRight } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import { Mascot } from "@/components/Mascot";
import { clockTime } from "@/lib/format";
import { useOrganizeView } from "@/lib/organize";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";
import { SessionRow } from "../sessions/SessionRow";
import { useSchedules } from "../schedules/store";
import { Composer } from "../composer/Composer";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";

type DayPart = "night" | "morning" | "noon" | "afternoon" | "evening";

function dayPart(): DayPart {
  const h = new Date().getHours();
  if (h < 5) return "night";
  if (h < 11) return "morning";
  if (h < 14) return "noon";
  if (h < 18) return "afternoon";
  return "evening";
}

const GREETING: Record<DayPart, string> = {
  night: "这么晚了还不睡吗",
  morning: "早上好",
  noon: "中午好",
  afternoon: "下午好",
  evening: "晚上好",
};

/** What Riesz (the mascot, perpetually sleepy and short on words) says when nothing needs you. */
const IDLE_LINES: Record<DayPart, string[]> = {
  night: ["我先眯一会儿。你忙完也早点睡。", "……这个点还在写代码吗。", "夜里安静，适合把事情做完。别熬太晚。"],
  morning: ["……早。我还没完全醒。", "新的一天。先从哪个项目开始？", "选一个项目开始新会话，或者接着昨天的工作。"],
  noon: ["吃过饭再开始也来得及。", "中午容易犯困。我是说，我。", "选一个项目，或者接着上午的会话。"],
  afternoon: ["……刚才没睡着。你说。", "下午了。有什么想交给 Claude 的？", "选一个项目开始新会话，或者继续之前的工作。"],
  evening: ["今天辛苦了。还要继续吗？", "晚上了。把剩下的收个尾？", "选一个项目开始新会话，或者继续之前的工作。"],
};

/** The landing view's greeting: the time of day, then a line about what is going on. */
function HomeGreeting() {
  const live = useApp((s) => s.live);
  const sessions = useApp((s) => s.sessions);
  const navigate = useUi((s) => s.navigate);
  const part = dayPart();
  // One idle line per visit, so it does not change while you look at it.
  const [pick] = useState(() => Math.random());
  const { running, waiting } = useMemo(() => {
    const entries = Object.entries(live);
    return {
      running: entries.filter(([, l]) => l.busy).length,
      waiting: entries.filter(([, l]) => l.waiting).map(([sid]) => sid),
    };
  }, [live]);

  let line: string;
  if (waiting.length) line = waiting.length > 1 ? `有 ${waiting.length} 个会话在等你点头。` : "有个会话在等你点头。";
  else if (running) line = running > 1 ? `有 ${running} 个会话还在跑，我替你看着。` : "有个会话还在跑，我替你看着。";
  else {
    const lines = IDLE_LINES[part];
    line = lines[Math.floor(pick * lines.length)];
  }

  return (
    <div className="rz-home-words">
      <h1 className="rz-home-title rz-home-greeting">{GREETING[part]}</h1>
      <p className="rz-home-line">
        {line}
        {waiting.length > 0 && (
          <button type="button" className="rz-home-action" onClick={() => navigate({ view: "session", sessionId: waiting[0] })}>
            去看看「{sessionTitle(sessions[waiting[0]])}」
          </button>
        )}
      </p>
    </div>
  );
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

/** Sessions cut off while working (the server restarted, the runner stopped): carry on or let go. */
function HomeInterrupted() {
  const sessions = useApp((s) => s.sessions);
  const projects = useApp((s) => s.projects);
  const live = useApp((s) => s.live);
  const navigate = useUi((s) => s.navigate);
  const [pending, setPending] = useState<Set<string>>(new Set());
  const list = useMemo(
    () => Object.values(sessions)
      .filter((s) => s.interrupted && !live[s.sessionId]?.busy)
      .sort((a, b) => (b.interrupted ?? 0) - (a.interrupted ?? 0)),
    [sessions, live],
  );
  if (!list.length) return null;

  const act = async (sid: string, action: "resume" | "dismiss-interrupted") => {
    setPending((p) => new Set(p).add(sid));
    try {
      await api(`sessions/${sid}/${action}`, { body: {} });
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setPending((p) => {
        const n = new Set(p);
        n.delete(sid);
        return n;
      });
    }
  };

  return (
    <section className="rz-home-recent rz-home-interrupted">
      <div className="rz-home-recent-head">
        <span className="rz-side-label">被中断的会话</span>
        {list.length > 1 && (
          <button type="button" className="rz-home-recent-all" onClick={() => list.forEach((s) => void act(s.sessionId, "resume"))}>
            全部恢复
          </button>
        )}
      </div>
      {list.map((s) => (
        <div key={s.sessionId} className="rz-interrupted-row">
          <button type="button" className="rz-interrupted-main" onClick={() => navigate({ view: "session", sessionId: s.sessionId })}>
            <span className="rz-interrupted-dot" />
            <span className="rz-interrupted-text">
              <span className="clamp-1">{sessionTitle(s)}</span>
              <span className="dim clamp-1">{projects.find((p) => p.id === s.project)?.name ?? ""} · {clockTime(s.interrupted)}</span>
            </span>
          </button>
          <Button size="small" variant="outlined" disabled={pending.has(s.sessionId)} onClick={() => void act(s.sessionId, "resume")}>恢复</Button>
          <Button size="small" color="inherit" disabled={pending.has(s.sessionId)} onClick={() => void act(s.sessionId, "dismiss-interrupted")}>忽略</Button>
        </div>
      ))}
    </section>
  );
}

/** Every scheduled wake-up, soonest first: switch one off here, or open its session's schedules. */
function HomeSchedules() {
  const items = useSchedules((s) => s.items);
  const sessions = useApp((s) => s.sessions);
  const setScheduleSession = useUi((s) => s.setScheduleSession);
  if (!items.length) return null;
  const sorted = [...items].sort((a, b) => Number(!a.enabled) - Number(!b.enabled) || (a.next ?? Infinity) - (b.next ?? Infinity));
  return (
    <section className="rz-home-recent rz-home-schedules">
      <div className="rz-home-recent-head"><span className="rz-side-label">定时唤醒</span></div>
      {sorted.map((s) => (
        <div key={s.id} className={`rz-interrupted-row${s.enabled ? "" : " off"}`}>
          <button type="button" className="rz-interrupted-main" onClick={() => setScheduleSession(s.sessionId)}>
            <AlarmClock size={14} className="rz-sched-mark" />
            <span className="rz-interrupted-text">
              <span className="clamp-1">{s.name || (sessions[s.sessionId] ? sessionTitle(sessions[s.sessionId]) : s.sessionId.slice(0, 8))}</span>
              <span className="dim clamp-1">{s.rule} · {s.enabled && s.next ? `下次 ${clockTime(s.next)}` : "已暂停"}</span>
            </span>
          </button>
          <Switch size="small" checked={s.enabled}
            onChange={(e) => void useSchedules.getState().update(s.id, { enabled: e.target.checked }).catch((err: Error) => toast(err.message, "error"))} />
        </div>
      ))}
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
        <HomeGreeting />
      </div>
      <HomeInterrupted />
      <HomeSchedules />
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
