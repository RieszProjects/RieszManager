/** Composer toolbar chip showing connected local computer nodes for THIS session. */
import { Laptop } from "lucide-react";
import { useApp } from "@/store/app";
import { accessibleNodeOwners, NODE_MODE_LABEL, useNodes } from "./store";

export function NodeBindChip({ sessionId }: { sessionId: string }) {
  const nodes = useNodes((s) => s.nodes);
  const openForSession = useNodes((s) => s.openForSession);
  const sessions = useApp((s) => s.sessions);

  const owners = accessibleNodeOwners(sessionId, sessions);
  const mine = nodes.filter((n) => owners.has(n.sessionId));
  if (mine.length === 0) return null;

  const label = mine.length === 1
    ? `${mine[0].name} · ${NODE_MODE_LABEL[mine[0].mode]}`
    : `已连 ${mine.length} 台本地电脑`;

  return (
    <button
      type="button"
      className="rz-chip rz-node-bound"
      onClick={() => openForSession(sessionId)}
      title="管理本会话已连接的本地电脑（改名、调权限、查看输出或断开）"
    >
      <Laptop size={13} />
      <span>{label}</span>
    </button>
  );
}
