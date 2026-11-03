import { ListItemIcon, Menu, MenuItem } from "@mui/material";
import { Copy, Ellipsis, Pencil } from "lucide-react";
import { memo, useState } from "react";
import type { SessionSummary } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { copyText } from "@/lib/clipboard";
import { relativeTime } from "@/lib/format";
import { sessionTitle, useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";

interface Props {
  session: SessionSummary;
  showProject?: boolean;
  active?: boolean;
}

/** "⋯" menu with per-session actions. */
function SessionActions({ session }: { session: SessionSummary }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const setRenameTarget = useUi((s) => s.setRenameTarget);
  const close = () => setAnchor(null);
  return (
    <>
      <button
        type="button"
        className={`rz-session-more${anchor ? " open" : ""}`}
        aria-label="会话操作"
        onClick={(e) => {
          e.stopPropagation();
          setAnchor(e.currentTarget);
        }}
      >
        <Ellipsis size={15} />
      </button>
      <Menu anchorEl={anchor} open={Boolean(anchor)} onClose={close} onClick={(e) => e.stopPropagation()}
        anchorOrigin={{ vertical: "bottom", horizontal: "right" }} transformOrigin={{ vertical: "top", horizontal: "right" }}>
        <MenuItem onClick={() => { close(); setRenameTarget({ sessionId: session.sessionId, title: sessionTitle(session) }); }}>
          <ListItemIcon><Pencil size={15} /></ListItemIcon>重命名
        </MenuItem>
        <MenuItem onClick={async () => { close(); if (await copyText(session.sessionId)) toast("已复制会话 ID", "success"); }}>
          <ListItemIcon><Copy size={15} /></ListItemIcon>复制会话 ID
        </MenuItem>
      </Menu>
    </>
  );
}

function SessionRowImpl({ session, showProject, active }: Props) {
  const navigate = useUi((s) => s.navigate);
  const live = useApp((s) => s.live[session.sessionId]);
  const projectName = useApp((s) => (showProject ? s.projects.find((p) => p.id === session.project)?.name : undefined));
  return (
    <div className={`rz-session-row${active ? " active" : ""}`}>
      <button type="button" className="rz-session-row-btn" onClick={() => navigate({ view: "session", sessionId: session.sessionId })}>
        <div className="rz-session-row-main">
          <span className="rz-session-title clamp-1">{sessionTitle(session)}</span>
          <StatusDot live={live} size={7} />
        </div>
        <div className="rz-session-row-sub">
          {showProject && projectName && <span className="rz-session-project clamp-1">{projectName}</span>}
          <span className="dim">{relativeTime(session.mtime)}</span>
        </div>
      </button>
      <SessionActions session={session} />
    </div>
  );
}

export const SessionRow = memo(SessionRowImpl);
