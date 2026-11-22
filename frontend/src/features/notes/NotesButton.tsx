/** AppBar button showing how many sticky notes belong to this session and toggling the floating notes window. */
import { IconButton, Tooltip } from "@mui/material";
import { StickyNote } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useApp } from "@/store/app";
import { notesForSession, useNotes } from "./store";

export function NotesButton({ sessionId }: { sessionId: string }) {
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

  const count = useMemo(
    () => notesForSession(items, sessions, sessionId).length,
    [items, sessions, sessionId],
  );
  const active = open && !minimized;
  const title = count > 0 ? `本会话便签（${count} 条）` : "便签";

  return (
    <Tooltip title={title}>
      <IconButton
        size="small"
        onClick={() => toggleOpen()}
        aria-label={title}
        className={`rz-notes-btn${active ? " active" : ""}${pulsing ? " pulsing" : ""}`}
      >
        <StickyNote size={18} />
        {count > 0 && (
          <span className="rz-notes-badge">
            {count > 99 ? "99+" : count}
          </span>
        )}
      </IconButton>
    </Tooltip>
  );
}
