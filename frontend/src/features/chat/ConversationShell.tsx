/** Shared full-height conversation frame used by both ChatView and read-only SubagentViewer. */
import type { DragEventHandler, ReactNode } from "react";
import { useIsMobile } from "@/lib/useIsMobile";
import { MobileHeaderButtons } from "../layout/MobileHeaderButtons";
import { RightDock, RightPanelButton } from "../layout/RightDock";
import { SessionContext, type SessionContextValue } from "./SessionContext";

interface Props {
  context: SessionContextValue;
  headStart?: ReactNode;
  title: ReactNode;
  subtitle: ReactNode;
  headActions?: ReactNode;
  body: ReactNode;
  bottom: ReactNode;
  overlay?: ReactNode;
  onDragOver?: DragEventHandler<HTMLDivElement>;
  onDragLeave?: DragEventHandler<HTMLDivElement>;
  onDrop?: DragEventHandler<HTMLDivElement>;
}

export function ConversationShell({
  context,
  headStart,
  title,
  subtitle,
  headActions,
  body,
  bottom,
  overlay,
  onDragOver,
  onDragLeave,
  onDrop,
}: Props) {
  const isMobile = useIsMobile();
  return (
    <SessionContext.Provider value={context}>
      <div className="rz-chat" onDragOver={onDragOver} onDragLeave={onDragLeave} onDrop={onDrop}>
        <header className="rz-chat-head">
          {isMobile && <MobileHeaderButtons side="left" />}
          {headStart}
          <div className="rz-chat-title">
            <div className="clamp-1 rz-chat-title-text">{title}</div>
            <div className="rz-chat-sub">{subtitle}</div>
          </div>
          {headActions}
          {isMobile ? <MobileHeaderButtons side="right" /> : <RightPanelButton />}
        </header>
        <div className="rz-chat-row">
          <div className="rz-chat-col">
            <div className="rz-chat-body">{body}</div>
            <div className="rz-chat-bottom">{bottom}</div>
          </div>
          <RightDock />
        </div>
        {overlay}
      </div>
    </SessionContext.Provider>
  );
}
