/**
 * Files an agent sent to the user with Riesz Manager's send_file tool (runner: tools.py, shares.py),
 * shown under the tool call: images inline, other files as cards that open in the file viewer.
 * They are looked up in the runner's record of the session (not in the tool result, which some
 * harnesses, such as Antigravity, do not pass on).
 */
import { Download, FileText } from "lucide-react";
import { useEffect, useState } from "react";
import { api, fileRawUrl } from "@/api/client";
import type { ToolBlock } from "@/api/types";
import { useUi } from "@/store/ui";
import { useSessionContext } from "../SessionContext";

export interface SentFile {
  ref: string;
  name: string;
  path: string;
  size: number;
  mime: string | null;
  caption: string | null;
  project: string;
}

/** A send_file call under any harness's naming (mcp__riesz__send_file, "Run mcp_riesz_send_file", "send_file(...)"). */
export function isSendFile(name: string): boolean {
  const n = name.replace(/^Run /, "");
  return /^mcp_+riesz_+send_file$/i.test(n) || /^send_file\b/.test(n);
}

const lists = new Map<string, { at: number; files: SentFile[]; loading?: Promise<SentFile[]> }>();

async function sentFiles(sessionId: string, fresh: boolean): Promise<SentFile[]> {
  const hit = lists.get(sessionId);
  if (hit?.loading) return hit.loading;
  if (hit && !fresh) return hit.files;
  const loading = api<{ files: SentFile[] }>(`sessions/${sessionId}/shared`).then((r) => r.files ?? []).catch(() => hit?.files ?? []);
  lists.set(sessionId, { at: Date.now(), files: hit?.files ?? [], loading });
  const files = await loading;
  lists.set(sessionId, { at: Date.now(), files });
  return files;
}

function wanted(input: Record<string, unknown>, cwd: string | null): string[] {
  const raw = input.paths ?? input.path;
  const list = Array.isArray(raw) ? raw.map(String) : typeof raw === "string" ? [raw] : [];
  return list.map((p) => (p.startsWith("/") || !cwd ? p : `${cwd.replace(/\/$/, "")}/${p.replace(/^\.\//, "")}`));
}

function size(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

export function SentFiles({ tool }: { tool: ToolBlock }) {
  const { sessionId, cwd } = useSessionContext();
  const openFile = useUi((s) => s.openFile);
  const paths = wanted((tool.input ?? {}) as Record<string, unknown>, cwd);
  const [files, setFiles] = useState<SentFile[]>(() => lists.get(sessionId)?.files ?? []);
  const found = paths.map((p) => files.find((f) => f.path === p) ?? files.find((f) => f.name === p.split("/").pop()));
  const missing = found.some((f) => !f);
  const done = Boolean(tool.result);

  useEffect(() => {
    if (!sessionId || !missing) return;
    let cancelled = false;
    let tries = 0;
    // The copy is made while the call runs: look again until it shows up (or the call is long over).
    const look = () => {
      void sentFiles(sessionId, tries > 0).then((list) => {
        if (cancelled) return;
        setFiles(list);
        if (tries++ < 8 && paths.some((p) => !list.some((f) => f.path === p || f.name === p.split("/").pop()))) {
          timer = window.setTimeout(look, 1500);
        }
      });
    };
    let timer = window.setTimeout(look, 0);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
    // re-run when the call finishes or the session changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, missing, done]);

  const shown = found.filter((f): f is SentFile => Boolean(f));
  if (!shown.length) return null;
  const caption = shown.find((f) => f.caption)?.caption;
  return (
    <div className="rz-sent">
      {caption && <div className="rz-sent-caption">{caption}</div>}
      {shown.map((f) =>
        f.mime?.startsWith("image/") && f.mime !== "image/svg+xml" ? (
          <button key={f.ref} type="button" className="rz-sent-image" title={f.name}
            onClick={() => openFile({ project: f.project, path: f.ref })}>
            <img src={fileRawUrl(f.project, f.ref)} alt={f.name} loading="lazy" />
          </button>
        ) : (
          <div key={f.ref} className="rz-sent-file">
            <button type="button" className="rz-sent-open" onClick={() => openFile({ project: f.project, path: f.ref })}>
              <FileText size={16} />
              <span className="rz-sent-name">{f.name}</span>
              <span className="rz-sent-size dim">{size(f.size)}</span>
            </button>
            <a className="rz-sent-download" href={fileRawUrl(f.project, f.ref, true)} download={f.name} title="下载">
              <Download size={15} />
            </a>
          </div>
        ),
      )}
    </div>
  );
}
