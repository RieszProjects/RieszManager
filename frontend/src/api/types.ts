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
  /** The Agent tool calls whose subagents are running now. */
  agents?: string[];
}

export interface AutoResumeInfo {
  cause: string;
  label: string;
  message: string;
  nextAt: number | null;
  sentAt: number | null;
  since: number;
  attempts: number;
  resetsAt?: number | null;
}

export interface SessionSummary {
  sessionId: string;
  /** Sessions of other harnesses ("codex", "dsh"); absent for Claude Code. */
  harness?: string;
  /** The model API it runs on (an ApiRoute id such as "deepseek:key"); absent: the harness's own login. */
  api?: string | null;
  /** The session whose conversation this one continues in another harness. */
  handoffFrom?: string | null;
  /** This session's conversation goes on in that session (another harness): it is not listed and leads there. */
  handedOffTo?: string | null;
  /** Started by that session for a task (browser_task): listed among its subagents, not on its own. */
  subagentOf?: string | null;
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
  /** When the runner stopped while this session was working (reboot, crash); cleared once it is taken up again. */
  interrupted?: number | null;
  /** How many subagents the session has started. */
  subagents?: number;
  /** When one of its subagents last wrote anything (unix seconds). */
  subagentsActive?: number | null;
  /** Scheduled auto-resume after a recoverable turn failure (quota, billing, transient error). */
  autoResume?: AutoResumeInfo | null;
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
  /** Set on the first reply of a turn that a model other than the chosen one gave (a refusal fallback, a reroute). */
  modelNote?: string | null;
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
  /** Subagents: what they have done so far (reported while they run). */
  progress?: { tokens?: number | null; toolUses?: number | null; durationMs?: number | null; lastTool?: string | null };
}

/** What the web app may offer for a session of another harness (runner/riesz_runner/drivers/base.py). */
export interface HarnessCapabilities {
  steer: boolean;
  queue: boolean;
  interrupt: boolean;
  interruptRestarts: boolean;
  images: boolean;
  approveForSession: boolean;
  userQuestions: boolean;
  modelSwitch: string;
  effortSwitch: string;
  streamingText: boolean;
  backgroundTasks: boolean;
  rateLimits: boolean;
  history: boolean;
  /** A session can move to another model API and keep its history. */
  apiSwitch?: boolean;
}

/** A model API a harness can run on: a subscription it is logged in to, or an API key set in the settings. */
export interface ApiRoute {
  id: string;
  provider: string;
  auth: "subscription" | "key";
  label: string;
  available: boolean;
  reason: string | null;
  plan?: string | null;
  /** The models it offers (in session and command listings). */
  models?: ModelInfo[];
  error?: string;
}

export interface HarnessMode {
  id: string;
  name: string;
  description?: string;
}

export interface HarnessInfo {
  id: string;
  name: string;
  /** Short name for tags. */
  label?: string;
  /** The provider of its own route ("openai" for Codex). */
  nativeProvider?: string;
  /** Terminal command to continue a session, "{id}" for its id; null: none. */
  resumeCommand?: string | null;
  fork?: "native" | "copy";
  /** Its slash commands before a session exists (the universal ones it can do). */
  commands?: SlashCommand[];
  available: boolean;
  reason: string | null;
  /** Claude Code's entry has none of these: the composer knows its modes and models. */
  modes?: HarnessMode[];
  defaultMode?: string | null;
  defaultModel?: string | null;
  capabilities?: HarnessCapabilities;
  routes: ApiRoute[];
}

export interface ProviderKey {
  id: string;
  name: string;
  configured: boolean;
  source: "command" | "key" | null;
  command: string | null;
  updatedAt?: number | null;
}

export interface SubscriptionLogin {
  provider: string;
  label: string;
  loggedIn: boolean;
  plan: string | null;
}

export interface SessionState {
  /** Set for sessions of other harnesses, with what they offer. */
  harness?: string;
  modes?: HarnessMode[];
  capabilities?: HarnessCapabilities;
  api?: string | null;
  context?: { used: number; window: number | null } | null;
  keepAlive?: boolean;
  /** Scheduled auto-resume after a recoverable turn failure. */
  autoResume?: AutoResumeInfo | null;
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
  /** Other harnesses: the model they use when none is chosen, and its usual effort. */
  isDefault?: boolean;
  defaultEffort?: string | null;
}

export interface FileEntry {
  name: string;
  path: string;
  dir: boolean;
  size: number | null;
  mtime: number;
  heavy: boolean;
  symlink: boolean;
  /** Ignored by git (.gitignore and friends). */
  ignored?: boolean;
}

export interface FileInfo {
  path: string;
  name: string;
  size: number;
  mtime: number;
  mime: string | null;
  kind: "text" | "image" | "pdf" | "audio" | "binary";
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
  /** Live transcription (text while speaking) is available. */
  transcriptionLive?: boolean;
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
  /** A runner reload waiting until the sessions it lists are idle (pending false: none). */
  | { ev: "reload"; pending: boolean; since?: number | null; waitingFor?: string[] }
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
