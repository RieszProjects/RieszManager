/**
 * Single WebSocket connection to the backend event stream.
 * Reconnects with backoff and re-establishes subscriptions after reconnecting.
 */
import type { ServerEvent } from "./types";

type Listener = (event: ServerEvent) => void;

const PING_MS = 25000;
/** No message at all (not even a pong) for this long: the connection is dead even if it still says OPEN. */
const STALE_MS = 60000;
/** Back in the foreground after this long without a message: check with a ping before trusting the socket. */
const PROBE_AFTER_MS = 20000;
const PROBE_WAIT_MS = 5000;
/** A handshake nobody answers (a server that accepted the connection but hangs) is retried after this long. */
const CONNECT_TIMEOUT_MS = 15000;

class EventSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<(state: "connecting" | "open" | "closed") => void>();
  private sessionRefs = new Map<string, number>();
  private wantGlobal = false;
  private retry = 0;
  private timer: number | null = null;
  private pingTimer: number | null = null;
  private probeTimer: number | null = null;
  private lastSeen = 0;
  private stopped = true;
  /** Which session this page shows and whether it is in active use (see lib/presence). */
  private presence: { sessionId: string | null; active: boolean } | null = null;

  start(): void {
    if (!this.stopped) return;
    this.stopped = false;
    this.connect();
    document.addEventListener("visibilitychange", this.onVisible);
    window.addEventListener("online", this.onVisible);
  }

  stop(): void {
    this.stopped = true;
    document.removeEventListener("visibilitychange", this.onVisible);
    window.removeEventListener("online", this.onVisible);
    if (this.timer) window.clearTimeout(this.timer);
    this.clearTimers();
    this.ws?.close();
    this.ws = null;
  }

  private clearTimers(): void {
    if (this.pingTimer) window.clearInterval(this.pingTimer);
    if (this.probeTimer) window.clearTimeout(this.probeTimer);
    this.pingTimer = null;
    this.probeTimer = null;
  }

  private onVisible = () => {
    if (document.visibilityState !== "visible") return;
    const ws = this.ws;
    if (!ws || ws.readyState > WebSocket.OPEN) {
      this.retry = 0;
      if (this.timer) window.clearTimeout(this.timer);
      this.connect();
    } else if (ws.readyState === WebSocket.OPEN && Date.now() - this.lastSeen > PROBE_AFTER_MS) {
      // After the phone slept or the network changed the socket can stay OPEN without delivering anything.
      this.send({ op: "ping", t: Date.now() });
      if (this.probeTimer) window.clearTimeout(this.probeTimer);
      const sent = Date.now();
      this.probeTimer = window.setTimeout(() => {
        this.probeTimer = null;
        if (this.ws === ws && this.lastSeen < sent) {
          this.retry = 0;
          this.drop(ws);
        }
      }, PROBE_WAIT_MS);
    }
  };

  /** Give up on a connection that looks dead without waiting for the browser to notice. */
  private drop(ws: WebSocket): void {
    ws.onopen = null;
    ws.onmessage = null;
    ws.onclose = null;
    try {
      ws.close();
    } catch {
      /* already closing */
    }
    this.closed(ws, 1006);
  }

  private closed(ws: WebSocket, code: number): void {
    if (this.ws !== ws) return;
    this.ws = null;
    this.clearTimers();
    this.emitState("closed");
    if (this.stopped || code === 4401) return;
    const delay = Math.min(15000, 600 * 2 ** this.retry) + Math.random() * 400;
    this.retry += 1;
    if (this.timer) window.clearTimeout(this.timer);
    this.timer = window.setTimeout(() => this.connect(), delay);
  }

  private connect(): void {
    if (this.stopped) return;
    this.emitState("connecting");
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`);
    this.ws = ws;
    this.clearTimers();
    this.probeTimer = window.setTimeout(() => {
      this.probeTimer = null;
      if (this.ws === ws && ws.readyState === WebSocket.CONNECTING) this.drop(ws);
    }, CONNECT_TIMEOUT_MS);
    ws.onopen = () => {
      this.clearTimers();
      this.retry = 0;
      this.lastSeen = Date.now();
      this.emitState("open");
      if (this.wantGlobal) this.send({ op: "sub_global" });
      for (const sid of this.sessionRefs.keys()) this.send({ op: "sub", sessionId: sid });
      if (this.presence) this.send({ op: "presence", ...this.presence });
      this.pingTimer = window.setInterval(() => {
        if (Date.now() - this.lastSeen > STALE_MS) this.drop(ws);
        else this.send({ op: "ping", t: Date.now() });
      }, PING_MS);
    };
    ws.onmessage = (msg) => {
      this.lastSeen = Date.now();
      let event: ServerEvent;
      try {
        event = JSON.parse(msg.data as string) as ServerEvent;
      } catch {
        return;
      }
      for (const l of this.listeners) l(event);
    };
    ws.onclose = (ev) => this.closed(ws, ev.code);
  }

  private send(obj: unknown): void {
    if (this.ws?.readyState === WebSocket.OPEN) this.ws.send(JSON.stringify(obj));
  }

  private emitState(state: "connecting" | "open" | "closed"): void {
    for (const l of this.stateListeners) l(state);
  }

  onEvent(listener: Listener): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onState(listener: (state: "connecting" | "open" | "closed") => void): () => void {
    this.stateListeners.add(listener);
    return () => this.stateListeners.delete(listener);
  }

  /** Tell the server what this page shows, so it does not push notifications about it. */
  setPresence(sessionId: string | null, active: boolean): void {
    this.presence = { sessionId, active };
    this.send({ op: "presence", sessionId, active });
  }

  subscribeGlobal(): void {
    this.wantGlobal = true;
    this.send({ op: "sub_global" });
  }

  /** Reference-counted session subscription; returns an unsubscribe function. */
  subscribeSession(sessionId: string): () => void {
    const count = this.sessionRefs.get(sessionId) ?? 0;
    this.sessionRefs.set(sessionId, count + 1);
    // Always ask for a snapshot: the caller may have lost its local copy.
    this.send({ op: "sub", sessionId });
    return () => {
      const c = (this.sessionRefs.get(sessionId) ?? 1) - 1;
      if (c <= 0) {
        this.sessionRefs.delete(sessionId);
        this.send({ op: "unsub", sessionId });
      } else {
        this.sessionRefs.set(sessionId, c);
      }
    };
  }

  /** Force a fresh snapshot for a session (e.g. after a fork or a takeover). */
  resubscribe(sessionId: string): void {
    if (this.sessionRefs.has(sessionId)) this.send({ op: "sub", sessionId });
  }
}

export const socket = new EventSocket();
