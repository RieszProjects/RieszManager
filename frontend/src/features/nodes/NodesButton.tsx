/** Session AppBar button showing local computer nodes bound strictly to THIS session. */
import { IconButton, Tooltip } from "@mui/material";
import { Laptop } from "lucide-react";
import { useApp } from "@/store/app";
import { accessibleNodeOwners, useNodes } from "./store";

export function NodesButton({ sessionId }: { sessionId: string }) {
  const nodes = useNodes((s) => s.nodes);
  const approvals = useNodes((s) => s.approvals);
  const dialogSessionId = useNodes((s) => s.dialogSessionId);
  const openForSession = useNodes((s) => s.openForSession);
  const sessions = useApp((s) => s.sessions);

  const owners = accessibleNodeOwners(sessionId, sessions);
  const mine = nodes.filter((n) => owners.has(n.sessionId));
  const pending = approvals.filter((a) => owners.has(a.sessionId)).length;
  const isOpen = dialogSessionId === sessionId;

  const title = pending > 0
    ? `本会话本地电脑（${mine.length} 台已连 · ${pending} 条待审批）`
    : mine.length > 0
      ? `本会话本地电脑（${mine.length} 台已连）`
      : "接入本地电脑到此会话";

  return (
    <Tooltip title={title}>
      <IconButton
        size="small"
        onClick={() => openForSession(isOpen ? null : sessionId)}
        aria-label={title}
        className={`rz-notes-btn${isOpen ? " active" : ""}${pending > 0 ? " pulsing" : ""}`}
      >
        <Laptop size={18} />
        {(pending > 0 || mine.length > 0) && (
          <span className={`rz-notes-badge${pending > 0 ? " danger" : " online"}`}>
            {pending > 0 ? pending : mine.length}
          </span>
        )}
      </IconButton>
    </Tooltip>
  );
}
