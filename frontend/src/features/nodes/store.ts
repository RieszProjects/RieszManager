/** State & API helpers for reverse-connected local computer nodes (`riesz-node`). */
import { create } from "zustand";
import { api } from "@/api/client";
import { socket } from "@/api/socket";
import { toast } from "@/store/ui";

export type NodeMode = "readonly" | "ask" | "auto" | "full";

export interface NodeBinding {
  sessionId: string;
  sessionTitle: string;
  harness: string;
  project: string;
  mode: NodeMode;
  dirs?: string[];
  boundAt: number;
}

export interface NodeCommand {
  id: string;
  nodeId: string;
  sessionId: string;
  sessionTitle: string;
  action: "exec" | "push" | "pull";
  command: string;
  cwd: string;
  risk: "low" | "medium" | "high" | string;
  riskReason: string;
  status: "running" | "done" | "error" | "killed" | "denied";
  startedAt: number;
  endedAt: number | null;
  exitCode: number | null;
  output?: string;
}

export interface NodeDevice {
  id: string;
  name: string;
  os: string;
  platform: string;
  hostname: string;
  ephemeral: boolean;
  revoked: boolean;
  createdAt: number | null;
  lastSeen: number | null;
  online: boolean;
  connectedAt: number | null;
  dirs: string[];
  maxMode: NodeMode | null;
  note: string;
  bindings: NodeBinding[];
  runningCommands: NodeCommand[];
  recentCommands: NodeCommand[];
}

export interface NodeApproval {
  id: string;
  nodeId: string;
  nodeName: string;
  sessionId: string;
  sessionTitle: string;
  action: "exec" | "push" | "pull";
  command: string;
  cwd: string;
  risk: string;
  reason: string;
  createdAt: number;
}

export interface NodePairing {
  code: string;
  label: string;
  ephemeral: boolean;
  createdAt: number;
  expiresAt: number;
}

export interface NodesSnapshot {
  devices: NodeDevice[];
  approvals: NodeApproval[];
  pairings: NodePairing[];
}

export const NODE_MODE_LABEL: Record<NodeMode, string> = {
  readonly: "只读",
  ask: "逐项确认",
  auto: "智能风控",
  full: "目录全放行",
};

interface NodesStore extends NodesSnapshot {
  loaded: boolean;
  open: boolean;
  downloadToken: string;
  downloadTokenPinned: boolean;
  setOpen: (open: boolean) => void;
  apply: (snap: Partial<NodesSnapshot>) => void;
  load: () => Promise<void>;
  loadDownloadToken: () => Promise<void>;
  rotateDownloadToken: () => Promise<void>;
  createPairing: (label: string, ephemeral: boolean) => Promise<NodePairing>;
  kick: (id: string) => Promise<void>;
  revoke: (id: string) => Promise<void>;
  remove: (id: string) => Promise<void>;
  bind: (nodeId: string, sessionId: string, mode: NodeMode) => Promise<void>;
  unbind: (nodeId: string, sessionId: string) => Promise<void>;
  resolveApproval: (approvalId: string, allow: boolean) => Promise<void>;
  killCommand: (nodeId: string, cmdId: string) => Promise<void>;
}

export const useNodes = create<NodesStore>((set, get) => ({
  devices: [],
  approvals: [],
  pairings: [],
  loaded: false,
  open: false,
  downloadToken: "",
  downloadTokenPinned: false,

  setOpen: (open) => {
    set({ open });
    if (open && !get().downloadToken) {
      void get().loadDownloadToken().catch(() => undefined);
    }
  },
  apply: (snap) =>
    set((s) => ({
      devices: snap.devices ?? s.devices,
      approvals: snap.approvals ?? s.approvals,
      pairings: snap.pairings ?? s.pairings,
      loaded: true,
    })),

  load: async () => {
    const res = await api<NodesSnapshot>("nodes");
    get().apply(res);
  },

  loadDownloadToken: async () => {
    const res = await api<{ token: string; pinned: boolean }>("nodes/download-token");
    set({ downloadToken: res.token || "", downloadTokenPinned: Boolean(res.pinned) });
  },

  rotateDownloadToken: async () => {
    const res = await api<{ token: string; pinned: boolean }>("nodes/download-token/rotate", { body: {} });
    set({ downloadToken: res.token || "", downloadTokenPinned: Boolean(res.pinned) });
    toast("已重置下载口令，旧链接立即失效", "success");
  },

  createPairing: async (label, ephemeral) => {
    const res = await api<NodesSnapshot & { pairing: NodePairing }>("nodes/pair", { body: { label, ephemeral } });
    get().apply(res);
    return res.pairing;
  },

  kick: async (id) => {
    const res = await api<NodesSnapshot>(`nodes/${id}/kick`, { body: {} });
    get().apply(res);
  },

  revoke: async (id) => {
    const res = await api<NodesSnapshot>(`nodes/${id}/revoke`, { body: {} });
    get().apply(res);
    toast("已吊销该设备密钥", "info");
  },

  remove: async (id) => {
    const res = await api<NodesSnapshot>(`nodes/${id}/delete`, { body: {} });
    get().apply(res);
  },

  bind: async (nodeId, sessionId, mode) => {
    const res = await api<NodesSnapshot>(`nodes/${nodeId}/bind`, { body: { sessionId, mode } });
    get().apply(res);
    toast("已绑定本地电脑节点", "success");
  },

  unbind: async (nodeId, sessionId) => {
    const res = await api<NodesSnapshot>(`nodes/${nodeId}/unbind`, { body: { sessionId } });
    get().apply(res);
    toast("已解除节点绑定", "info");
  },

  resolveApproval: async (approvalId, allow) => {
    const res = await api<NodesSnapshot>(`nodes/approvals/${approvalId}/resolve`, { body: { allow } });
    get().apply(res);
  },

  killCommand: async (nodeId, cmdId) => {
    await api(`nodes/${nodeId}/commands/${cmdId}/kill`, { body: {} });
    toast("已发送终止指令", "info");
  },
}));

let started = false;

export function startNodes(): void {
  if (started) return;
  started = true;
  const load = () => void useNodes.getState().load().catch(() => undefined);
  load();
  socket.onEvent((raw) => {
    const ev = raw as Partial<NodesSnapshot> & { ev?: string };
    if (ev.ev !== "nodes") return;
    useNodes.getState().apply(ev);
  });
}
