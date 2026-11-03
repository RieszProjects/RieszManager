/** Responsive three-pane layout. On phones the side panes become swipeable drawers. */
import { Drawer, IconButton, SwipeableDrawer, Tooltip } from "@mui/material";
import { PanelLeftOpen, Plus, Settings } from "lucide-react";
import type { ReactNode } from "react";
import { Mascot } from "@/components/Mascot";
import { useIsMobile, useIsWide } from "@/lib/useIsMobile";
import { useUi } from "@/store/ui";
import { layout } from "@/theme/tokens";
import { RightPanel } from "./RightPanel";
import { Sidebar } from "./Sidebar";

// Keep the invisible edge-swipe zones clear of the header and composer buttons.
const SWIPE_AREA = { sx: { top: "calc(64px + var(--safe-top)) !important", bottom: "150px !important", height: "auto !important" } };

const iOS = typeof navigator !== "undefined" && /iPad|iPhone|iPod/.test(navigator.userAgent);

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
  const rightOpen = useUi((s) => s.rightOpen);
  const rightOverlay = useUi((s) => s.rightOverlay);
  const setRightOverlay = useUi((s) => s.setRightOverlay);
  const collapsed = useUi((s) => s.desktopLeftCollapsed);
  const toggleLeft = useUi((s) => s.toggleDesktopLeft);
  const contextProject = useUi((s) => s.contextProject);

  if (isMobile) {
    return (
      <div className="rz-shell mobile">
        <SwipeableDrawer
          anchor="left"
          open={leftOpen}
          onOpen={() => setLeftOpen(true)}
          onClose={() => setLeftOpen(false)}
          swipeAreaWidth={24}
          disableBackdropTransition={!iOS}
          disableDiscovery={iOS}
          slotProps={{ paper: { sx: { width: "min(86vw, 340px)" } }, swipeArea: SWIPE_AREA }}
          ModalProps={{ keepMounted: true }}
        >
          <Sidebar />
        </SwipeableDrawer>
        <main className="rz-main">{children}</main>
        <SwipeableDrawer
          anchor="right"
          open={rightOverlay && Boolean(contextProject) && !leftOpen}
          onOpen={() => contextProject && setRightOverlay(true)}
          onClose={() => setRightOverlay(false)}
          swipeAreaWidth={contextProject ? 20 : 0}
          disableBackdropTransition={!iOS}
          disableDiscovery={iOS}
          slotProps={{ paper: { sx: { width: "min(88vw, 360px)" } }, swipeArea: SWIPE_AREA }}
        >
          <RightPanel onClose={() => setRightOverlay(false)} />
        </SwipeableDrawer>
      </div>
    );
  }

  const showRightInline = isWide && rightOpen && Boolean(contextProject);
  return (
    <div
      className="rz-shell"
      style={{
        gridTemplateColumns: `${collapsed ? "56px" : `${layout.sidebarWidth}px`} minmax(0, 1fr) ${showRightInline ? `${layout.rightPanelWidth}px` : ""}`,
      }}
    >
      {collapsed ? <Rail /> : <Sidebar onCollapse={toggleLeft} />}
      <main className="rz-main">{children}</main>
      {showRightInline && <RightPanel />}
      {!isWide && (
        <Drawer anchor="right" open={rightOverlay && Boolean(contextProject)} onClose={() => setRightOverlay(false)}
          slotProps={{ paper: { sx: { width: layout.rightPanelWidth } } }}>
          <RightPanel onClose={() => setRightOverlay(false)} />
        </Drawer>
      )}
    </div>
  );
}
