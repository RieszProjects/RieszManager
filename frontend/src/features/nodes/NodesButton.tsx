/** AppBar button showing online local computer nodes and pending approvals. */
import { IconButton, Tooltip } from "@mui/material";
import { Laptop } from "lucide-react";
import { useNodes } from "./store";

export function NodesButton() {
  const devices = useNodes((s) => s.devices);
  const approvals = useNodes((s) => s.approvals);
  const open = useNodes((s) => s.open);
  const setOpen = useNodes((s) => s.setOpen);

  const online = devices.filter((d) => d.online).length;
  const pending = approvals.length;
  const title = pending > 0
    ? `本地节点（${online} 台在线 · ${pending} 条待审批）`
    : online > 0
      ? `本地节点（${online} 台在线）`
      : "本地电脑节点（点击配对）";

  return (
    <Tooltip title={title}>
      <IconButton
        size="small"
        onClick={() => setOpen(!open)}
        aria-label={title}
        className={`rz-notes-btn${open ? " active" : ""}${pending > 0 ? " pulsing" : ""}`}
      >
        <Laptop size={18} />
        {(pending > 0 || online > 0) && (
          <span className={`rz-notes-badge${pending > 0 ? " danger" : " online"}`}>
            {pending > 0 ? pending : online}
          </span>
        )}
      </IconButton>
    </Tooltip>
  );
}
