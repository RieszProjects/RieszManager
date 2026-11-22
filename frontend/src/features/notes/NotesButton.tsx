/** AppBar button showing how many sticky notes are available and toggling the floating notes window. */
import { IconButton, Tooltip } from "@mui/material";
import { StickyNote } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useApp } from "@/store/app";
import { notesForSession, useNotes } from "./store";

export function NotesButton({ sessionId }: { sessionId?: string | null }) {
  const items = useNotes((s) => s.items);
  const open = useNotes((s) => s.open);
  const minimized = useNotes((s) => s.minimized);
  const pulse = useNotes((s) => s.pulse);
  const toggleOpen = useNotes((s) => s.toggleOpen);
  const sessions = useApp((s) => s.sessions);
  const [pulsing, setPulsing] = useState(false);

  useEffect(() => {
    if (!pulse) return;
    setPulsing(true);
    const t = window.setTimeout(() => setPulsing(false), 1800);
    return () => window.clearTimeout(t);
  }, [pulse]);

  const sessionCount = useMemo(
    () => (sessionId ? notesForSession(items, sessions, sessionId).length : 0),
    [items, sessions, sessionId],
  );
  const totalCount = items.length;
  const badgeCount = sessionId ? (sessionCount > 0 ? sessionCount : totalCount) : totalCount;
  const mutedBadge = Boolean(sessionId && sessionCount === 0 && totalCount > 0);
  const active = open && !minimized;

  const title = sessionId
    ? sessionCount > 0
      ? `便签（本会话 ${sessionCount} 条${totalCount > sessionCount ? ` · 共 ${totalCount} 条` : ""}）`
      : totalCount > 0
        ? `便签（其他会话共 ${totalCount} 条）`
        : "便签"
    : totalCount > 0
      ? `便签（共 ${totalCount} 条）`
      : "便签";

  const onClick = () => {
    const preferScope: "session" | "all" = sessionId && sessionCount > 0 ? "session" : "all";
    toggleOpen(preferScope);
  };

  return (
    <Tooltip title={title}>
      <IconButton
        size="small"
        onClick={onClick}
        aria-label={title}
        className={`rz-notes-btn${active ? " active" : ""}${pulsing ? " pulsing" : ""}`}
      >
        <StickyNote size={18} />
        {badgeCount > 0 && (
          <span className={`rz-notes-badge${mutedBadge ? " muted" : ""}`}>
            {badgeCount > 99 ? "99+" : badgeCount}
          </span>
        )}
      </IconButton>
    </Tooltip>
  );
}
