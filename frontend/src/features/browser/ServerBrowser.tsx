/** Interactive viewer for the server's persistent Chrome (CDP screencast + direct input). */
import { Button, Dialog, IconButton, Tooltip } from "@mui/material";
import {
  ArrowLeft, ArrowRight, Globe, LoaderCircle, Plus, Power, RotateCw, SendHorizontal, X,
} from "lucide-react";
import {
  useCallback, useEffect, useRef, useState,
  type ClipboardEvent, type FormEvent, type KeyboardEvent, type MouseEvent, type WheelEvent,
} from "react";
import { api } from "@/api/client";
import { useIsMobile } from "@/lib/useIsMobile";
import { toast, useUi } from "@/store/ui";

interface BrowserTab {
  id: string;
  title: string;
  url: string;
}

interface BrowserStatus {
  enabled: boolean;
  available: boolean;
  running: boolean;
  mcpReady: boolean;
  viewport: { width: number; height: number };
  tabs: BrowserTab[];
  error?: string | null;
}

const SPECIAL_KEYS: Record<string, number> = {
  Backspace: 8, Tab: 9, Enter: 13, Escape: 27, PageUp: 33, PageDown: 34,
  End: 35, Home: 36, ArrowLeft: 37, ArrowUp: 38, ArrowRight: 39, ArrowDown: 40,
  Insert: 45, Delete: 46,
};

function modifiersFrom(e: { altKey: boolean; ctrlKey: boolean; metaKey: boolean; shiftKey: boolean }): number {
  return (e.altKey ? 1 : 0) | (e.ctrlKey ? 2 : 0) | (e.metaKey ? 4 : 0) | (e.shiftKey ? 8 : 0);
}

function mouseButton(btn: number): "left" | "middle" | "right" {
  if (btn === 2) return "right";
  if (btn === 1) return "middle";
  return "left";
}

function wsUrl(targetId: string): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws/browser?target=${encodeURIComponent(targetId)}`;
}

export function ServerBrowser() {
  const open = useUi((s) => s.browserOpen);
  const setOpen = useUi((s) => s.setBrowserOpen);
  const isMobile = useIsMobile();

  const [status, setStatus] = useState<BrowserStatus | null>(null);
  const [loading, setLoading] = useState(false);
  const [activeTab, setActiveTab] = useState<string | null>(null);
  const [urlInput, setUrlInput] = useState("");
  const [textInput, setTextInput] = useState("");
  const [frameSrc, setFrameSrc] = useState<string | null>(null);
  const [frameSize, setFrameSize] = useState({ width: 1280, height: 800 });
  const [wsState, setWsState] = useState<"idle" | "connecting" | "open" | "error">("idle");
  const [wsError, setWsError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const imgRef = useRef<HTMLImageElement>(null);
  const stageRef = useRef<HTMLDivElement>(null);
  const lastMoveRef = useRef(0);

  const send = useCallback((payload: Record<string, unknown>) => {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(payload));
  }, []);

  const refresh = useCallback(async (autoStart = false) => {
    setLoading(true);
    try {
      let res = await api<BrowserStatus>("browser/status");
      if (autoStart && res.available && !res.running) {
        res = await api<BrowserStatus>("browser/start", { body: {} });
      }
      setStatus(res);
      setActiveTab((cur) => {
        if (cur && res.tabs.some((t) => t.id === cur)) return cur;
        return res.tabs[0]?.id ?? null;
      });
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!open) return;
    void refresh(true);
    const timer = window.setInterval(() => {
      void api<BrowserStatus>("browser/status").then((res) => {
        setStatus(res);
        setActiveTab((cur) => (cur && res.tabs.some((t) => t.id === cur) ? cur : res.tabs[0]?.id ?? null));
      }).catch(() => undefined);
    }, 5000);
    return () => window.clearInterval(timer);
  }, [open, refresh]);

  // Connect CDP screencast stream for the active tab.
  useEffect(() => {
    if (!open || !status?.running || !activeTab) {
      wsRef.current?.close();
      wsRef.current = null;
      setFrameSrc(null);
      setWsState("idle");
      return;
    }
    setWsState("connecting");
    setWsError(null);
    const ws = new WebSocket(wsUrl(activeTab));
    wsRef.current = ws;

    ws.onopen = () => setWsState("open");
    ws.onmessage = (ev) => {
      try {
        const msg = JSON.parse(String(ev.data)) as Record<string, unknown>;
        if (msg.ev === "frame" && typeof msg.data === "string") {
          setFrameSrc(`data:image/jpeg;base64,${msg.data}`);
          if (typeof msg.width === "number" && typeof msg.height === "number") {
            setFrameSize({ width: msg.width, height: msg.height });
          }
        } else if (msg.ev === "ready") {
          if (typeof msg.url === "string") setUrlInput(msg.url);
        } else if (msg.ev === "nav" && typeof msg.url === "string") {
          setUrlInput(msg.url);
        } else if (msg.ev === "error" && typeof msg.message === "string") {
          setWsError(msg.message);
          setWsState("error");
        }
      } catch {
        /* ignore malformed frame */
      }
    };
    ws.onerror = () => setWsState("error");
    ws.onclose = () => {
      if (wsRef.current === ws) wsRef.current = null;
    };
    return () => {
      ws.close();
      if (wsRef.current === ws) wsRef.current = null;
    };
  }, [open, status?.running, activeTab]);

  const coords = (e: { clientX: number; clientY: number }): { x: number; y: number } | null => {
    const img = imgRef.current;
    if (!img) return null;
    const r = img.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) return null;
    const x = Math.round(((e.clientX - r.left) / r.width) * frameSize.width);
    const y = Math.round(((e.clientY - r.top) / r.height) * frameSize.height);
    if (x < 0 || y < 0 || x > frameSize.width || y > frameSize.height) return null;
    return { x, y };
  };

  const onMouseDown = (e: MouseEvent) => {
    e.preventDefault();
    stageRef.current?.focus();
    const p = coords(e);
    if (!p) return;
    send({ op: "mouse", type: "mousePressed", ...p, button: mouseButton(e.button), clickCount: Math.max(1, e.detail), modifiers: modifiersFrom(e) });
  };

  const onMouseUp = (e: MouseEvent) => {
    e.preventDefault();
    const p = coords(e);
    if (!p) return;
    send({ op: "mouse", type: "mouseReleased", ...p, button: mouseButton(e.button), clickCount: Math.max(1, e.detail), modifiers: modifiersFrom(e) });
  };

  const onMouseMove = (e: MouseEvent) => {
    const now = performance.now();
    if (now - lastMoveRef.current < 35) return;
    lastMoveRef.current = now;
    const p = coords(e);
    if (!p) return;
    send({ op: "mouse", type: "mouseMoved", ...p, modifiers: modifiersFrom(e) });
  };

  const onWheel = (e: WheelEvent) => {
    const p = coords(e);
    if (!p) return;
    send({ op: "mouse", type: "mouseWheel", ...p, deltaX: e.deltaX, deltaY: e.deltaY, modifiers: modifiersFrom(e) });
  };

  const onKeyDown = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.nativeEvent.isComposing || e.key === "Process") return;
    const mods = modifiersFrom(e);
    if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "v") return; // let onPaste handle clipboard text
    e.preventDefault();
    const vk = SPECIAL_KEYS[e.key];
    if (vk !== undefined) {
      send({ op: "key", type: "rawKeyDown", key: e.key, code: e.code, windowsVirtualKeyCode: vk, modifiers: mods });
      if (e.key === "Enter") send({ op: "key", type: "char", text: "\r", unmodifiedText: "\r", modifiers: mods });
      return;
    }
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
      send({ op: "text", text: e.key });
      return;
    }
    if (e.key.length === 1) {
      const code = e.key.toUpperCase().charCodeAt(0);
      send({ op: "key", type: "rawKeyDown", key: e.key, code: e.code, windowsVirtualKeyCode: code, modifiers: mods });
    }
  };

  const onKeyUp = (e: KeyboardEvent<HTMLDivElement>) => {
    const vk = SPECIAL_KEYS[e.key];
    if (vk !== undefined) {
      e.preventDefault();
      send({ op: "key", type: "keyUp", key: e.key, code: e.code, windowsVirtualKeyCode: vk, modifiers: modifiersFrom(e) });
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLDivElement>) => {
    const text = e.clipboardData.getData("text");
    if (text) {
      e.preventDefault();
      send({ op: "text", text });
    }
  };

  const submitUrl = (e: FormEvent) => {
    e.preventDefault();
    if (!urlInput.trim()) return;
    send({ op: "navigate", url: urlInput.trim() });
    stageRef.current?.focus();
  };

  const submitText = (e: FormEvent) => {
    e.preventDefault();
    if (!textInput) return;
    send({ op: "text", text: textInput });
    setTextInput("");
    stageRef.current?.focus();
  };

  const newTab = async () => {
    try {
      const res = await api<{ tab: BrowserTab; tabs: BrowserTab[] }>("browser/tabs", { body: { url: "about:blank" } });
      setStatus((s) => (s ? { ...s, running: true, tabs: res.tabs } : s));
      setActiveTab(res.tab.id);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const closeTab = async (tid: string) => {
    try {
      const res = await api<{ tabs: BrowserTab[] }>(`browser/tabs/${tid}/close`, { body: {} });
      setStatus((s) => (s ? { ...s, tabs: res.tabs } : s));
      if (activeTab === tid) setActiveTab(res.tabs[0]?.id ?? null);
    } catch (err) {
      toast((err as Error).message, "error");
    }
  };

  const togglePower = async () => {
    setLoading(true);
    try {
      const action = status?.running ? "stop" : "start";
      const res = await api<BrowserStatus>(`browser/${action}`, { body: {} });
      setStatus(res);
      setActiveTab(res.tabs[0]?.id ?? null);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setLoading(false);
    }
  };

  return (
    <Dialog
      open={open}
      onClose={() => setOpen(false)}
      fullScreen={isMobile}
      maxWidth={false}
      slotProps={{ paper: { className: "rz-browser-dialog" } }}
    >
      <div className="rz-browser-head">
        <div className="rz-browser-tabs" role="tablist">
          <Globe size={15} className="rz-browser-globe" />
          {(status?.tabs ?? []).map((t) => (
            <div key={t.id} className={`rz-browser-tab${t.id === activeTab ? " on" : ""}`}>
              <button
                type="button"
                className="rz-browser-tab-btn"
                title={t.url || t.title}
                onClick={() => {
                  setActiveTab(t.id);
                  setUrlInput(t.url);
                }}
              >
                <span className="clamp-1">{t.title || t.url || "新标签页"}</span>
              </button>
              <button type="button" className="rz-browser-tab-close" aria-label="关闭标签页" onClick={() => void closeTab(t.id)}>
                <X size={12} />
              </button>
            </div>
          ))}
          {status?.running && (
            <Tooltip title="新建标签页">
              <IconButton size="small" onClick={() => void newTab()}><Plus size={15} /></IconButton>
            </Tooltip>
          )}
        </div>
        <div className="rz-browser-head-actions">
          <Tooltip title={status?.running ? "关闭服务器浏览器" : "启动服务器浏览器"}>
            <IconButton size="small" disabled={loading || !status?.available} onClick={() => void togglePower()}>
              <Power size={15} />
            </IconButton>
          </Tooltip>
          <IconButton size="small" aria-label="关闭窗口" onClick={() => setOpen(false)}><X size={17} /></IconButton>
        </div>
      </div>

      {status?.running && (
        <div className="rz-browser-bar">
          <IconButton size="small" aria-label="后退" onClick={() => send({ op: "history", delta: -1 })}><ArrowLeft size={15} /></IconButton>
          <IconButton size="small" aria-label="前进" onClick={() => send({ op: "history", delta: 1 })}><ArrowRight size={15} /></IconButton>
          <IconButton size="small" aria-label="刷新" onClick={() => send({ op: "reload" })}><RotateCw size={14} /></IconButton>
          <form className="rz-browser-url" onSubmit={submitUrl}>
            <input
              type="text"
              value={urlInput}
              onChange={(e) => setUrlInput(e.target.value)}
              placeholder="输入网址…"
              spellCheck={false}
            />
          </form>
          <form className="rz-browser-ime" onSubmit={submitText}>
            <input
              type="text"
              value={textInput}
              onChange={(e) => setTextInput(e.target.value)}
              placeholder="输入文字或粘贴…"
            />
            <IconButton size="small" type="submit" disabled={!textInput} aria-label="发送到页面"><SendHorizontal size={14} /></IconButton>
          </form>
        </div>
      )}

      <div
        ref={stageRef}
        className="rz-browser-stage"
        tabIndex={0}
        onKeyDown={onKeyDown}
        onKeyUp={onKeyUp}
        onPaste={onPaste}
        onContextMenu={(e) => e.preventDefault()}
      >
        {!status || loading ? (
          <div className="rz-center"><LoaderCircle size={22} className="spin" /></div>
        ) : !status.available ? (
          <div className="rz-browser-empty">
            <div>服务器未检测到 Chrome / Chromium</div>
          </div>
        ) : !status.running ? (
          <div className="rz-browser-empty">
            <div>服务器浏览器未运行</div>
            {status.error && <div className="dim">{status.error}</div>}
            <Button variant="outlined" size="small" onClick={() => void togglePower()}>启动浏览器</Button>
          </div>
        ) : frameSrc ? (
          <img
            ref={imgRef}
            src={frameSrc}
            alt=""
            className="rz-browser-frame"
            draggable={false}
            onMouseDown={onMouseDown}
            onMouseUp={onMouseUp}
            onMouseMove={onMouseMove}
            onWheel={onWheel}
          />
        ) : (
          <div className="rz-browser-empty">
            {wsState === "error" ? (
              <>
                <div>{wsError || "连接画面失败"}</div>
                <Button variant="outlined" size="small" onClick={() => void refresh(true)}>重试</Button>
              </>
            ) : (
              <LoaderCircle size={22} className="spin" />
            )}
          </div>
        )}
      </div>
    </Dialog>
  );
}
