/** Display metadata for tool calls: icon, label and a one-line argument summary. */
import {
  Bot, BookOpen, CircleHelp, ClipboardList, FilePen, FilePlus2, FileSearch, FileText, Globe, Hammer,
  ListChecks, type LucideIcon, Map as MapIcon, MonitorSmartphone, Plug, Search, Sparkles, SquareTerminal, Timer, Wrench,
} from "lucide-react";
import { shortPath } from "@/lib/format";

export interface ToolMeta {
  icon: LucideIcon;
  label: string;
  summary: string;
}

const str = (v: unknown): string => (typeof v === "string" ? v : v === undefined || v === null ? "" : JSON.stringify(v));

function firstLine(s: string, max = 140): string {
  const line = s.split("\n")[0];
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

export function mcpParts(name: string): { server: string; tool: string } | null {
  if (!name.startsWith("mcp__")) return null;
  const rest = name.slice(5);
  const idx = rest.lastIndexOf("__");
  if (idx < 0) return { server: rest, tool: rest };
  return { server: rest.slice(0, idx).replace(/_/g, " "), tool: rest.slice(idx + 2) };
}

export function toolMeta(name: string, input: Record<string, unknown>, cwd?: string | null): ToolMeta {
  const path = (key = "file_path") => shortPath(str(input[key]), cwd);
  switch (name) {
    case "Bash":
      return { icon: SquareTerminal, label: "命令", summary: firstLine(str(input.command)) };
    case "BashOutput":
    case "TaskOutput":
      return { icon: SquareTerminal, label: "读取输出", summary: str(input.bash_id ?? input.task_id) };
    case "KillShell":
    case "TaskStop":
      return { icon: SquareTerminal, label: "停止任务", summary: str(input.shell_id ?? input.task_id) };
    case "Read":
      return { icon: FileText, label: "读取", summary: path() };
    case "Write":
      return { icon: FilePlus2, label: "写入", summary: path() };
    case "Edit":
    case "MultiEdit":
      return { icon: FilePen, label: "编辑", summary: path() };
    case "NotebookEdit":
      return { icon: FilePen, label: "编辑笔记本", summary: path("notebook_path") };
    case "Grep":
      return { icon: Search, label: "搜索", summary: `${str(input.pattern)}${input.path ? `  · ${shortPath(str(input.path), cwd)}` : ""}` };
    case "Glob":
      return { icon: FileSearch, label: "查找文件", summary: str(input.pattern) };
    case "TodoWrite":
      return { icon: ListChecks, label: "任务清单", summary: "" };
    case "Task":
    case "Agent":
      return { icon: Bot, label: str(input.subagent_type) || "子代理", summary: str(input.description) };
    case "WebFetch":
      return { icon: Globe, label: "抓取网页", summary: str(input.url) };
    case "WebSearch":
      return { icon: Search, label: "网页搜索", summary: str(input.query) };
    case "AskUserQuestion":
      return { icon: CircleHelp, label: "提问", summary: firstLine(str((input.questions as { question?: string }[] | undefined)?.[0]?.question)) };
    case "ExitPlanMode":
      return { icon: MapIcon, label: "计划", summary: "" };
    case "EnterPlanMode":
      return { icon: MapIcon, label: "进入计划模式", summary: "" };
    case "Skill":
      return { icon: Sparkles, label: "技能", summary: str(input.skill ?? input.command) };
    case "ToolSearch":
      return { icon: Wrench, label: "加载工具", summary: str(input.query) };
    case "ScheduleWakeup":
    case "CronCreate":
      return { icon: Timer, label: "定时", summary: str(input.reason ?? input.schedule) };
    case "Monitor":
      return { icon: Timer, label: "监视", summary: str(input.description ?? input.command) };
    case "SendMessage":
      return { icon: Bot, label: "发送消息", summary: str(input.to) };
    case "ListAgents":
      return { icon: Bot, label: "列出会话", summary: "" };
    case "Workflow":
      return { icon: ClipboardList, label: "工作流", summary: str((input as { meta?: { name?: string } }).meta?.name ?? input.name) };
    case "Artifact":
      return { icon: BookOpen, label: "发布页面", summary: str(input.file_path ?? input.action) };
    default: {
      const mcp = mcpParts(name);
      if (mcp) {
        const icon = mcp.server.includes("chrome") ? MonitorSmartphone : Plug;
        const action = str(input.action);
        return { icon, label: mcp.tool, summary: action || firstLine(str(input.url ?? input.query ?? input.text ?? "")) };
      }
      return { icon: Hammer, label: name, summary: firstLine(str(Object.values(input)[0] ?? "")) };
    }
  }
}
