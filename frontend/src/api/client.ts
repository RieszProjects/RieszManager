/** Thin fetch wrapper for the JSON API (CSRF header, error normalisation). */

export class ApiError extends Error {
  status: number;
  code?: string;
  data: Record<string, unknown>;

  constructor(status: number, message: string, code?: string, data: Record<string, unknown> = {}) {
    super(message);
    this.status = status;
    this.code = code;
    this.data = data;
  }
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler): void {
  onUnauthorized = handler;
}

function csrfToken(): string {
  const match = document.cookie.match(/(?:^|;\s*)riesz_csrf=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

async function parse(resp: Response): Promise<unknown> {
  const text = await resp.text();
  if (!text) return {};
  try {
    return JSON.parse(text);
  } catch {
    return { error: text.slice(0, 300) };
  }
}

export async function api<T = unknown>(
  path: string,
  options: { method?: string; body?: unknown; query?: Record<string, string | number | undefined | null>; signal?: AbortSignal } = {},
): Promise<T> {
  const url = new URL(`/api/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(options.query ?? {})) {
    if (v !== undefined && v !== null && v !== "") url.searchParams.set(k, String(v));
  }
  const method = options.method ?? (options.body !== undefined ? "POST" : "GET");
  const headers: Record<string, string> = { Accept: "application/json" };
  let body: BodyInit | undefined;
  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers["Content-Type"] = "application/json";
    body = JSON.stringify(options.body);
  }
  if (method !== "GET") headers["X-CSRFToken"] = csrfToken();
  let resp: Response;
  try {
    resp = await fetch(url, { method, headers, body, credentials: "same-origin", signal: options.signal });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    throw new ApiError(0, "网络连接失败");
  }
  const data = (await parse(resp)) as Record<string, unknown>;
  if (!resp.ok) {
    if (resp.status === 401 && data.code === "unauthenticated") onUnauthorized?.();
    const message = typeof data.error === "string" ? data.error : `请求失败（${resp.status}）`;
    throw new ApiError(resp.status, message, data.code as string | undefined, data);
  }
  return data as T;
}

export function mediaUrl(path: string, query: Record<string, string> = {}): string {
  const url = new URL(`/api/${path}`, window.location.origin);
  for (const [k, v] of Object.entries(query)) url.searchParams.set(k, v);
  return url.pathname + url.search;
}

export function imageUrl(sessionId: string, ref: string): string {
  return mediaUrl(`sessions/${sessionId}/image`, { ref });
}

export function fileRawUrl(project: string, path: string, download = false): string {
  const q: Record<string, string> = { project, path };
  if (download) {
    q.download = "1";
    q.name = path.split("/").pop() ?? "file";
  }
  return mediaUrl("files/raw", q);
}
