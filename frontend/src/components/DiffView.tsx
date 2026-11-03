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
