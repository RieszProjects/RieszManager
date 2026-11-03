/** Responsive three-pane layout. On phones the side panes become swipeable drawers. */
import { Drawer, IconButton, Tooltip } from "@mui/material";
import { PanelLeftOpen, Plus, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { Mascot } from "@/components/Mascot";
import { useIsMobile, useIsWide } from "@/lib/useIsMobile";
import { useUi } from "@/store/ui";
import { layout } from "@/theme/tokens";
import { MobileDrawers } from "./MobileDrawers";
import { RightPanel } from "./RightPanel";
import { Sidebar } from "./Sidebar";

function Rail() {
  const toggle = useUi((s) => s.toggleDesktopLeft);
  const setNewSessionOpen = useUi((s) => s.setNewSessionOpen);
  const setSettingsOpen = useUi((s) => s.setSettingsOpen);
  const navigate = useUi((s) => s.navigate);
  return (
    <div className="rz-rail">
      <button type="button" className="rz-rail-logo" onClick={() => navigate({ view: "home" })}><Mascot pose="icon" size={30} /></button>
      <Tooltip title="展开侧栏" placement="right"><IconButton size="small" onClick={toggle}><PanelLeftOpen size={18} /></IconButton></Tooltip>
      <Tooltip title="新会话" placement="right"><IconButton size="small" onClick={() => setNewSessionOpen(true)}><Plus size={18} /></IconButton></Tooltip>
      <div className="rz-spacer" />
      <Tooltip title="设置" placement="right"><IconButton size="small" onClick={() => setSettingsOpen(true)}><Settings size={17} /></IconButton></Tooltip>
    </div>
  );
}

export function AppShell({ children }: { children: ReactNode }) {
  const isMobile = useIsMobile();
  const isWide = useIsWide();
  const leftOpen = useUi((s) => s.leftOpen);
  const setLeftOpen = useUi((s) => s.setLeftOpen);
  const rightOverlay = useUi((s) => s.rightOverlay);
  const setRightOverlay = useUi((s) => s.setRightOverlay);
  const collapsed = useUi((s) => s.desktopLeftCollapsed);
  const toggleLeft = useUi((s) => s.toggleDesktopLeft);
  const contextProject = useUi((s) => s.contextProject);

  if (isMobile) {
    return (
      <div className="rz-shell mobile">
        <main className="rz-main">{children}</main>
        <MobileDrawers
          left={<Sidebar />}
          right={<RightPanel onClose={() => setRightOverlay(false)} />}
          leftOpen={leftOpen}
          rightOpen={rightOverlay && Boolean(contextProject) && !leftOpen}
          rightEnabled={Boolean(contextProject)}
          onLeft={setLeftOpen}
          onRight={setRightOverlay}
        />
      </div>
    );
  }

  return (
    <div className="rz-shell" style={{ gridTemplateColumns: `${collapsed ? "56px" : `${layout.sidebarWidth}px`} minmax(0, 1fr)` }}>
      {collapsed ? <Rail /> : <Sidebar onCollapse={toggleLeft} />}
      <main className="rz-main">{children}</main>
      {!isWide && (
        // Medium widths: the same floating card, opened over the content below the header.
        <Drawer
          anchor="right"
          open={rightOverlay && Boolean(contextProject)}
          onClose={() => setRightOverlay(false)}
          slotProps={{
            paper: { className: "rz-right-overlay-paper", sx: { width: layout.rightPanelWidth } },
            backdrop: { sx: { background: "rgba(0, 0, 0, 0.18)" } },
          }}
        >
          <RightPanel variant="floating" onClose={() => setRightOverlay(false)} />
        </Drawer>
      )}
    </div>
  );
}
