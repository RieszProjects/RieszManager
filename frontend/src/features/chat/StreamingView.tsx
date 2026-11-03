/** Live rendering of the message currently being generated, plus the activity line. */
import { Brain, LoaderCircle } from "lucide-react";
import { memo, useEffect, useState } from "react";
import { Markdown } from "@/components/Markdown";
import { Mascot } from "@/components/Mascot";
import { duration } from "@/lib/format";
import type { StreamOverlay } from "@/store/sessions";
import { toolMeta } from "./tools/toolMeta";

const VERBS = ["思索中", "推敲中", "琢磨中", "斟酌中", "构思中", "整理思路", "翻阅资料", "演算中", "落笔中", "打磨中"];

function useTicker(active: boolean): number {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, [active]);
  return now;
}

export const StreamingView = memo(function StreamingView({ stream }: { stream: StreamOverlay | null }) {
  if (!stream || stream.blocks.length === 0) return null;
  return (
    <div className="rz-assistant rz-streaming">
      {stream.blocks.map((b) => {
        if (b.type === "text") return <Markdown key={b.index} text={b.text} className={b.done ? "" : "rz-caret-host"} />;
        if (b.type === "thinking") {
          return (
            <div key={b.index} className="rz-thinking muted live">
              <Brain size={14} /> 思考中…
              {b.text && <div className="rz-thinking-live">{b.text.slice(-400)}</div>}
            </div>
          );
        }
        if (b.type === "tool_use" || b.type === "server_tool_use") {
          let input: Record<string, unknown> = {};
          try {
            input = JSON.parse(b.text || "{}");
          } catch {
            /* still streaming */
          }
          const meta = toolMeta(b.name ?? "tool", input);
          const Icon = meta.icon;
          return (
            <div key={b.index} className="rz-tool pending">
              <div className="rz-tool-head static">
                <span className="rz-tool-icon"><Icon size={15} /></span>
                <span className="rz-tool-label">{meta.label}</span>
                <span className="rz-tool-summary mono">{meta.summary || (b.text ? `${b.text.length} 字符…` : "")}</span>
                <LoaderCircle size={14} className="spin" />
              </div>
            </div>
          );
        }
        return null;
      })}
    </div>
  );
});

interface ActivityProps {
  busy: boolean;
  since: number | null;
  status?: string | null;
  waiting: boolean;
  compact?: boolean;
}

export function ActivityLine({ busy, since, status, waiting, compact }: ActivityProps) {
  const now = useTicker(busy);
  const [verb] = useState(() => VERBS[Math.floor(Math.random() * VERBS.length)]);
  if (!busy) return null;
  const elapsed = since ? duration(now - since) : "";
  const label = waiting ? "等待你的确认" : status === "compacting" ? "正在压缩上下文" : verb;
  return (
    <div className="rz-activity">
      {compact ? <LoaderCircle size={14} className="spin" /> : <Mascot pose="working" size={50} className="rz-activity-mascot" />}
      <span className="rz-activity-verb">{label}…</span>
      {elapsed && <span className="dim">{elapsed}</span>}
      {!waiting && <span className="dim rz-activity-hint"><kbd>Esc</kbd> 中断</span>}
    </div>
  );
}
