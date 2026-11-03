import { memo } from "react";
import type { SessionSummary } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { relativeTime } from "@/lib/format";
import { sessionTitle, useApp } from "@/store/app";
import { useUi } from "@/store/ui";

interface Props {
  session: SessionSummary;
  showProject?: boolean;
  active?: boolean;
}

function SessionRowImpl({ session, showProject, active }: Props) {
  const navigate = useUi((s) => s.navigate);
  const live = useApp((s) => s.live[session.sessionId]);
  const projectName = useApp((s) => (showProject ? s.projects.find((p) => p.id === session.project)?.name : undefined));
  return (
    <button
      type="button"
      className={`rz-session-row${active ? " active" : ""}`}
      onClick={() => navigate({ view: "session", sessionId: session.sessionId })}
    >
      <div className="rz-session-row-main">
        <span className="rz-session-title clamp-1">{sessionTitle(session)}</span>
        <StatusDot live={live} size={7} />
      </div>
      <div className="rz-session-row-sub">
        {showProject && projectName && <span className="rz-session-project clamp-1">{projectName}</span>}
        <span className="dim">{relativeTime(session.mtime)}</span>
      </div>
    </button>
  );
}

export const SessionRow = memo(SessionRowImpl);
