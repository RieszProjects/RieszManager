/** Right panel: the current project's sessions and files. */
import { IconButton, Tooltip } from "@mui/material";
import { FolderGit2, GitBranch, Plus, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { api } from "@/api/client";
import type { Project } from "@/api/types";
import { useApp } from "@/store/app";
import { useUi } from "@/store/ui";
import { FileTree } from "../files/FileTree";
import { useOrganizeView } from "@/lib/organize";
import { useOrganize } from "@/store/organize";
import { SortableList } from "../organize/SortableList";
import { SessionRow } from "../sessions/SessionRow";

interface Props {
  /** "floating": glass card docked below the view header; "drawer": full-height phone drawer. */
  variant?: "floating" | "drawer";
  onClose?: () => void;
}

export function RightPanel({ variant = "drawer", onClose }: Props) {
  const projectId = useUi((s) => s.contextProject);
  const tab = useUi((s) => s.rightTab);
  const setTab = useUi((s) => s.setRightTab);
  const route = useUi((s) => s.route);
  const navigate = useUi((s) => s.navigate);
  const project = useApp((s) => s.projects.find((p) => p.id === projectId));
  const sessions = useApp((s) => s.sessions);
  const [detail, setDetail] = useState<Project | null>(null);

  useEffect(() => {
    setDetail(null);
    if (!projectId) return;
    api<{ project: Project }>(`projects/${encodeURIComponent(projectId)}`).then((r) => setDetail(r.project)).catch(() => undefined);
  }, [projectId, tab]);

  const view = useOrganizeView();
  const setOrder = useOrganize((s) => s.setOrder);
  const list = useMemo(
    () => view.orderSessions(Object.values(sessions).filter((s) => s.project === projectId && view.sessionVisible(s))),
    [sessions, projectId, view],
  );

  const cls = `rz-right${variant === "floating" ? " floating" : ""}`;

  if (!projectId || !project) {
    return (
      <aside className={cls}>
        <div className="rz-right-empty dim">选择一个会话或项目后，这里会显示它的会话和文件。</div>
      </aside>
    );
  }

  const activeSession = route.view === "session" ? route.sessionId : null;
  const branch = detail?.git?.branch;
  const changes = detail?.git?.changes ?? {};
  const changeCount = Object.keys(changes).length;
  const showFiles = tab === "files" && project.filesAllowed;

  return (
    <aside className={cls}>
      <div className="rz-right-head">
        <FolderGit2 size={17} className="rz-right-icon" />
        <div className="rz-right-title">
          <div className="clamp-1 rz-right-name">{project.name}</div>
          <div className="clamp-1 dim mono rz-right-path" title={project.path}>{project.path}</div>
        </div>
        <Tooltip title="在此项目中新建会话">
          <IconButton size="small" onClick={() => navigate({ view: "project", projectId })}><Plus size={17} /></IconButton>
        </Tooltip>
        {onClose && <IconButton size="small" onClick={onClose} aria-label="关闭"><X size={17} /></IconButton>}
      </div>
      {branch && (
        <div className="rz-right-git">
          <GitBranch size={13} /> <span className="mono">{branch}</span>
          {changeCount > 0 && <span className="dim">· {changeCount} 处改动</span>}
        </div>
      )}
      <div className="rz-side-tabs rz-right-tabs" role="tablist">
        <button type="button" className={showFiles ? "on" : ""} onClick={() => setTab("files")} disabled={!project.filesAllowed}>文件</button>
        <button type="button" className={showFiles ? "" : "on"} onClick={() => setTab("sessions")}>会话 <span className="dim">{list.length}</span></button>
      </div>
      <div className="rz-right-body">
        {!showFiles ? (
          <div className="rz-side-list">
            <SortableList items={list} getId={(s) => s.sessionId} onReorder={(ids) => void setOrder("session", ids)}>
              {(s) => <SessionRow session={s} active={s.sessionId === activeSession} />}
            </SortableList>
            {list.length === 0 && <div className="rz-side-empty">这个项目还没有会话</div>}
          </div>
        ) : (
          <FileTree projectId={projectId} changes={changes} />
        )}
      </div>
    </aside>
  );
}
