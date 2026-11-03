/** Short-lived, isolated preview URLs for project files (see backend bridge/viewer.py). */
import { api } from "@/api/client";

const cache = new Map<string, { base: string; until: number }>();

/** URL that renders a project file as a web page in an isolated origin. */
export async function viewUrl(project: string, path: string): Promise<string> {
  let entry = cache.get(project);
  if (!entry || entry.until < Date.now()) {
    const res = await api<{ base: string; expiresIn: number }>("files/view-token", { query: { project } });
    entry = { base: res.base, until: Date.now() + Math.max(60, res.expiresIn - 300) * 1000 };
    cache.set(project, entry);
  }
  return entry.base + path.split("/").map(encodeURIComponent).join("/");
}

/** Sandbox flags for preview iframes: scripts run, but in an opaque origin (no allow-same-origin). */
export const PREVIEW_SANDBOX = "allow-scripts allow-forms allow-modals allow-popups allow-popups-to-escape-sandbox";
