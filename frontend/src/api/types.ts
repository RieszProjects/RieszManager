/** Shapes of the data exchanged with the backend (mirrors the runner's JSON). */

export interface Project {
  id: string;
  name: string;
  path: string;
  external: boolean;
  isRoot: boolean;
  exists: boolean;
  isGit: boolean;
  filesAllowed: boolean;
  mtime: number | null;
  sessionCount: number;
  lastActivity: number | null;
  liveCount: number;
  git?: { branch: string | null; changes: Record<string, string> } | null;
}

export interface LiveInfo {
  hosted: boolean;
  busy: boolean;
  pid: number;
  waiting?: boolean;
  kind?: string | null;
  entrypoint?: string | null;
  status?: string | null;
  held?: number;
}

export interface SessionSummary {
  sessionId: string;
  cwd: string | null;
  gitBranch: string | null;
  title: string | null;
  customTitle: string | null;
  firstPrompt: string | null;
  lastPrompt: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  promptCount: number;
  model: string | null;
  effort?: string | null;
  permissionMode: string | null;
  project: string | null;
  live: LiveInfo | null;
  mtime: number;
  size: number;
  /** The user asked for this session's claude process to stay running. */
  keepAlive?: boolean;
}

export interface LiveProcess {
  pid: number;
  sessionId: string;
  cwd: string | null;
  kind: string | null;
  entrypoint: string | null;
  status: string | null;
  name: string | null;
  hosted: boolean;
}

export interface ImageRef {
  ref: string;
  mediaType: string;
}

export interface ToolResult {
  text: string;
  truncated: boolean;
  isError: boolean;
  images: ImageRef[];
  structured?: Record<string, unknown> | null;
  ts?: string;
}

export type AssistantBlock =
  | { type: "text"; text: string; truncated?: boolean }
  | { type: "thinking"; text: string; redacted: boolean; durationMs?: number | null }
  | {
      type: "tool";
      id: string;
      name: string;
      server?: boolean;
      input: Record<string, unknown>;
      result: ToolResult | null;
    };

export type ToolBlock = Extract<AssistantBlock, { type: "tool" }>;

interface ItemBase {
  key: string;
  seq: number;
  ts: string | null;
  uuid: string | null;
}

export interface UserItem extends ItemBase {
  kind: "user";
  text: string;
  truncated?: boolean;
  images: ImageRef[];
  origin: string | null;
  peerName?: string;
  queued?: boolean;
}

export interface AssistantItem extends ItemBase {
  kind: "assistant";
  msgId: string | null;
  model: string | null;
  synthetic: boolean;
  blocks: AssistantBlock[];
  stopReason: string | null;
  isError?: boolean;
  /** Token usage reported by the API for this message. */
  usage?: {
    input_tokens?: number | null;
    output_tokens?: number | null;
    cache_read_input_tokens?: number | null;
    cache_creation_input_tokens?: number | null;
  };
}

export interface CommandItem extends ItemBase { kind: "command"; name: string; args: string }
export interface OutputItem extends ItemBase { kind: "output"; text: string; isError: boolean; truncated?: boolean }
export interface BashItem extends ItemBase { kind: "bash"; command: string; stdout: string; stderr: string }
export interface NoticeItem extends ItemBase { kind: "notice"; subtype: string | null; level: string; text: string }
export interface CompactItem extends ItemBase {
  kind: "compact";
  summary: string | null;
  trigger?: string | null;
  preTokens?: number | null;
  /** Context size right after compaction (recent Claude Code versions only). */
  postTokens?: number | null;
}
export interface InterruptedItem extends ItemBase { kind: "interrupted"; forTool?: boolean }
export interface TaskItem extends ItemBase { kind: "task"; taskId: string; status: string; summary: string }

export type Item =
  | UserItem
  | AssistantItem
  | CommandItem
  | OutputItem
  | BashItem
  | NoticeItem
  | CompactItem
  | InterruptedItem
  | TaskItem;

export interface QueueEntry {
  uuid: string;
  preview: string;
  state: string;
  ts: number;
}

export interface PermissionSuggestion {
  type: string;
  mode?: string;
  destination?: string;
  rules?: { toolName: string; ruleContent?: string }[];
  behavior?: string;
  directories?: string[];
}

export interface PermissionRequest {
  requestId: string;
  toolName: string;
  displayName?: string | null;
  input: Record<string, unknown>;
  toolUseId?: string;
  suggestions: PermissionSuggestion[];
  reason?: string | null;
  title?: string | null;
  description?: string | null;
  blockedPath?: string | null;
  suppressAlwaysAllow?: boolean | null;
  defaultToNo?: boolean | null;
  ts: number;
}

export interface PartialBlock {
  type: string;
  text: string;
  name?: string | null;
  id?: string | null;
}

export interface PartialMessage {
  msgId: string | null;
  blocks: Record<string, PartialBlock>;
}

export interface TurnResult {
  subtype: string;
  isError: boolean;
  durationMs: number | null;
  costUsd: number | null;
  numTurns: number | null;
  stopReason: string | null;
  ts: number;
}

export interface TaskInfo {
  id: string;
  description: string;
  type: string;
  background: boolean;
  toolUseId?: string | null;
  startedAt: number;
}

export interface SessionState {
  keepAlive?: boolean;
  hosted: boolean;
  alive: boolean;
  ready?: boolean;
  external?: boolean;
  pid?: number | null;
  busy: boolean;
  status?: string | null;
  model?: string | null;
  effort?: string | null;
  permissionMode?: string | null;
  queue: QueueEntry[];
  permissions: PermissionRequest[];
  tasks?: TaskInfo[];
  partial: PartialMessage | null;
  lastResult?: TurnResult | null;
  startedAt?: number;
  exitCode?: number | null;
  kind?: string | null;
  entrypoint?: string | null;
}

export interface SlashCommand {
  name: string;
  description: string;
  argumentHint?: string;
  builtin?: boolean | null;
}

export interface ModelInfo {
  value: string;
  resolvedModel?: string;
  displayName: string;
  description?: string;
  supportedEffortLevels?: string[];
}

export interface FileEntry {
  name: string;
  path: string;
  dir: boolean;
  size: number | null;
  mtime: number;
  heavy: boolean;
  symlink: boolean;
}

export interface FileInfo {
  path: string;
  name: string;
  size: number;
  mtime: number;
  mime: string | null;
  kind: "text" | "image" | "pdf" | "binary";
  content?: string;
  truncated?: boolean;
  language?: string;
  encodingIssues?: boolean;
}

export interface UploadMeta {
  id: string;
  name: string;
  originalName: string;
  mime: string;
  size: number;
}

export interface HistoryEntry {
  text: string;
  ts: number;
  sessionId: string | null;
}

export interface AppConfig {
  runner: boolean;
  workspaceRoot: string | null;
  allowBypass: boolean;
  defaultPermissionMode: string;
  transcription: boolean;
  username: string;
}

export interface AuthState {
  authenticated: boolean;
  pendingTotp: boolean;
  username: string | null;
  totpEnabled: boolean | null;
  totpSetupRequired: boolean;
}

/** Events pushed over the WebSocket. */
export type ServerEvent =
  | { ev: "live"; live: LiveProcess[] }
  | { ev: "sessions"; sessions: SessionSummary[]; removed: string[] }
  | { ev: "session_live"; sessionId: string; live: LiveInfo | null }
  | {
      ev: "snapshot";
      sessionId: string;
      items: Item[];
      hasMore: boolean;
      state: SessionState;
      session: SessionSummary | null;
    }
  | { ev: "reset"; sessionId: string; items: Item[]; hasMore: boolean }
  | { ev: "items"; sessionId: string; items: Item[] }
  | { ev: "state"; sessionId: string; state: SessionState }
  | {
      ev: "stream";
      sessionId: string;
      op: "start" | "block" | "delta" | "block_stop";
      msgId: string | null;
      index?: number;
      blockType?: string;
      name?: string | null;
      id?: string | null;
      text?: string;
    }
  | { ev: "result"; sessionId: string; result: TurnResult }
  | { ev: "permission"; sessionId: string; request: PermissionRequest }
  | { ev: "permission_resolved"; sessionId: string; requestId: string }
  | { ev: "exit"; sessionId: string; code: number; stderr: string[] }
  | { ev: "notice"; sessionId: string; level: string; text: string }
  | { ev: "forked"; sessionId: string; to: string }
  | { ev: "commands"; sessionId: string }
  | { ev: "rate_limit"; sessionId: string; info: unknown }
  | { ev: "usage_changed" }
  | { ev: "pong"; t: number }
  | { ev: "runner_unavailable" };
