/**
 * Single WebSocket connection to the backend event stream.
 * Reconnects with backoff and re-establishes subscriptions after reconnecting.
 */
import type { ServerEvent } from "./types";

type Listener = (event: ServerEvent) => void;

class EventSocket {
  private ws: WebSocket | null = null;
  private listeners = new Set<Listener>();
  private stateListeners = new Set<(state: "connecting" | "open" | "closed") => void>();
  private sessionRefs = new Map<string, number>();
  private wantGlobal = false;
  private retry = 0;
  private timer: number | null = null;
  private pingTimer: number | null = null;
  private stopped = true;

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
    if (this.pingTimer) window.clearInterval(this.pingTimer);
    this.ws?.close();
    this.ws = null;
  }

  private onVisible = () => {
    if (document.visibilityState === "visible" && (!this.ws || this.ws.readyState > WebSocket.OPEN)) {
      this.retry = 0;
      if (this.timer) window.clearTimeout(this.timer);
      this.connect();
    }
  };

  private connect(): void {
    if (this.stopped) return;
    this.emitState("connecting");
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const ws = new WebSocket(`${proto}//${window.location.host}/ws/events`);
    this.ws = ws;
    ws.onopen = () => {
      this.retry = 0;
      this.emitState("open");
      if (this.wantGlobal) this.send({ op: "sub_global" });
      for (const sid of this.sessionRefs.keys()) this.send({ op: "sub", sessionId: sid });
      if (this.pingTimer) window.clearInterval(this.pingTimer);
      this.pingTimer = window.setInterval(() => this.send({ op: "ping", t: Date.now() }), 25000);
    };
    ws.onmessage = (msg) => {
      let event: ServerEvent;
      try {
        event = JSON.parse(msg.data as string) as ServerEvent;
      } catch {
        return;
      }
      for (const l of this.listeners) l(event);
    };
    ws.onclose = (ev) => {
      if (this.ws !== ws) return;
      this.ws = null;
      if (this.pingTimer) window.clearInterval(this.pingTimer);
      this.emitState("closed");
      if (this.stopped || ev.code === 4401) return;
      const delay = Math.min(15000, 600 * 2 ** this.retry) + Math.random() * 400;
      this.retry += 1;
      this.timer = window.setTimeout(() => this.connect(), delay);
    };
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
