/**
 * Phone navigation drawers driven by horizontal swipes anywhere on the screen.
 *
 * - Swipe right: open the left drawer (sessions). Swipe left: open the right
 *   drawer (project), if there is one.
 * - While a drawer is open, swiping towards its edge closes it (closing always
 *   wins over opening the other side).
 * - The drawer follows the finger; on release it settles open or closed based
 *   on how far and how fast it was dragged.
 * - Gestures starting inside something that can itself scroll horizontally
 *   (code blocks, tables, chip rows), or while a dialog/menu is open, are ignored.
 */
import { useEffect, useRef, type ReactNode } from "react";
import { DRAGGING_CLASS } from "../organize/SortableList";

type Side = "left" | "right";

interface Props {
  left: ReactNode;
  right: ReactNode;
  leftOpen: boolean;
  rightOpen: boolean;
  rightEnabled: boolean;
  onLeft: (open: boolean) => void;
  onRight: (open: boolean) => void;
}

const LOCK_DISTANCE = 10;
const HORIZONTAL_RATIO = 1.2;
const FLING_VELOCITY = 0.35; // px per ms

interface Gesture {
  x0: number;
  y0: number;
  target: EventTarget | null;
  locked: boolean;
  abandoned: boolean;
  side?: Side;
  closing?: boolean;
  width: number;
  progress: number;
  lastX: number;
  lastT: number;
  velocity: number;
}

function insideOverlay(target: EventTarget | null): boolean {
  if (document.querySelector(".MuiDialog-root, .MuiPopover-root")) return true;
  return target instanceof Element && Boolean(target.closest(".MuiModal-root"));
}

/** Can an element between `target` and the body scroll horizontally in the direction of `dx`? */
function nativeHorizontalScroll(target: EventTarget | null, dx: number): boolean {
  let el = target instanceof Element ? target : null;
  while (el && el !== document.body) {
    if (el instanceof HTMLElement && el.scrollWidth > el.clientWidth + 1) {
      const ox = getComputedStyle(el).overflowX;
      if (ox === "auto" || ox === "scroll") {
        if (dx > 0 && el.scrollLeft > 0) return true;
        if (dx < 0 && el.scrollLeft + el.clientWidth < el.scrollWidth - 1) return true;
      }
    }
    el = el.parentElement;
  }
  return false;
}

export function MobileDrawers({ left, right, leftOpen, rightOpen, rightEnabled, onLeft, onRight }: Props) {
  const leftEl = useRef<HTMLDivElement>(null);
  const rightEl = useRef<HTMLDivElement>(null);
  const backdropEl = useRef<HTMLDivElement>(null);
  const latest = useRef({ leftOpen, rightOpen, rightEnabled, onLeft, onRight });
  latest.current = { leftOpen, rightOpen, rightEnabled, onLeft, onRight };

  useEffect(() => {
    let g: Gesture | null = null;

    const panel = (side: Side) => (side === "left" ? leftEl.current : rightEl.current);

    const paint = (side: Side, progress: number) => {
      const el = panel(side);
      const backdrop = backdropEl.current;
      if (!el || !backdrop) return;
      const offset = (1 - progress) * 100 * (side === "left" ? -1 : 1);
      el.style.transform = `translateX(${offset}%)`;
      backdrop.style.opacity = String(progress);
    };

    const release = (side: Side) => {
      const el = panel(side);
      const backdrop = backdropEl.current;
      for (const node of [el, backdrop]) {
        if (!node) continue;
        node.style.transition = "";
        node.style.transform = "";
        node.style.opacity = "";
        node.style.visibility = "";
      }
    };

    const onStart = (e: TouchEvent) => {
      if (e.touches.length !== 1 || insideOverlay(e.target) || document.body.classList.contains(DRAGGING_CLASS)) {
        g = null;
        return;
      }
      const target = e.target;
      if (target instanceof HTMLTextAreaElement && document.activeElement === target) {
        g = null; // keep caret dragging inside the composer
        return;
      }
      const t = e.touches[0];
      g = {
        x0: t.clientX, y0: t.clientY, target, locked: false, abandoned: false,
        width: 1, progress: 0, lastX: t.clientX, lastT: performance.now(), velocity: 0,
      };
    };

    const onMove = (e: TouchEvent) => {
      if (!g || g.abandoned) return;
      if (document.body.classList.contains(DRAGGING_CLASS)) {
        if (g.locked && g.side) release(g.side);
        g = null;
        return;
      }
      const t = e.touches[0];
      const dx = t.clientX - g.x0;
      const dy = t.clientY - g.y0;
      if (!g.locked) {
        if (Math.abs(dx) < LOCK_DISTANCE && Math.abs(dy) < LOCK_DISTANCE) return;
        if (Math.abs(dx) < Math.abs(dy) * HORIZONTAL_RATIO) {
          g.abandoned = true;
          return;
        }
        const { leftOpen: lo, rightOpen: ro, rightEnabled: re } = latest.current;
        let side: Side | undefined;
        let closing = false;
        if (lo) {
          if (dx < 0) [side, closing] = ["left", true];
        } else if (ro) {
          if (dx > 0) [side, closing] = ["right", true];
        } else if (dx > 0) {
          side = "left";
        } else if (re) {
          side = "right";
        }
        const el = side ? panel(side) : null;
        if (!side || !el || nativeHorizontalScroll(g.target, dx)) {
          g.abandoned = true;
          return;
        }
        g.locked = true;
        g.side = side;
        g.closing = closing;
        g.width = el.getBoundingClientRect().width || 1;
        g.x0 = t.clientX - (dx > 0 ? 1 : -1) * LOCK_DISTANCE; // avoid a jump when the lock engages
        for (const node of [el, backdropEl.current]) {
          if (!node) continue;
          node.style.transition = "none";
          node.style.visibility = "visible";
        }
      }
      if (e.cancelable) e.preventDefault();
      const delta = t.clientX - g.x0;
      const towardsOpen = g.side === "left" ? delta : -delta;
      const progress = Math.max(0, Math.min(1, (g.closing ? 1 : 0) + towardsOpen / g.width));
      const now = performance.now();
      const dt = Math.max(1, now - g.lastT);
      const step = (g.side === "left" ? 1 : -1) * (t.clientX - g.lastX);
      g.velocity = 0.7 * g.velocity + 0.3 * (step / dt);
      g.lastX = t.clientX;
      g.lastT = now;
      g.progress = progress;
      paint(g.side!, progress);
    };

    const onEnd = (e: TouchEvent) => {
      const cur = g;
      g = null;
      if (!cur?.locked || !cur.side) return;
      const cancelled = e.type === "touchcancel";
      let open: boolean;
      if (cancelled) open = Boolean(cur.closing);
      else if (cur.velocity > FLING_VELOCITY) open = true;
      else if (cur.velocity < -FLING_VELOCITY) open = false;
      // Slow release: settle in the direction the finger was last moving, once past a third of the way.
      else if (cur.velocity > 0.02) open = cur.progress > 0.35;
      else if (cur.velocity < -0.02) open = cur.progress > 0.65;
      else open = cur.progress > 0.5;
      // Let CSS animate from the dragged position to the settled one.
      const side = cur.side;
      const el = panel(side);
      if (el) {
        el.style.transition = "";
        backdropEl.current && (backdropEl.current.style.transition = "");
        requestAnimationFrame(() => release(side));
      }
      const { onLeft: setLeft, onRight: setRight } = latest.current;
      if (side === "left") setLeft(open);
      else setRight(open);
    };

    document.addEventListener("touchstart", onStart, { passive: true });
    document.addEventListener("touchmove", onMove, { passive: false });
    document.addEventListener("touchend", onEnd);
    document.addEventListener("touchcancel", onEnd);
    return () => {
      document.removeEventListener("touchstart", onStart);
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("touchend", onEnd);
      document.removeEventListener("touchcancel", onEnd);
    };
  }, []);

  useEffect(() => {
    if (!leftOpen && !rightOpen) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      latest.current.onLeft(false);
      latest.current.onRight(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [leftOpen, rightOpen]);

  return (
    <>
      <div
        ref={backdropEl}
        className={`rz-mdrawer-backdrop${leftOpen || rightOpen ? " show" : ""}`}
        onClick={() => {
          onLeft(false);
          onRight(false);
        }}
      />
      <div ref={leftEl} className={`rz-mdrawer left${leftOpen ? " open" : ""}`} aria-hidden={!leftOpen}>
        {left}
      </div>
      <div ref={rightEl} className={`rz-mdrawer right${rightOpen ? " open" : ""}`} aria-hidden={!rightOpen}>
        {rightEnabled ? right : null}
      </div>
    </>
  );
}
