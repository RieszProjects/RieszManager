/** Non-modal, draggable, resizable in-page floating window for reader sticky notes. */
import { IconButton, Tooltip } from "@mui/material";
import { Copy, GripHorizontal, Maximize2, Minus, Plus, StickyNote, Trash2, X } from "lucide-react";
import { useEffect, useMemo, useRef, useState, type PointerEvent as ReactPointerEvent } from "react";
import { Markdown } from "@/components/Markdown";
import { copyText } from "@/lib/clipboard";
import { relativeTime } from "@/lib/format";
import { storage } from "@/lib/storage";
import { useIsMobile } from "@/lib/useIsMobile";
import { sessionTitle, useApp } from "@/store/app";
import { toast, useUi } from "@/store/ui";
import { notesForSession, useNotes, type Note, type NoteColor } from "./store";

interface Box {
  x: number;
  y: number;
  w: number;
  h: number;
}

const MIN_W = 290;
const MAX_W = 640;
const MIN_H = 220;
const DEFAULT_W = 368;
const DEFAULT_H = 440;
const COLORS: NoteColor[] = ["amber", "sage", "slate", "rose"];

function clampBox(b: Box): Box {
  const vw = window.innerWidth;
  const vh = window.innerHeight;
  const w = Math.max(MIN_W, Math.min(MAX_W, Math.min(b.w, vw - 24)));
  const h = Math.max(MIN_H, Math.min(Math.round(vh * 0.82), b.h));
  const x = Math.max(8, Math.min(vw - w - 8, b.x));
  const y = Math.max(8, Math.min(vh - 56, b.y));
  return { x, y, w, h };
}

function initialBox(): Box {
  const fallback: Box = {
    x: Math.max(16, window.innerWidth - DEFAULT_W - 22),
    y: 62,
    w: DEFAULT_W,
    h: DEFAULT_H,
  };
  const saved = storage.get<Box | null>("notesWin.v1", null);
  return clampBox(saved ?? fallback);
}

function NoteCard({
  note,
  showSession,
  highlighted,
}: {
  note: Note;
  showSession: boolean;
  highlighted: boolean;
}) {
  const sessions = useApp((s) => s.sessions);
  const navigate = useUi((s) => s.navigate);
  const remove = useNotes((s) => s.remove);
  const cardRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (highlighted) cardRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [highlighted]);

  const srcSession = note.sessionId ? sessions[note.sessionId] : undefined;
  const onCopy = async () => {
    const text = note.title ? `${note.title}\n\n${note.content}` : note.content;
    if (await copyText(text)) toast("已复制便签", "success");
  };
  const onDelete = () => {
    void remove(note.id).catch((err: Error) => toast(err.message, "error"));
  };

  return (
    <article ref={cardRef} className={`rz-note-card color-${note.color}${highlighted ? " focus" : ""}`}>
      <div className="rz-note-card-head">
        <div className="rz-note-card-meta">
          {note.title && <div className="rz-note-card-title">{note.title}</div>}
          <div className="rz-note-card-sub">
            <span>{relativeTime(note.updatedAt || note.createdAt)}</span>
            {showSession && note.sessionId && (
              <button
                type="button"
                className="rz-note-session"
                onClick={() => navigate({ view: "session", sessionId: note.sessionId })}
                title="跳转到来源会话"
              >
                {srcSession ? sessionTitle(srcSession) : note.sessionId.slice(0, 8)}
              </button>
            )}
          </div>
        </div>
        <div className="rz-note-card-actions">
          <Tooltip title="复制">
            <IconButton size="small" onClick={() => void onCopy()} aria-label="复制便签">
              <Copy size={13} />
            </IconButton>
          </Tooltip>
          <Tooltip title="移除便签">
            <IconButton size="small" onClick={onDelete} aria-label="移除便签">
              <Trash2 size={13} />
            </IconButton>
          </Tooltip>
        </div>
      </div>
      <div className="rz-note-card-body">
        <Markdown text={note.content} sans />
      </div>
    </article>
  );
}

export function NotesWindow() {
  const isMobile = useIsMobile();
  const route = useUi((s) => s.route);
  const sessions = useApp((s) => s.sessions);
  const items = useNotes((s) => s.items);
  const open = useNotes((s) => s.open);
  const minimized = useNotes((s) => s.minimized);
  const scope = useNotes((s) => s.scope);
  const focusNoteId = useNotes((s) => s.focusNoteId);
  const setOpen = useNotes((s) => s.setOpen);
  const setMinimized = useNotes((s) => s.setMinimized);
  const setScope = useNotes((s) => s.setScope);
  const clearFocus = useNotes((s) => s.clearFocus);
  const create = useNotes((s) => s.create);

  const [box, setBox] = useState<Box>(() => initialBox());
  const boxRef = useRef(box);
  boxRef.current = box;

  const [adding, setAdding] = useState(false);
  const [draftTitle, setDraftTitle] = useState("");
  const [draftContent, setDraftContent] = useState("");
  const [draftColor, setDraftColor] = useState<NoteColor>("amber");
  const [saving, setSaving] = useState(false);

  const sessionId = route.view === "session" ? route.sessionId : null;
  const sessionItems = useMemo(
    () => (sessionId ? notesForSession(items, sessions, sessionId) : []),
    [items, sessions, sessionId],
  );
  const effectiveScope: "session" | "all" = sessionId ? scope : "all";
  const visible = effectiveScope === "session" ? sessionItems : items;
  const showScopeTabs = Boolean(sessionId && (items.length !== sessionItems.length || sessionItems.length > 0));

  // If the highlighted note lives in another session, switch to "all" automatically so it is visible.
  useEffect(() => {
    if (!focusNoteId || !open) return;
    if (effectiveScope === "session" && !sessionItems.some((n) => n.id === focusNoteId) && items.some((n) => n.id === focusNoteId)) {
      setScope("all");
    }
    const timer = window.setTimeout(() => clearFocus(), 2600);
    return () => window.clearTimeout(timer);
  }, [focusNoteId, open, effectiveScope, sessionItems, items, setScope, clearFocus]);

  // Keep floating window inside viewport when the browser resizes.
  useEffect(() => {
    const onResize = () => setBox((cur) => clampBox(cur));
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  if (!open) return null;

  const startDrag = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (isMobile || e.button !== 0) return;
    if ((e.target as HTMLElement).closest("button, input, textarea")) return;
    e.preventDefault();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = boxRef.current;
    const onMove = (ev: PointerEvent) => {
      setBox(clampBox({ ...orig, x: orig.x + ev.clientX - startX, y: orig.y + ev.clientY - startY }));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      storage.set("notesWin.v1", boxRef.current);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const startResize = (e: ReactPointerEvent<HTMLDivElement>) => {
    if (isMobile || e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    const startX = e.clientX;
    const startY = e.clientY;
    const orig = boxRef.current;
    const onMove = (ev: PointerEvent) => {
      setBox(clampBox({ ...orig, w: orig.w + ev.clientX - startX, h: orig.h + ev.clientY - startY }));
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      storage.set("notesWin.v1", boxRef.current);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
  };

  const submitDraft = async () => {
    const text = draftContent.trim();
    if (!text || saving) return;
    setSaving(true);
    try {
      await create({
        sessionId: sessionId ?? "",
        title: draftTitle.trim(),
        content: text,
        color: draftColor,
      });
      setDraftTitle("");
      setDraftContent("");
      setAdding(false);
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setSaving(false);
    }
  };

  const style = isMobile
    ? undefined
    : { left: box.x, top: box.y, width: box.w, height: minimized ? "auto" : box.h };

  return (
    <aside
      className={`rz-notes-win${isMobile ? " mobile" : ""}${minimized ? " minimized" : ""}`}
      style={style}
      aria-label="便签浮动窗口"
    >
      <div className="rz-notes-head" onPointerDown={startDrag}>
        {!isMobile && <GripHorizontal size={14} className="rz-notes-grip" />}
        <StickyNote size={15} className="rz-notes-head-icon" />
        <span className="rz-notes-head-title">便签</span>
        {visible.length > 0 && <span className="rz-notes-head-count">{visible.length}</span>}
        <div className="rz-spacer" />
        {!minimized && (
          <Tooltip title={adding ? "取消新建" : "新建便签"}>
            <IconButton size="small" onClick={() => setAdding((v) => !v)} aria-label="新建便签" className={adding ? "active" : ""}>
              <Plus size={15} />
            </IconButton>
          </Tooltip>
        )}
        <Tooltip title={minimized ? "展开" : "收起"}>
          <IconButton size="small" onClick={() => setMinimized(!minimized)} aria-label={minimized ? "展开便签" : "收起便签"}>
            {minimized ? <Maximize2 size={14} /> : <Minus size={15} />}
          </IconButton>
        </Tooltip>
        <Tooltip title="关闭">
          <IconButton size="small" onClick={() => setOpen(false)} aria-label="关闭便签">
            <X size={15} />
          </IconButton>
        </Tooltip>
      </div>

      {!minimized && (
        <>
          {showScopeTabs && (
            <div className="rz-notes-tabs">
              <button
                type="button"
                className={effectiveScope === "session" ? "on" : ""}
                onClick={() => setScope("session")}
              >
                本会话{sessionItems.length > 0 ? ` (${sessionItems.length})` : ""}
              </button>
              <button
                type="button"
                className={effectiveScope === "all" ? "on" : ""}
                onClick={() => setScope("all")}
              >
                全部{items.length > 0 ? ` (${items.length})` : ""}
              </button>
            </div>
          )}

          {adding && (
            <div className="rz-note-form">
              <input
                className="rz-note-input"
                placeholder="标题（可选）"
                value={draftTitle}
                maxLength={80}
                onChange={(e) => setDraftTitle(e.target.value)}
              />
              <textarea
                className="rz-note-textarea"
                placeholder="记一点需要一直对照看的内容…"
                rows={3}
                value={draftContent}
                maxLength={4000}
                onChange={(e) => setDraftContent(e.target.value)}
                onKeyDown={(e) => {
                  if ((e.metaKey || e.ctrlKey) && e.key === "Enter") {
                    e.preventDefault();
                    void submitDraft();
                  }
                }}
                autoFocus
              />
              <div className="rz-note-form-foot">
                <div className="rz-note-colors">
                  {COLORS.map((c) => (
                    <button
                      key={c}
                      type="button"
                      className={`rz-note-color color-${c}${draftColor === c ? " on" : ""}`}
                      onClick={() => setDraftColor(c)}
                      aria-label={c}
                    />
                  ))}
                </div>
                <div className="rz-spacer" />
                <button type="button" className="rz-btn" onClick={() => setAdding(false)}>取消</button>
                <button type="button" className="rz-btn primary" disabled={saving || !draftContent.trim()} onClick={() => void submitDraft()}>
                  保存
                </button>
              </div>
            </div>
          )}

          <div className="rz-notes-body">
            {visible.length === 0 ? (
              <div className="rz-notes-empty dim">
                {effectiveScope === "session" && items.length > 0 ? "本会话暂无便签，可切到「全部」查看" : "暂无便签"}
              </div>
            ) : (
              visible.map((n) => (
                <NoteCard
                  key={n.id}
                  note={n}
                  showSession={effectiveScope === "all" && n.sessionId !== sessionId}
                  highlighted={n.id === focusNoteId}
                />
              ))
            )}
          </div>

          {!isMobile && <div className="rz-notes-resize" onPointerDown={startResize} aria-hidden="true" />}
        </>
      )}
    </aside>
  );
}
