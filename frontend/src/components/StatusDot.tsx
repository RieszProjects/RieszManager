import { color } from "@/theme/tokens";
import type { LiveInfo } from "@/api/types";

/** Small coloured dot reflecting a session's process state. */
export function StatusDot({ live, size = 8 }: { live: LiveInfo | null | undefined; size?: number }) {
  if (!live) return null;
  const waiting = live.waiting;
  const busy = live.busy;
  const bg = waiting ? color.amber : busy ? color.sage : live.hosted ? color.sageDeep : color.text4;
  const title = waiting ? "等待确认" : busy ? "运行中" : live.hosted ? "已连接（空闲）" : "在终端中打开";
  return (
    <span
      title={title}
      style={{
        display: "inline-block",
        width: size,
        height: size,
        borderRadius: "50%",
        background: bg,
        flexShrink: 0,
        boxShadow: busy || waiting ? `0 0 0 3px ${waiting ? "rgba(198,150,107,.18)" : "rgba(147,168,140,.18)"}` : undefined,
        animation: busy || waiting ? "rz-pulse 1.6s ease-in-out infinite" : undefined,
      }}
    />
  );
}
