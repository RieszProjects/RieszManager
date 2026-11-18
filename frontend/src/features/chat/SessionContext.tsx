import { createContext, useContext } from "react";
import type { HarnessCapabilities, TaskInfo } from "@/api/types";

export interface SessionContextValue {
  sessionId: string;
  cwd: string | null;
  projectId: string | null;
  /** Tool calls that are really still running (others without a result were cut off). */
  pendingTools: Set<string>;
  /** Tasks (subagents, background commands) of the session's claude process, when it runs here. */
  tasks: TaskInfo[];
  /** The session's claude process runs here, so its tasks can be stopped or backgrounded. */
  hosted: boolean;
  /** Sessions of other harnesses: which one, and what it offers. */
  harness?: string | null;
  capabilities?: HarnessCapabilities | null;
}

export const SessionContext = createContext<SessionContextValue>({
  sessionId: "", cwd: null, projectId: null, pendingTools: new Set(), tasks: [], hosted: false,
});

export const useSessionContext = () => useContext(SessionContext);
