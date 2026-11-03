import { Dialog, DialogContent, DialogTitle } from "@mui/material";
import { Folder, FolderGit2 } from "lucide-react";
import { useMemo } from "react";
import { useOrganizeView } from "@/lib/organize";
import { useApp } from "@/store/app";
import { useUi } from "@/store/ui";

/** Project picker used when "new session" is pressed without a project in context. */
export function NewSessionDialog() {
  const open = useUi((s) => s.newSessionOpen);
  const setOpen = useUi((s) => s.setNewSessionOpen);
  const navigate = useUi((s) => s.navigate);
  const allProjects = useApp((s) => s.projects);
  const view = useOrganizeView();
  const projects = useMemo(
    () => view.orderProjects(allProjects.filter((p) => !p.external && view.projectVisible(p))),
    [allProjects, view],
  );
  return (
    <Dialog open={open} onClose={() => setOpen(false)} fullWidth maxWidth="xs">
      <DialogTitle sx={{ fontFamily: "var(--font-display)", fontSize: 22 }}>选择项目</DialogTitle>
      <DialogContent sx={{ px: 1.5, pb: 2 }}>
        {projects.map((p) => (
          <button
            key={p.id}
            type="button"
            className="rz-project-row"
            onClick={() => {
              setOpen(false);
              navigate({ view: "project", projectId: p.id });
            }}
          >
            {p.isGit ? <FolderGit2 size={15} /> : <Folder size={15} />}
            <span className="clamp-1 rz-project-name">{p.name}</span>
            <span className="dim mono rz-project-count">{p.sessionCount || ""}</span>
          </button>
        ))}
        {projects.length === 0 && <div className="rz-side-empty">工作区里还没有项目文件夹</div>}
      </DialogContent>
    </Dialog>
  );
}
