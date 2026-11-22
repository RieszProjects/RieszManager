/** What the latest turn of a session is doing: running a tool, waiting on background work, or cut off. */
import type { Item, SessionState, TaskInfo, ToolBlock } from "@/api/types";

export interface TurnStatus {
  /** True while a claude process is actually working on the session. */
  working: boolean;
  /** Tool calls that are genuinely still running. */
  pendingTools: Set<string>;
  /** The tool call the session is currently waiting on, and when it started. */
  waitingTool: ToolBlock | null;
  waitingSince: string | null;
  /** Background tasks a hosted, otherwise idle session is still waiting for. */
  backgroundTasks: TaskInfo[];
  /** Tool calls without a result in a turn that still ended normally: the harness never reported them done. */
  settledTools: Set<string>;
  /** The last turn stopped before finishing and nothing is running it any more. */
  terminated: boolean;
}

const isPrompt = (it: Item) => (it.kind === "user" && (!it.origin || it.origin === "human")) || it.kind === "command" || it.kind === "bash";

export function turnStatus(items: Item[], state: SessionState | null): TurnStatus {
  const alive = Boolean(state?.alive);
  const hosted = Boolean(state?.hosted && alive);
  const working = alive && (Boolean(state?.busy) || (hosted && (state?.queue?.length ?? 0) > 0));

  let lastPrompt = -1;
  for (let i = items.length - 1; i >= 0; i--) {
    if (isPrompt(items[i])) {
      lastPrompt = i;
      break;
    }
  }
  const trailing = items.slice(lastPrompt + 1);
  const unresolved: { tool: ToolBlock; ts: string | null }[] = [];
  const superseded: ToolBlock[] = [];
  let answered = false;
  for (const it of trailing) {
    if (it.kind === "assistant") {
      if (!it.synthetic) answered = true;
      for (const b of it.blocks) {
        if (b.type === "tool" && !b.result) {
          unresolved.push({ tool: b, ts: it.ts });
        } else if (b.type === "text" && !it.synthetic && b.text.trim() && unresolved.length > 0) {
          // The agent wrote a reply after these tool calls: they already finished (the harness just omitted their results).
          for (const u of unresolved) superseded.push(u.tool);
          unresolved.length = 0;
        }
      }
    } else if (it.kind === "output" || it.kind === "notice" || it.kind === "compact") {
      answered = true;
    }
  }
  const interrupted = trailing.some((it) => it.kind === "interrupted");
  const backgroundTasks = hosted ? (state?.tasks ?? []).filter((t) => t.background) : [];
  const waiting = working ? unresolved[unresolved.length - 1] ?? null : null;
  const promptUnanswered = lastPrompt >= 0 && items[lastPrompt].kind === "user" && !answered;
  // The harness said the turn finished fine after those tool calls: they ended, the harness just never said so.
  const result = state?.lastResult;
  const lastCall = unresolved.length ? Date.parse(unresolved[unresolved.length - 1].ts ?? "") / 1000 : NaN;
  const endedFine = !working && !!result && result.subtype === "success" && !result.isError && result.ts >= lastCall;
  const settledTools = new Set<string>(superseded.map((t) => t.id));
  if (endedFine) for (const u of unresolved) settledTools.add(u.tool.id);

  return {
    working,
    pendingTools: new Set(working ? unresolved.map((u) => u.tool.id) : []),
    waitingTool: waiting?.tool ?? null,
    waitingSince: waiting?.ts ?? null,
    backgroundTasks,
    settledTools,
    terminated: !working && !interrupted && !endedFine && backgroundTasks.length === 0 && (unresolved.length > 0 || promptUnanswered),
  };
}

/** Activity-line wording for the tool a session is waiting on. */
export function waitingLabel(tool: ToolBlock): string {
  switch (tool.name) {
    case "Bash":
      return "等待命令执行完成";
    case "Task":
    case "Agent":
      return "等待子代理完成";
    case "WebFetch":
    case "WebSearch":
      return "等待网页返回";
    case "Monitor":
      return "等待监视条件";
    default:
      return "等待工具执行完成";
  }
}
