import { ListItemIcon, Menu, MenuItem } from "@mui/material";
import { Archive, ArchiveRestore, Check, Copy, Ellipsis, Pencil, Pin } from "lucide-react";
import { memo, useState } from "react";
import type { SessionSummary } from "@/api/types";
import { StatusDot } from "@/components/StatusDot";
import { copyText } from "@/lib/clipboard";
import { setKeepAlive } from "@/lib/keepAlive";
import { relativeTime } from "@/lib/format";
import { sessionTitle, useApp } from "@/store/app";
import { useOrganize } from "@/store/organize";
import { toast, useUi } from "@/store/ui";

interface Props {
  session: SessionSummary;
  showProject?: boolean;
  active?: boolean;
}

/** "⋯" menu with per-session actions. */
function SessionActions({ session, archived }: { session: SessionSummary; archived: boolean }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const setRenameTarget = useUi((s) => s.setRenameTarget);
  const setArchived = useOrganize((s) => s.setArchived);
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
        <MenuItem onClick={() => { close(); void setArchived("session", session.sessionId, !archived); }}>
          <ListItemIcon>{archived ? <ArchiveRestore size={15} /> : <Archive size={15} />}</ListItemIcon>
          {archived ? "取消归档" : "归档"}
        </MenuItem>
        <MenuItem onClick={async () => {
          close();
          try {
            await setKeepAlive(session.sessionId, !session.keepAlive);
          } catch (err) {
            toast((err as Error).message, "error");
          }
        }}>
          <ListItemIcon><Pin size={15} /></ListItemIcon>
          <span className="rz-menu-grow">保持运行</span>
          {session.keepAlive && <Check size={14} className="rz-menu-check" />}
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
  const archived = useOrganize((s) => s.archived.session.has(session.sessionId));
  return (
    <div className={`rz-session-row${active ? " active" : ""}${archived ? " archived" : ""}`}>
      <button type="button" className="rz-session-row-btn" onClick={() => navigate({ view: "session", sessionId: session.sessionId })}>
        <div className="rz-session-row-main">
          <span className="rz-session-title clamp-1">{sessionTitle(session)}</span>
          {session.keepAlive && <Pin size={11} className="rz-keep-pin" aria-label="保持运行" />}
          <StatusDot live={live} size={7} />
        </div>
        <div className="rz-session-row-sub">
          {archived && <span className="rz-tag muted">已归档</span>}
          {showProject && projectName && <span className="rz-session-project clamp-1">{projectName}</span>}
          <span className="dim">{relativeTime(session.mtime)}</span>
        </div>
      </button>
      <SessionActions session={session} archived={archived} />
    </div>
  );
}

export const SessionRow = memo(SessionRowImpl);
