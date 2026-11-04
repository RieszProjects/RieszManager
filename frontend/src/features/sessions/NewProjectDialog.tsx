/** Creates a project: a new folder directly under the workspace root. */
import { Button, Dialog, DialogActions, DialogContent, DialogTitle, TextField } from "@mui/material";
import { useEffect, useState } from "react";
import { ApiError, api } from "@/api/client";
import type { Project } from "@/api/types";
import { useApp } from "@/store/app";
import { useUi } from "@/store/ui";

const MAX_NAME_BYTES = 200;

/** Same rules as the runner (riesz_runner/server.py: _project_name_problem). */
function nameProblem(name: string): string | null {
  if (name.startsWith(".")) return "不能以点开头";
  if (/[/\\\u0000-\u001f\u007f]/.test(name)) return "不能包含 / 或 \\";
  if (new TextEncoder().encode(name).length > MAX_NAME_BYTES) return "名字太长";
  return null;
}

const SERVER_PROBLEMS: Record<string, string> = {
  exists: "已经有同名的文件或文件夹",
  dot: "不能以点开头",
  chars: "不能包含 / 或 \\",
  long: "名字太长",
  "no-root": "工作区文件夹不存在",
};

export function NewProjectDialog() {
  const open = useUi((s) => s.newProjectOpen);
  const setOpen = useUi((s) => s.setNewProjectOpen);
  const navigate = useUi((s) => s.navigate);
  const root = useApp((s) => s.config?.workspaceRoot);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [serverError, setServerError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setName("");
      setServerError(null);
    }
  }, [open]);

  const trimmed = name.trim();
  const problem = trimmed ? nameProblem(trimmed) : null;
  const error = problem ?? serverError;

  const create = async () => {
    if (!trimmed || problem || busy) return;
    setBusy(true);
    try {
      const res = await api<{ project: Project }>("projects", { body: { name: trimmed } });
      await useApp.getState().loadProjects().catch(() => undefined);
      setOpen(false);
      navigate({ view: "project", projectId: res.project.id });
    } catch (err) {
      const code = err instanceof ApiError ? err.code : undefined;
      setServerError((code && SERVER_PROBLEMS[code]) || (err as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Dialog open={open} onClose={() => !busy && setOpen(false)} fullWidth maxWidth="xs">
      <DialogTitle sx={{ fontFamily: "var(--font-display)", fontSize: 22 }}>新建项目</DialogTitle>
      <DialogContent>
        <TextField
          autoFocus
          fullWidth
          margin="dense"
          label="文件夹名"
          value={name}
          onChange={(e) => {
            setName(e.target.value);
            setServerError(null);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void create();
            }
          }}
          error={Boolean(error)}
          helperText={error ?? (root ? `${root.replace(/\/+$/, "")}/${trimmed}` : " ")}
        />
      </DialogContent>
      <DialogActions sx={{ px: 3, pb: 2 }}>
        <Button onClick={() => setOpen(false)} disabled={busy}>取消</Button>
        <Button variant="contained" onClick={() => void create()} disabled={!trimmed || Boolean(problem) || busy}>创建</Button>
      </DialogActions>
    </Dialog>
  );
}
