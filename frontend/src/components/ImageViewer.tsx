/**
 * Full-screen image viewer for images in a conversation.
 *
 * - Pinch (touch) or the wheel (mouse) zooms around the fingers/cursor; a
 *   double tap or double click toggles between fitted and 2.5×.
 * - Zoomed in, one finger or the mouse pans (kept within the image's edges).
 * - Not zoomed: swipe sideways for the previous/next image of the message,
 *   swipe down to close. A tap outside the image closes too.
 * - It is a MUI dialog, so Escape and the phone's Back gesture close it.
 */
import { Dialog, Fade, IconButton, Tooltip } from "@mui/material";
import { ChevronLeft, ChevronRight, Download, ExternalLink, X } from "lucide-react";
import { useCallback, useEffect, useRef, useState, type PointerEvent as ReactPointerEvent, type WheelEvent } from "react";
import { useIsMobile } from "@/lib/useIsMobile";
import { useUi } from "@/store/ui";

const MAX_SCALE = 6;
const DOUBLE_TAP_MS = 300;
const TAP_SLOP = 8;

interface View {
  s: number;
  x: number;
  y: number;
}

interface Point {
  x: number;
  y: number;
}

const FIT: View = { s: 1, x: 0, y: 0 };

export function ImageViewer() {
  const gallery = useUi((s) => s.gallery);
  const openGallery = useUi((s) => s.openGallery);
  const isMobile = useIsMobile();
  const [index, setIndex] = useState(0);
  const [view, setView] = useState<View>(FIT);
  const [swipe, setSwipe] = useState<Point | null>(null); // finger offset while swiping an unzoomed image
  const [animate, setAnimate] = useState(false);
  const stage = useRef<HTMLDivElement>(null);
  const img = useRef<HTMLImageElement>(null);
  const pointers = useRef(new Map<number, Point>());
  const gesture = useRef<{ view: View; start: Point; dist?: number; mid?: Point; moved: boolean; onImage: boolean; t: number } | null>(null);
  const lastTap = useRef<{ t: number; p: Point } | null>(null);
  const viewRef = useRef(view);
  viewRef.current = view;

  const urls = gallery?.urls ?? [];
  const url = urls[index];
  const close = useCallback(() => openGallery(null), [openGallery]);

  useEffect(() => {
    if (gallery) setIndex(gallery.index);
  }, [gallery]);
  useEffect(() => {
    setView(FIT);
    setSwipe(null);
  }, [index, gallery]);

  /** Position of a client point relative to the stage centre. */
  const local = (p: Point): Point => {
    const r = stage.current?.getBoundingClientRect();
    return r ? { x: p.x - r.left - r.width / 2, y: p.y - r.top - r.height / 2 } : p;
  };

  /** Keep the zoomed image covering the stage instead of sliding off it. */
  const clamp = (v: View): View => {
    const r = stage.current?.getBoundingClientRect();
    const el = img.current;
    if (!r || !el) return v;
    const s = Math.min(MAX_SCALE, Math.max(1, v.s));
    const maxX = Math.max(0, (el.clientWidth * s - r.width) / 2);
    const maxY = Math.max(0, (el.clientHeight * s - r.height) / 2);
    return { s, x: Math.min(maxX, Math.max(-maxX, v.x)), y: Math.min(maxY, Math.max(-maxY, v.y)) };
  };

  /** Zoom to `s`, keeping the image point under `at` (stage-centred) where it is. */
  const zoomAt = (from: View, s: number, at: Point, anchor: Point = at): View =>
    clamp({ s, x: at.x - ((anchor.x - from.x) * s) / from.s, y: at.y - ((anchor.y - from.y) * s) / from.s });

  const go = (step: number) => {
    if (urls.length < 2) return;
    setAnimate(false);
    setIndex((i) => (i + step + urls.length) % urls.length);
  };

  useEffect(() => {
    if (!gallery) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "ArrowLeft") go(-1);
      else if (e.key === "ArrowRight") go(1);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
    // go() only reads urls, which change with gallery
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [gallery, urls.length]);

  const onPointerDown = (e: ReactPointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    setAnimate(false);
    const pts = [...pointers.current.values()];
    const onImage = e.target === img.current;
    if (pts.length === 2) {
      const [a, b] = pts;
      gesture.current = {
        view: viewRef.current, start: pts[0], dist: Math.hypot(a.x - b.x, a.y - b.y),
        mid: local({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 }), moved: true, onImage, t: performance.now(),
      };
      setSwipe(null);
    } else if (pts.length === 1) {
      gesture.current = { view: viewRef.current, start: pts[0], moved: false, onImage, t: performance.now() };
    }
  };

  const onPointerMove = (e: ReactPointerEvent) => {
    if (!pointers.current.has(e.pointerId)) return;
    pointers.current.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const g = gesture.current;
    if (!g) return;
    const pts = [...pointers.current.values()];
    if (pts.length >= 2 && g.dist && g.mid) {
      const [a, b] = pts;
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      const mid = local({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
      const s = Math.min(MAX_SCALE, Math.max(1, (g.view.s * dist) / g.dist));
      setView(zoomAt(g.view, s, mid, g.mid));
      return;
    }
    const dx = e.clientX - g.start.x;
    const dy = e.clientY - g.start.y;
    if (!g.moved && Math.hypot(dx, dy) < TAP_SLOP) return;
    g.moved = true;
    if (g.view.s > 1) setView(clamp({ s: g.view.s, x: g.view.x + dx, y: g.view.y + dy }));
    else setSwipe({ x: dx, y: dy });
  };

  const onPointerUp = (e: ReactPointerEvent) => {
    if (!pointers.current.delete(e.pointerId)) return;
    const g = gesture.current;
    const remaining = [...pointers.current.values()];
    if (remaining.length === 1) {
      // Pinch ended with one finger still down: carry on panning from here.
      gesture.current = { view: viewRef.current, start: remaining[0], moved: true, onImage: false, t: performance.now() };
      return;
    }
    gesture.current = null;
    if (!g) return;
    setAnimate(true);
    if (swipe) {
      const { x, y } = swipe;
      setSwipe(null);
      if (Math.abs(y) > 110 && Math.abs(y) > Math.abs(x)) close();
      else if (Math.abs(x) > 70 && Math.abs(x) > Math.abs(y)) go(x < 0 ? 1 : -1);
      return;
    }
    if (g.moved) {
      if (viewRef.current.s < 1.03) setView(FIT);
      return;
    }
    // A tap: double tap toggles zoom; a single tap outside the image closes.
    const p = { x: e.clientX, y: e.clientY };
    const now = performance.now();
    const prev = lastTap.current;
    if (prev && now - prev.t < DOUBLE_TAP_MS && Math.hypot(prev.p.x - p.x, prev.p.y - p.y) < 30) {
      lastTap.current = null;
      setView(viewRef.current.s > 1 ? FIT : zoomAt(viewRef.current, 2.5, local(p)));
      return;
    }
    lastTap.current = { t: now, p };
    if (!g.onImage && viewRef.current.s === 1) window.setTimeout(() => lastTap.current?.t === now && close(), DOUBLE_TAP_MS);
  };

  const onWheel = (e: WheelEvent) => {
    setAnimate(false);
    const v = viewRef.current;
    const s = Math.min(MAX_SCALE, Math.max(1, v.s * Math.exp(-e.deltaY * 0.0015)));
    setView(s === 1 ? FIT : zoomAt(v, s, local({ x: e.clientX, y: e.clientY })));
  };

  const offset = swipe ?? { x: 0, y: 0 };
  const fade = swipe ? Math.max(0.35, 1 - Math.abs(swipe.y) / 400) : 1;
  const transform = `translate(${view.x + offset.x}px, ${view.y + offset.y}px) scale(${view.s})`;

  return (
    <Dialog
      open={Boolean(gallery && url)}
      onClose={close}
      fullScreen
      slots={{ transition: Fade }}
      slotProps={{ paper: { className: "rz-imgview", style: { backgroundColor: `rgba(8, 10, 9, ${fade})` } } }}
    >
      {url && (
        <>
          <div
            ref={stage}
            className="rz-imgview-stage"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerUp}
            onWheel={onWheel}
          >
            <img
              ref={img}
              key={url}
              src={url}
              alt=""
              draggable={false}
              className={animate ? "animate" : ""}
              style={{ transform, cursor: view.s > 1 ? "grab" : "zoom-in" }}
            />
          </div>
          <div className="rz-imgview-bar">
            {urls.length > 1 && <span className="rz-imgview-count">{index + 1} / {urls.length}</span>}
            <div className="rz-spacer" />
            <Tooltip title="下载">
              <IconButton component="a" href={url} download size="small" aria-label="下载"><Download size={18} /></IconButton>
            </Tooltip>
            {!isMobile && (
              <Tooltip title="在新标签页打开">
                <IconButton component="a" href={url} target="_blank" rel="noreferrer" size="small" aria-label="在新标签页打开"><ExternalLink size={17} /></IconButton>
              </Tooltip>
            )}
            <IconButton onClick={close} size="small" aria-label="关闭"><X size={20} /></IconButton>
          </div>
          {urls.length > 1 && !isMobile && (
            <>
              <IconButton className="rz-imgview-nav prev" onClick={() => go(-1)} aria-label="上一张"><ChevronLeft size={26} /></IconButton>
              <IconButton className="rz-imgview-nav next" onClick={() => go(1)} aria-label="下一张"><ChevronRight size={26} /></IconButton>
            </>
          )}
        </>
      )}
    </Dialog>
  );
}
