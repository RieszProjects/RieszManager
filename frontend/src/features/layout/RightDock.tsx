/**
 * The project panel as a floating card below a view's header.
 * Wide screens show it permanently; medium widths open it as an overlay from
 * the header button; phones use the swipeable drawer in AppShell.
 */
import { IconButton, Tooltip } from "@mui/material";
import { PanelRight } from "lucide-react";
import { useIsMobile, useIsWide } from "@/lib/useIsMobile";
import { useUi } from "@/store/ui";
import { RightPanel } from "./RightPanel";

export function RightDock() {
  const isWide = useIsWide();
  const contextProject = useUi((s) => s.contextProject);
  if (!isWide || !contextProject) return null;
  return (
    <div className="rz-right-dock">
      <RightPanel variant="floating" />
    </div>
  );
}

/** Header button that opens the panel where it is not permanently docked. */
export function RightPanelButton() {
  const isWide = useIsWide();
  const isMobile = useIsMobile();
  const contextProject = useUi((s) => s.contextProject);
  const setRightOverlay = useUi((s) => s.setRightOverlay);
  if (isWide || isMobile) return null;
  return (
    <Tooltip title="项目面板">
      <span>
        <IconButton size="small" aria-label="项目面板" disabled={!contextProject} onClick={() => setRightOverlay(true)}>
          <PanelRight size={18} />
        </IconButton>
      </span>
    </Tooltip>
  );
}
