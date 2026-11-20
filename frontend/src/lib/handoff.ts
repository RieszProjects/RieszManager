/** Moving a session to another harness (runner: hub.handoff): it continues there as the same conversation. */
import { api } from "@/api/client";
import { useUi } from "@/store/ui";

/** Hand the session over to `harness` and open the session that continues it. */
export async function handOff(sessionId: string, harness: string): Promise<void> {
  const res = await api<{ sessionId: string }>(`sessions/${sessionId}/handoff`, { body: { harness } });
  useUi.getState().navigate({ view: "session", sessionId: res.sessionId }, true);
}
