import { IconButton } from "@mui/material";
import { FolderTree, Menu } from "lucide-react";
import { useApp } from "@/store/app";
import { useUi } from "@/store/ui";

/** Hamburger / project-panel buttons used in headers on small screens. */
export function MobileHeaderButtons({ side }: { side: "left" | "right" }) {
  const setLeftOpen = useUi((s) => s.setLeftOpen);
  const setRightOverlay = useUi((s) => s.setRightOverlay);
  const contextProject = useUi((s) => s.contextProject);
  const waiting = useApp((s) => Object.values(s.live).some((l) => l.waiting));
  if (side === "left") {
    return (
      <IconButton size="small" onClick={() => setLeftOpen(true)} aria-label="打开会话列表" className="rz-mobile-menu">
        <Menu size={20} />
        {waiting && <span className="rz-badge-dot" />}
      </IconButton>
    );
  }
  return (
    <IconButton size="small" onClick={() => setRightOverlay(true)} aria-label="打开项目面板" disabled={!contextProject}>
      <FolderTree size={19} />
    </IconButton>
  );
}
