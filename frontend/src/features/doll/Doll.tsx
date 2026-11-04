/**
 * The doll widget: Lisi, the mascot, peeking into the window, which can be squeezed
 * and tells you how much of the Claude plan has been used.
 *
 * - Desktop: she leans in from a bottom corner. Drag her to the other corner.
 *   When the composer reaches into her corner she stands on top of it instead.
 * - Phone: only half of her head peeks in from a screen edge. Tap to bring her
 *   out (and see the usage), drag to move her along the edge or to the other side.
 * - Press and hold to squeeze. Right-click (desktop) or long-press (phone) for options.
 *
 * Everything lives in a fixed overlay that ignores pointer events except on her
 * silhouette, so the page underneath keeps working. Drawers and dialogs stack above her.
 */
import { ListItemIcon, ListItemText, Menu, MenuItem } from "@mui/material";
import { Check, EyeOff, MessageSquareText, Pencil, RefreshCw, Volume2, VolumeX } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties, type PointerEvent as ReactPointerEvent } from "react";
import { DRAGGING_CLASS } from "@/features/organize/SortableList";
import { composerBus } from "@/lib/composerBus";
import { storage } from "@/lib/storage";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";
import { ART, EXPRESSIONS, type Expression } from "./art";
import { SIZE_LABELS } from "./DollSettings";
import * as say from "./lines";
import { phraseLabel, usePhrases } from "./phrases";
import { PhrasesDialog } from "./PhrasesDialog";
import { useDollPrefs, type DollSize } from "./prefs";
import { squeak } from "./sound";
import { peakWindow, resetLabel, useUsage, windowName, type Usage } from "./usage";
import "./doll.css";

const HEIGHTS = { corner: { s: 150, m: 200, l: 260 }, side: { s: 100, m: 124, l: 156 } } as const;
const DRAG_THRESHOLD = 6;
const LONG_PRESS_MS = 900;
const SQUEEZE_MS = 320;
const SLEEP_AFTER_MS = 3 * 60 * 1000;
const BUBBLE_MS = { status: 7000, squish: 2600, waiting: 9000, warn: 9000, phrases: 15000 } as const;
/** Phones: fraction of her width hidden beyond the edge while she only peeks. */
const PEEK_HIDDEN = 0.42;

type BubbleKind = keyof typeof BUBBLE_MS;

interface Bubble {
  kind: BubbleKind;
  text: string;
  usage?: boolean;
  action?: { label: string; run: () => void };
  id: number;
}

interface Press {
  pointerId: number;
  x0: number;
  y0: number;
  t0: number;
  dragging: boolean;
  touch: boolean;
  /** The long press opened the menu; the release must not do anything else. */
  menu: boolean;
}

/** Swallow the click a touch screen fires after a long press, so it does not close the menu it opened. */
function swallowNextClick() {
  const swallow = (ev: Event) => {
    ev.stopPropagation();
    ev.preventDefault();
  };
  document.addEventListener("click", swallow, { capture: true, once: true });
  window.setTimeout(() => document.removeEventListener("click", swallow, true), 600);
}

let bubbleSeq = 0;

/** Phones: a finger covers her face while pressing, so instead she pulls a random face each time she comes out. */
const REACTIONS: Expression[] = ["happy", "squish", "sleep"];
let lastReaction: Expression | null = null;
function randomReaction(): Expression {
  const choices = REACTIONS.filter((e) => e !== lastReaction);
  lastReaction = choices[Math.floor(Math.random() * choices.length)];
  return lastReaction;
}

function UsageRows({ usage }: { usage: Usage | null }) {
  if (!usage) return <div className="rz-doll-usage dim">正在看用量……</div>;
  if (usage.plan === "api") return <div className="rz-doll-usage dim">这台机器用的是 API 计费，没有套餐额度。</div>;
  if (!usage.windows.length) return <div className="rz-doll-usage dim">没有拿到用量数据。</div>;
  return (
    <div className="rz-doll-usage">
      {usage.windows.map((w) => {
        const level = w.percent >= 90 ? "high" : w.percent >= 80 ? "mid" : "";
        const reset = resetLabel(w.resetsAt, w.resetText);
        return (
          <div key={w.label} className={`rz-doll-window ${level}`}>
            <div className="rz-doll-window-head">
              <span>{windowName(w)}</span>
              <span className="mono">{Math.round(w.percent)}%</span>
            </div>
            <div className="rz-doll-meter"><span style={{ width: `${Math.min(100, w.percent)}%` }} /></div>
            {reset && <div className="rz-doll-reset dim">{reset} 重置</div>}
          </div>
        );
      })}
    </div>
  );
}

/** Is the on-screen keyboard (probably) open? */
function useKeyboardOpen(enabled: boolean) {
  const [open, setOpen] = useState(false);
  useEffect(() => {
    if (!enabled) return;
    const vv = window.visualViewport;
    const check = () => {
      const el = document.activeElement;
      const typing = el instanceof HTMLTextAreaElement || (el instanceof HTMLInputElement && el.type !== "checkbox");
      setOpen(typing || Boolean(vv && vv.height < window.innerHeight * 0.75));
    };
    const later = () => window.setTimeout(check, 50);
    vv?.addEventListener("resize", check);
    document.addEventListener("focusin", check);
    document.addEventListener("focusout", later);
    return () => {
      vv?.removeEventListener("resize", check);
      document.removeEventListener("focusin", check);
      document.removeEventListener("focusout", later);
    };
  }, [enabled]);
  return open;
}

/** Bounding box of the composer, re-measured as the layout changes. */
function useComposerRect() {
  const [rect, setRect] = useState<DOMRect | null>(null);
  useEffect(() => {
    let last = "";
    const measure = () => {
      const el = document.querySelector(".rz-composer-wrap");
      const r = el?.getBoundingClientRect() ?? null;
      const key = r ? `${r.left|0},${r.top|0},${r.right|0},${r.bottom|0}` : "";
      if (key !== last) {
        last = key;
        setRect(r);
      }
    };
    measure();
    const timer = window.setInterval(measure, 700);
    window.addEventListener("resize", measure);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("resize", measure);
    };
  }, []);
  return rect;
}

export default function Doll() {
  const isMobile = useIsMobile();
  const pose = isMobile ? "side" : "corner";
  const art = ART[pose];
  const { sound, voice, size, side, y, update } = useDollPrefs();
  useEffect(() => {
    if (sound) squeak.preload(voice);
  }, [sound, voice]);
  const live = useApp((s) => s.live);
  const sessions = useApp((s) => s.sessions);
  const route = useUi((s) => s.route);
  const navigate = useUi((s) => s.navigate);
  const { usage, reload } = useUsage();

  const [pressed, setPressed] = useState(false);
  const [boing, setBoing] = useState(false);
  const [bubble, setBubble] = useState<Bubble | null>(null);
  const [out, setOut] = useState(false);
  const [flash, setFlash] = useState<Expression | null>(null);
  const [drag, setDrag] = useState<{ dx: number; dy: number } | null>(null);
  const [menu, setMenu] = useState<{ x: number; y: number } | null>(null);
  const [editingPhrases, setEditingPhrases] = useState(false);
  const phrases = usePhrases((s) => s.phrases);
  const [lastActive, setLastActive] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  const press = useRef<Press | null>(null);
  const longPress = useRef<number | undefined>(undefined);
  const bubbleTimer = useRef<number | undefined>(undefined);
  const hovering = useRef(false);
  const root = useRef<HTMLDivElement>(null);

  const height = HEIGHTS[pose][size];
  const width = Math.round((height * art.width) / art.height);
  const keyboard = useKeyboardOpen(isMobile);
  const composer = useComposerRect();

  // ---- what is going on ----------------------------------------------------
  const { busy, waiting } = useMemo(() => {
    const ids = Object.entries(live);
    return {
      busy: ids.some(([, l]) => l.busy),
      waiting: ids.filter(([, l]) => l.waiting).map(([sid]) => sid),
    };
  }, [live]);

  useEffect(() => {
    if (busy) setLastActive(Date.now());
  }, [busy]);
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 30000);
    return () => window.clearInterval(t);
  }, []);

  const peak = peakWindow(usage);
  const limited = Boolean(usage?.limited);
  const sleeping = !busy && waiting.length === 0 && now - lastActive > SLEEP_AFTER_MS;

  let expression: Expression = "normal";
  if (pressed && !isMobile) expression = "squish";
  else if (flash) expression = flash;
  else if (limited || (peak && peak.percent >= 90)) expression = "worried";
  else if (bubble?.kind === "status") expression = "happy";
  else if (waiting.length) expression = "normal";
  else if (busy) expression = "think";
  else if (sleeping) expression = "sleep";

  // ---- bubbles ---------------------------------------------------------------
  const showBubble = useCallback((b: Omit<Bubble, "id">) => {
    window.clearTimeout(bubbleTimer.current);
    setBubble({ ...b, id: ++bubbleSeq });
    const hide = () => {
      if (hovering.current) {
        bubbleTimer.current = window.setTimeout(hide, 1500);
        return;
      }
      setBubble(null);
      setOut(false);
      setFlash(null);
    };
    bubbleTimer.current = window.setTimeout(hide, BUBBLE_MS[b.kind]);
  }, []);

  const closeBubble = useCallback(() => {
    window.clearTimeout(bubbleTimer.current);
    setBubble(null);
    setOut(false);
    setFlash(null);
  }, []);

  useEffect(() => () => window.clearTimeout(bubbleTimer.current), []);

  // ---- quick phrases -----------------------------------------------------------
  const openPhrases = useCallback(() => {
    const show = () => {
      setOut(true);
      showBubble({ kind: "phrases", text: usePhrases.getState().phrases.length ? "要说什么？" : "还没有快捷短语。" });
    };
    if (usePhrases.getState().loaded) show();
    else usePhrases.getState().load().then(show, () => toast("快捷短语加载失败", "error"));
  }, [showBubble]);

  const pickPhrase = (text: string) => {
    if (composerBus.insert(text)) {
      closeBubble();
      return;
    }
    showBubble({ kind: "squish", text: "先打开一个会话，我才知道填到哪里。" });
  };

  // Close the bubble when tapping elsewhere.
  useEffect(() => {
    if (!bubble) return;
    const onDown = (e: PointerEvent) => {
      if (root.current && e.target instanceof Node && root.current.contains(e.target)) return;
      closeBubble();
    };
    document.addEventListener("pointerdown", onDown, true);
    return () => document.removeEventListener("pointerdown", onDown, true);
  }, [bubble, closeBubble]);

  // A session starts waiting for a permission answer.
  const seenWaiting = useRef<Set<string>>(new Set(waiting));
  useEffect(() => {
    const fresh = waiting.filter((sid) => !seenWaiting.current.has(sid));
    seenWaiting.current = new Set(waiting);
    const target = fresh.find((sid) => !(route.view === "session" && route.sessionId === sid));
    if (!target) return;
    setLastActive(Date.now());
    setOut(true);
    showBubble({
      kind: "waiting",
      text: say.waitingLine(waiting.length),
      action: { label: sessionTitle(sessions[target]), run: () => navigate({ view: "session", sessionId: target }) },
    });
  }, [waiting, route, sessions, navigate, showBubble]);

  // Usage crosses 80% / 95%: say so once per window and reset period.
  useEffect(() => {
    if (!usage) return;
    const warned = storage.get<Record<string, number>>("doll.warned", {});
    for (const w of usage.windows) {
      const level = w.percent >= 95 ? 95 : w.percent >= 80 ? 80 : 0;
      if (!level) continue;
      const key = `${w.label}|${w.resetsAt ?? w.resetText}|${level}`;
      if (warned[key]) continue;
      warned[key] = Date.now();
      // Forget keys older than two weeks.
      for (const [k, t] of Object.entries(warned)) if (Date.now() - t > 14 * 86400000) delete warned[k];
      storage.set("doll.warned", warned);
      setOut(true);
      showBubble({ kind: "warn", text: say.warnLine(w, windowName(w)), usage: true });
      break;
    }
  }, [usage, showBubble]);

  // ---- pointer handling ------------------------------------------------------
  const endPress = useCallback(() => {
    window.clearTimeout(longPress.current);
    document.body.classList.remove(DRAGGING_CLASS);
    press.current = null;
  }, []);

  useEffect(() => endPress, [endPress]);

  const onPointerDown = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (e.button !== 0 || press.current) return;
    e.currentTarget.setPointerCapture(e.pointerId);
    const touch = e.pointerType !== "mouse";
    // Keep the phone drawers from treating this as a swipe.
    if (touch) document.body.classList.add(DRAGGING_CLASS);
    press.current = { pointerId: e.pointerId, x0: e.clientX, y0: e.clientY, t0: performance.now(), dragging: false, touch, menu: false };
    setBoing(false);
    setPressed(true);
    setLastActive(Date.now());
    if (sound) squeak.press(voice);
    if (touch) {
      longPress.current = window.setTimeout(() => {
        const p = press.current;
        if (!p || p.dragging) return;
        p.menu = true;
        setPressed(false);
        navigator.vibrate?.(12);
        setMenu({ x: p.x0, y: p.y0 });
      }, LONG_PRESS_MS);
    }
  };

  const onPointerMove = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (!p || p.pointerId !== e.pointerId) return;
    const dx = e.clientX - p.x0;
    const dy = e.clientY - p.y0;
    if (p.menu || (!p.dragging && Math.hypot(dx, dy) < DRAG_THRESHOLD)) return;
    if (!p.dragging) {
      p.dragging = true;
      window.clearTimeout(longPress.current);
      setPressed(false);
      closeBubble();
    }
    setDrag({ dx, dy: isMobile ? dy : 0 });
  };

  const finishDrag = (clientX: number, dy: number) => {
    const nextSide = clientX < window.innerWidth / 2 ? "left" : "right";
    const patch: Partial<{ side: typeof side; y: number }> = {};
    if (nextSide !== side) patch.side = nextSide;
    if (isMobile) patch.y = clampY(y * window.innerHeight + dy) / window.innerHeight;
    update(patch);
    setDrag(null);
  };

  const onPointerUp = (e: ReactPointerEvent<HTMLDivElement>) => {
    const p = press.current;
    if (!p || p.pointerId !== e.pointerId) return;
    const held = performance.now() - p.t0;
    endPress();
    if (p.menu) {
      swallowNextClick();
      return;
    }
    if (p.dragging) {
      finishDrag(e.clientX, e.clientY - p.y0);
      return;
    }
    setPressed(false);
    setBoing(true);
    if (sound) squeak.release(voice);
    if (isMobile) setFlash(randomReaction());
    if (isMobile && !out) {
      setOut(true);
      showBubble({ kind: "status", text: say.statusLine({ peak, limited, sleeping, busy }), usage: true });
      void reload(true);
      return;
    }
    if (held >= SQUEEZE_MS) {
      showBubble({ kind: "squish", text: say.squishLine() });
      return;
    }
    if (bubble?.kind === "status") {
      closeBubble();
      return;
    }
    showBubble({ kind: "status", text: say.statusLine({ peak, limited, sleeping, busy }), usage: true });
    if (!usage || Date.now() / 1000 - usage.fetchedAt > 60) void reload(true);
  };

  const onPointerCancel = () => {
    const p = press.current;
    endPress();
    setPressed(false);
    if (p?.dragging) setDrag(null);
  };

  // ---- geometry --------------------------------------------------------------
  const topLimit = 64;
  const bottomLimit = () => {
    const floor = composer && composer.top > window.innerHeight / 3 ? composer.top : window.innerHeight;
    return floor - 12 - height;
  };
  function clampY(centre: number) {
    const top = centre - height / 2;
    return Math.max(topLimit, Math.min(bottomLimit(), top)) + height / 2;
  }

  const style: Record<string, string | number> = {
    "--doll-w": `${width}px`,
    "--doll-h": `${height}px`,
    "--doll-hull": art.hull,
  };
  if (isMobile) {
    const top = clampY(y * window.innerHeight) - height / 2 + (drag?.dy ?? 0);
    const sign = side === "right" ? 1 : -1;
    const shift = keyboard ? 1.1 : out || pressed || drag ? 0 : PEEK_HIDDEN;
    style.top = `${top}px`;
    style.transform = `translateX(${sign * shift * width + (drag?.dx ?? 0)}px)`;
  } else {
    // Stand on the composer when it reaches into her corner.
    let floor = 0;
    if (composer) {
      const intrudes = side === "right" ? composer.right > window.innerWidth - width : composer.left < width;
      if (intrudes && composer.top < window.innerHeight) floor = Math.max(0, window.innerHeight - composer.top + 4);
    }
    style.bottom = `${floor}px`;
    if (drag) style.transform = `translateX(${drag.dx}px)`;
  }

  const headStyle = { left: `${(side === "left" ? 1 - art.head.x : art.head.x) * 100}%`, top: `${art.head.y * 100}%` };

  const classes = [
    "rz-doll",
    isMobile ? "mobile" : "desktop",
    `side-${side}`,
    pressed && "pressed",
    drag && "dragging",
    expression === "sleep" && "sleeping",
    out && "out",
    keyboard && "away",
  ].filter(Boolean).join(" ");

  const sizeItem = (k: DollSize) => (
    <MenuItem key={k} dense onClick={() => { update({ size: k }); setMenu(null); }}>
      <ListItemIcon>{size === k ? <Check size={15} /> : null}</ListItemIcon>
      <ListItemText>{SIZE_LABELS[k]}号</ListItemText>
    </MenuItem>
  );

  return (
    <div ref={root} className={classes} style={style as CSSProperties} aria-hidden={keyboard || undefined}>
      {bubble && (
        <div
          key={bubble.id}
          className={`rz-doll-bubble ${bubble.kind}`}
          role="status"
          onPointerEnter={() => { hovering.current = true; }}
          onPointerLeave={() => { hovering.current = false; }}
        >
          <div className="rz-doll-line">{bubble.text}</div>
          {bubble.usage && <UsageRows usage={usage} />}
          {bubble.action && (
            <button type="button" className="rz-doll-action" onClick={() => { bubble.action?.run(); closeBubble(); }}>
              {bubble.action.label} →
            </button>
          )}
          {bubble.kind === "phrases" && (
            <>
              {phrases.length > 0 && (
                <div className="rz-doll-phrases">
                  {phrases.map((p) => (
                    <button key={p.id} type="button" className="rz-doll-phrase" title={p.text} onClick={() => pickPhrase(p.text)}>
                      {phraseLabel(p.text)}
                    </button>
                  ))}
                </div>
              )}
              <div className="rz-doll-foot">
                <button type="button" className="rz-doll-link" onClick={() => { closeBubble(); setEditingPhrases(true); }}>
                  <Pencil size={12} /> {phrases.length ? "编辑短语" : "添加短语"}
                </button>
              </div>
            </>
          )}
          {(bubble.kind === "status" || bubble.kind === "warn") && (
            <div className="rz-doll-foot">
              <button type="button" className="rz-doll-chip" onClick={openPhrases}>
                <MessageSquareText size={13} /> 快捷短语
              </button>
            </div>
          )}
        </div>
      )}
      <div className="rz-doll-enter">
      <div className="rz-doll-breath">
        <div className={`rz-doll-squish${boing ? " boing" : ""}`} onAnimationEnd={(e) => e.target === e.currentTarget && setBoing(false)}>
          <div className="rz-doll-art">
            {EXPRESSIONS.map((name) => (
              <img key={name} src={art.images[name]} alt="" draggable={false} className={name === expression ? "on" : ""} />
            ))}
            <div
              className="rz-doll-hit"
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={onPointerUp}
              onPointerCancel={onPointerCancel}
              onPointerEnter={() => { hovering.current = true; }}
              onPointerLeave={() => { hovering.current = false; }}
              onContextMenu={(e) => {
                e.preventDefault();
                endPress();
                setPressed(false);
                setMenu({ x: e.clientX, y: e.clientY });
              }}
            />
          </div>
          {waiting.length > 0 && !pressed && (
            <div className="rz-doll-alert" style={headStyle}>!</div>
          )}
          {expression === "sleep" && (
            <div className="rz-doll-zzz" style={headStyle}>
              <span>z</span><span>z</span><span>z</span>
            </div>
          )}
        </div>
      </div>
      </div>
      <Menu
        open={Boolean(menu)}
        onClose={() => setMenu(null)}
        anchorReference="anchorPosition"
        anchorPosition={menu ? { top: menu.y, left: menu.x } : undefined}
        slotProps={{ paper: { className: "rz-doll-menu" } }}
      >
        <MenuItem dense onClick={() => { update({ sound: !sound }); setMenu(null); }}>
          <ListItemIcon>{sound ? <Volume2 size={15} /> : <VolumeX size={15} />}</ListItemIcon>
          <ListItemText>{sound ? "关闭声音" : "打开声音"}</ListItemText>
        </MenuItem>
        <MenuItem dense onClick={() => { setMenu(null); openPhrases(); }}>
          <ListItemIcon><MessageSquareText size={15} /></ListItemIcon>
          <ListItemText>快捷短语</ListItemText>
        </MenuItem>
        {(Object.keys(SIZE_LABELS) as DollSize[]).map(sizeItem)}
        <MenuItem dense onClick={() => { setMenu(null); void reload(true); showBubble({ kind: "status", text: "刚看了一眼。", usage: true }); }}>
          <ListItemIcon><RefreshCw size={15} /></ListItemIcon>
          <ListItemText>刷新用量</ListItemText>
        </MenuItem>
        <MenuItem dense onClick={() => { setMenu(null); update({ enabled: false }); toast("莉思已收起，可以在 设置 → 通用 里重新打开"); }}>
          <ListItemIcon><EyeOff size={15} /></ListItemIcon>
          <ListItemText>收起莉思</ListItemText>
        </MenuItem>
      </Menu>
      <PhrasesDialog open={editingPhrases} onClose={() => setEditingPhrases(false)} />
    </div>
  );
}
