import { structuredPatch } from "diff";
import { memo, useMemo } from "react";

export interface Hunk {
  oldStart: number;
  oldLines: number;
  newStart: number;
  newLines: number;
  lines: string[];
}

interface Props {
  hunks?: Hunk[] | null;
  oldText?: string;
  newText?: string;
  maxHeight?: number | string;
}

function DiffViewImpl({ hunks, oldText, newText, maxHeight }: Props) {
  const resolved = useMemo<Hunk[]>(() => {
    if (hunks && hunks.length) return hunks;
    if (oldText === undefined || newText === undefined) return [];
    return structuredPatch("a", "b", oldText, newText, "", "", { context: 3 }).hunks as Hunk[];
  }, [hunks, oldText, newText]);

  if (!resolved.length) return <div className="rz-diff-empty">没有变化</div>;

  return (
    <div className="rz-diff" style={{ maxHeight }}>
      {resolved.map((h, hi) => {
        let oldNo = h.oldStart;
        let newNo = h.newStart;
        return (
          <div key={hi} className="rz-diff-hunk">
            {hi > 0 && <div className="rz-diff-sep">⋯</div>}
            {h.lines.map((line, li) => {
              const sign = line[0];
              const body = line.slice(1);
              let a = "";
              let b = "";
              let cls = "ctx";
              if (sign === "-") {
                a = String(oldNo++);
                cls = "del";
              } else if (sign === "+") {
                b = String(newNo++);
                cls = "add";
              } else if (sign === "\\") {
                return null;
              } else {
                a = String(oldNo++);
                b = String(newNo++);
              }
              return (
                <div key={li} className={`rz-diff-line ${cls}`}>
                  <span className="rz-diff-no">{a}</span>
                  <span className="rz-diff-no">{b}</span>
                  <span className="rz-diff-sign">{sign === " " ? "" : sign}</span>
                  <span className="rz-diff-text">{body || " "}</span>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

export const DiffView = memo(DiffViewImpl);

export function diffStats(hunks: Hunk[] | null | undefined, oldText?: string, newText?: string): { add: number; del: number } {
  let list = hunks;
  if ((!list || !list.length) && oldText !== undefined && newText !== undefined) {
    list = structuredPatch("a", "b", oldText, newText, "", "", { context: 0 }).hunks as Hunk[];
  }
  let add = 0;
  let del = 0;
  for (const h of list ?? []) {
    for (const l of h.lines) {
      if (l[0] === "+") add++;
      else if (l[0] === "-") del++;
    }
  }
  return { add, del };
}

/** Hunks of a unified diff ("@@ -1,3 +1,4 @@" …); text without hunk headers counts as added lines. */
export function parseUnifiedDiff(text: string, added = true): Hunk[] {
  const lines = text.replace(/\n$/, "").split("\n");
  if (!lines.some((l) => l.startsWith("@@"))) {
    if (!text) return [];
    const sign = added ? "+" : "-";
    return [{ oldStart: added ? 0 : 1, oldLines: added ? 0 : lines.length, newStart: added ? 1 : 0, newLines: added ? lines.length : 0,
      lines: lines.map((l) => sign + l) }];
  }
  const hunks: Hunk[] = [];
  let cur: Hunk | null = null;
  for (const line of lines) {
    const m = line.match(/^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/);
    if (m) {
      cur = { oldStart: +m[1], oldLines: m[2] === undefined ? 1 : +m[2], newStart: +m[3], newLines: m[4] === undefined ? 1 : +m[4], lines: [] };
      hunks.push(cur);
    } else if (cur && /^[ +\-\\]/.test(line) && !line.startsWith("+++") && !line.startsWith("---")) {
      cur.lines.push(line);
    } else if (cur && line === "") {
      cur.lines.push(" ");
    }
  }
  return hunks;
}

export interface PatchChange {
  path: string;
  kind?: { type?: string; move_path?: string | null } | string;
  diff?: string;
}

/** Codex's file changes (ApplyPatch): each file's hunks. */
export function patchHunks(change: PatchChange): Hunk[] {
  const kind = typeof change.kind === "string" ? change.kind : change.kind?.type;
  return parseUnifiedDiff(change.diff ?? "", kind !== "delete");
}
