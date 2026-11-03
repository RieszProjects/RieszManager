/**
 * Reports to the server which session this page shows and whether someone is
 * actually using it (page visible, window focused, input within the last few
 * minutes). The server does not push notifications about a session that is
 * being looked at on any device.
 */
import { socket } from "@/api/socket";
import { useUi } from "@/store/ui";

const IDLE_AFTER_MS = 4 * 60 * 1000;
const REFRESH_MS = 30 * 1000;

let lastInput = Date.now();
let last = "";

function report(force = false): void {
  const route = useUi.getState().route;
  const sessionId = route.view === "session" ? route.sessionId : null;
  const active = document.visibilityState === "visible" && document.hasFocus() && Date.now() - lastInput < IDLE_AFTER_MS;
  const key = `${sessionId}|${active}`;
  if (!force && key === last) return;
  last = key;
  socket.setPresence(sessionId, active);
}

export function setupPresence(): () => void {
  const onInput = () => {
    const wasIdle = Date.now() - lastInput >= IDLE_AFTER_MS;
    lastInput = Date.now();
    if (wasIdle) report();
  };
  const onChange = () => report();
  const inputs = ["pointerdown", "keydown", "wheel", "touchstart"] as const;
  for (const t of inputs) window.addEventListener(t, onInput, { passive: true, capture: true });
  document.addEventListener("visibilitychange", onChange);
  window.addEventListener("focus", onChange);
  window.addEventListener("blur", onChange);
  const unsub = useUi.subscribe((s, prev) => s.route !== prev.route && report());
  // Refresh regularly: the server forgets reports that are not renewed.
  const timer = window.setInterval(() => report(true), REFRESH_MS);
  report(true);
  return () => {
    for (const t of inputs) window.removeEventListener(t, onInput, { capture: true });
    document.removeEventListener("visibilitychange", onChange);
    window.removeEventListener("focus", onChange);
    window.removeEventListener("blur", onChange);
    unsub();
    window.clearInterval(timer);
  };
}
