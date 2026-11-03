/** Lazily-loaded, read-only project file tree with git status markers. */
import { ChevronRight, File, FileCode2, FileImage, FileText, Folder, FolderOpen, LoaderCircle, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { api } from "@/api/client";
import type { FileEntry } from "@/api/types";
import { useUi } from "@/store/ui";

const CODE_EXT = /\.(py|ts|tsx|js|jsx|mjs|rs|go|java|c|cpp|h|sh|rb|php|css|scss|html|vue|json|ya?ml|toml|sql)$/i;
const IMAGE_EXT = /\.(png|jpe?g|gif|webp|svg|bmp|ico|avif)$/i;
const TEXT_EXT = /\.(md|txt|rst|log|csv|tex)$/i;

function iconFor(entry: FileEntry) {
  if (IMAGE_EXT.test(entry.name)) return FileImage;
  if (CODE_EXT.test(entry.name)) return FileCode2;
  if (TEXT_EXT.test(entry.name)) return FileText;
  return File;
}

interface Props {
  projectId: string;
  changes?: Record<string, string>;
}

export function FileTree({ projectId, changes = {} }: Props) {
  const [children, setChildren] = useState<Record<string, FileEntry[]>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set([""]));
  const [loading, setLoading] = useState<Set<string>>(new Set());
  const [error, setError] = useState<string | null>(null);
  const openFile = useUi((s) => s.openFile);
  const activeFile = useUi((s) => (s.file?.project === projectId ? s.file.path : null));

  const load = useCallback(
    async (path: string) => {
      setLoading((s) => new Set(s).add(path));
      try {
        const res = await api<{ entries: FileEntry[] }>("files/list", { query: { project: projectId, path } });
        setChildren((c) => ({ ...c, [path]: res.entries }));
        setError(null);
      } catch (err) {
        if (path === "") setError((err as Error).message);
      } finally {
        setLoading((s) => {
          const n = new Set(s);
          n.delete(path);
          return n;
        });
      }
    },
    [projectId],
  );

  useEffect(() => {
    setChildren({});
    setExpanded(new Set([""]));
    void load("");
  }, [projectId, load]);

  const toggle = (path: string) => {
    setExpanded((s) => {
      const n = new Set(s);
      if (n.has(path)) n.delete(path);
      else {
        n.add(path);
        if (!children[path]) void load(path);
      }
      return n;
    });
  };

  const dirtyDirs = new Set<string>();
  for (const p of Object.keys(changes)) {
    const parts = p.split("/");
    for (let i = 1; i < parts.length; i++) dirtyDirs.add(parts.slice(0, i).join("/"));
  }

  const render = (path: string, depth: number) => {
    const list = children[path];
    if (!list) return loading.has(path) ? <div className="rz-tree-loading" style={{ paddingLeft: 14 + depth * 14 }}><LoaderCircle size={13} className="spin" /></div> : null;
    if (!list.length) return <div className="rz-tree-empty" style={{ paddingLeft: 26 + depth * 14 }}>空目录</div>;
    return list.map((e) => {
      const open = expanded.has(e.path);
      const status = changes[e.path] ?? changes[`${e.path}/`];
      const Icon = e.dir ? (open ? FolderOpen : Folder) : iconFor(e);
      return (
        <div key={e.path}>
          <button
            type="button"
            className={`rz-tree-row${activeFile === e.path ? " active" : ""}${e.heavy ? " heavy" : ""}`}
            style={{ paddingLeft: 8 + depth * 14 }}
            onClick={() => (e.dir ? toggle(e.path) : openFile({ project: projectId, path: e.path }))}
            title={e.path}
          >
            {e.dir ? <ChevronRight size={13} className={`rz-tree-chevron${open ? " open" : ""}`} /> : <span className="rz-tree-chevron-space" />}
            <Icon size={14} className="rz-tree-icon" />
            <span className="clamp-1 rz-tree-name">{e.name}</span>
            {status ? <span className={`rz-git s-${status.replace("?", "u")}`}>{status === "??" ? "U" : status[0]}</span> : e.dir && dirtyDirs.has(e.path) ? <span className="rz-git-dot" /> : null}
          </button>
          {e.dir && open && render(e.path, depth + 1)}
        </div>
      );
    });
  };

  return (
    <div className="rz-tree">
      <div className="rz-tree-toolbar">
        <span className="dim">只读</span>
        <button type="button" onClick={() => { setChildren({}); void load(""); for (const p of expanded) if (p) void load(p); }} aria-label="刷新">
          <RefreshCw size={13} />
        </button>
      </div>
      {error ? <div className="rz-side-empty">{error}</div> : render("", 0)}
    </div>
  );
}
