/** Formatting helpers for times, sizes and paths. */

export function relativeTime(input: string | number | null | undefined): string {
  if (input === null || input === undefined) return "";
  const ms = typeof input === "number" ? (input < 1e12 ? input * 1000 : input) : Date.parse(input);
  if (Number.isNaN(ms)) return "";
  const diff = (Date.now() - ms) / 1000;
  if (diff < 45) return "刚刚";
  if (diff < 3600) return `${Math.round(diff / 60)} 分钟前`;
  if (diff < 86400) return `${Math.round(diff / 3600)} 小时前`;
  if (diff < 86400 * 7) return `${Math.round(diff / 86400)} 天前`;
  const d = new Date(ms);
  const sameYear = d.getFullYear() === new Date().getFullYear();
  return d.toLocaleDateString("zh-CN", sameYear ? { month: "short", day: "numeric" } : { year: "numeric", month: "short", day: "numeric" });
}

export function clockTime(input: string | number | null | undefined): string {
  if (input === null || input === undefined) return "";
  const ms = typeof input === "number" ? (input < 1e12 ? input * 1000 : input) : Date.parse(input);
  if (Number.isNaN(ms)) return "";
  return new Date(ms).toLocaleString("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
}

export type DateBucket = "今天" | "昨天" | "近 7 天" | "近 30 天" | "更早";

export function dateBucket(epochSeconds: number): DateBucket {
  const now = new Date();
  const startOfToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;
  if (epochSeconds >= startOfToday) return "今天";
  if (epochSeconds >= startOfToday - 86400) return "昨天";
  if (epochSeconds >= startOfToday - 86400 * 6) return "近 7 天";
  if (epochSeconds >= startOfToday - 86400 * 29) return "近 30 天";
  return "更早";
}

export function fileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function duration(ms: number): string {
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${s % 60}s`;
  return `${Math.floor(m / 60)}h ${m % 60}m`;
}

export function shortPath(path: string, base?: string | null): string {
  if (base && (path === base || path.startsWith(base + "/"))) {
    const rel = path.slice(base.length + 1);
    return rel || ".";
  }
  return path.replace(/^\/home\/[^/]+/, "~");
}

export function basename(path: string): string {
  const parts = path.split("/").filter(Boolean);
  return parts[parts.length - 1] ?? path;
}

export function modelLabel(model: string | null | undefined): string {
  if (!model) return "默认模型";
  const m = model.toLowerCase();
  const pick = (name: string) => {
    const ver = m.match(/(\d+)[-.](\d+)/);
    return ver ? `${name} ${ver[1]}.${ver[2]}` : name;
  };
  if (m.includes("opus")) return pick("Opus");
  if (m.includes("sonnet")) return pick("Sonnet");
  if (m.includes("haiku")) return pick("Haiku");
  if (m.includes("fable")) return pick("Fable");
  return model;
}

export const PERMISSION_MODES: { value: string; label: string; hint: string }[] = [
  { value: "default", label: "逐项确认", hint: "每次编辑和命令都请求许可" },
  { value: "acceptEdits", label: "自动接受编辑", hint: "文件编辑自动通过，命令仍需确认" },
  { value: "plan", label: "计划模式", hint: "只读调研，先给出计划" },
  { value: "auto", label: "自动模式", hint: "由分类器判断风险，安全操作自动执行" },
  { value: "bypassPermissions", label: "跳过权限", hint: "不再询问任何权限（危险）" },
];

export function permissionLabel(mode: string | null | undefined): string {
  return PERMISSION_MODES.find((m) => m.value === mode)?.label ?? "逐项确认";
}
