import { createContext, useContext } from "react";

export interface SessionContextValue {
  sessionId: string;
  cwd: string | null;
  projectId: string | null;
}

export const SessionContext = createContext<SessionContextValue>({ sessionId: "", cwd: null, projectId: null });

export const useSessionContext = () => useContext(SessionContext);
