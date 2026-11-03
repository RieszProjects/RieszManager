import { createContext, useContext } from "react";

export interface SessionContextValue {
  sessionId: string;
  cwd: string | null;
  projectId: string | null;
  /** Tool calls that are really still running (others without a result were cut off). */
  pendingTools: Set<string>;
}

export const SessionContext = createContext<SessionContextValue>({
  sessionId: "", cwd: null, projectId: null, pendingTools: new Set(),
});

export const useSessionContext = () => useContext(SessionContext);
