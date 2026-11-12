/**
 * The message input: slash commands, @file mentions, prompt history, attachments,
 * voice input, interrupting (Esc) and permission-mode cycling (Shift+Tab).
 */
import { IconButton, Tooltip } from "@mui/material";
import { ArrowUp, FileText, Image as ImageIcon, LoaderCircle, Mic, Paperclip, Square, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ClipboardEvent, type KeyboardEvent } from "react";
import { api } from "@/api/client";
import type { SessionState } from "@/api/types";
import { composerBus } from "@/lib/composerBus";
import { fileSize } from "@/lib/format";
import { storage } from "@/lib/storage";
import { useIsMobile } from "@/lib/useIsMobile";
import { uuid4 } from "@/lib/uuid";
import { useApp } from "@/store/app";
import { toast } from "@/store/ui";
import { ContextMeter } from "./ContextMeter";
import { useCommands } from "./useCommands";
import { cycleMode, ModelPicker, ModePicker } from "./SessionControls";
import { SuggestMenu, type Suggestion } from "./SuggestMenu";
import { useAttachments } from "./useAttachments";
import { usePromptHistory } from "./useHistory";
import { useVoiceInput } from "./useVoiceInput";

interface Props {
  sessionId: string | null;
  projectId: string | null;
  state: SessionState | null;
  defaultModel?: string | null;
  defaultMode?: string | null;
  onCreated?: (sessionId: string) => void;
  autoFocus?: boolean;
  placeholder?: string;
  /** Files dropped onto the surrounding view are forwarded here. */
  dropRef?: React.MutableRefObject<((files: File[]) => void) | null>;
}

const isCoarse = () => window.matchMedia?.("(pointer: coarse)").matches ?? false;

function slashToken(text: string, caret: number): string | null {
  if (!text.startsWith("/")) return null;
  const before = text.slice(0, caret);
  if (/\s/.test(before)) return null;
  return before.slice(1);
}

function mentionToken(text: string, caret: number): { query: string; start: number } | null {
  const before = text.slice(0, caret);
  const m = before.match(/(^|\s)@([^\s@]*)$/);
  if (!m) return null;
  return { query: m[2], start: caret - m[2].length - 1 };
}

export function Composer({ sessionId, projectId, state, defaultModel, defaultMode, onCreated, autoFocus, placeholder, dropRef }: Props) {
  const config = useApp((s) => s.config);
  const isMobile = useIsMobile();
  const draftKey = `draft:${sessionId ?? `new:${projectId ?? ""}`}`;
  const [text, setText] = useState(() => storage.get(draftKey, ""));
  const [caret, setCaret] = useState(0);
  const [sending, setSending] = useState(false);
  const [menuIndex, setMenuIndex] = useState(0);
  const [menuDismissed, setMenuDismissed] = useState(false);
  const [mentionResults, setMentionResults] = useState<Suggestion[]>([]);
  const [localModel, setLocalModel] = useState<string | null>(null);
  const [localMode, setLocalMode] = useState<string | null>(null);
  const [localEffort, setLocalEffort] = useState<string | null>(null);
  const [interim, setInterim] = useState("");
  const ta = useRef<HTMLTextAreaElement>(null);
  const attachments = useAttachments();
  const history = usePromptHistory(projectId);
  const { commands, models, defaults } = useCommands(sessionId, projectId);

  const hosted = Boolean(state?.hosted && state.alive);
  const external = Boolean(state?.external && state.alive && !hosted);
  const busy = Boolean(state?.busy);
  const model = (hosted ? state?.model : localModel ?? state?.model ?? defaultModel) ?? defaults?.model ?? null;
  const effort = (hosted ? state?.effort : localEffort ?? state?.effort) ?? defaults?.effort ?? null;
  const mode = (hosted ? state?.permissionMode : localMode ?? state?.permissionMode ?? defaultMode)
    ?? config?.defaultPermissionMode ?? "default";

  // Restore the draft when switching sessions.
  useEffect(() => {
    setText(storage.get(draftKey, ""));
    setLocalModel(null);
    setLocalMode(null);
    setLocalEffort(null);
    attachments.clear();
    history.reset();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [draftKey]);

  useEffect(() => {
    const t = window.setTimeout(() => (text ? storage.set(draftKey, text) : storage.remove(draftKey)), 300);
    return () => window.clearTimeout(t);
  }, [text, draftKey]);

  useEffect(() => {
    if (autoFocus && !isCoarse()) ta.current?.focus();
  }, [autoFocus, draftKey]);

  // Text sent from elsewhere on the page, e.g. the doll's quick phrases (see lib/composerBus).
  // It goes where the caret last was, or at the end; on its own line if the text before it is not empty.
  const textRef = useRef(text);
  textRef.current = text;
  const caretTouched = useRef(false);
  useEffect(() => composerBus.register((phrase) => {
    const el = ta.current;
    const cur = textRef.current;
    const at = el && caretTouched.current ? Math.min(el.selectionStart, cur.length) : cur.length;
    const before = cur.slice(0, at);
    const head = before + (before && !/\s$/.test(before) ? "\n" : "") + phrase;
    setText(head + cur.slice(at));
    requestAnimationFrame(() => {
      if (!el) return;
      if (!isCoarse()) el.focus();
      el.setSelectionRange(head.length, head.length);
      caretTouched.current = true;
      setCaret(head.length);
    });
  }), []);

  useLayoutEffect(() => {
    const el = ta.current;
    if (!el) return;
    el.style.height = "auto";
    const max = Math.max(160, window.innerHeight * 0.4);
    el.style.height = `${Math.min(el.scrollHeight, max)}px`;
    el.style.overflowY = el.scrollHeight > max ? "auto" : "hidden";
  }, [text, interim]);

  if (dropRef) dropRef.current = (files) => void attachments.add(files);

  // ---- suggestions -----------------------------------------------------------
  const slash = slashToken(text, caret);
  const mention = slash === null ? mentionToken(text, caret) : null;

  const slashItems = useMemo<Suggestion[]>(() => {
    if (slash === null) return [];
    const q = slash.toLowerCase();
    const list = commands
      .filter((c) => !c.name.startsWith("__"))
      .map((c) => ({ c, score: c.name.toLowerCase().startsWith(q) ? 0 : c.name.toLowerCase().includes(q) ? 1 : c.description.toLowerCase().includes(q) ? 2 : 9 }))
      .filter((x) => x.score < 9)
      .sort((a, b) => a.score - b.score || a.c.name.localeCompare(b.c.name));
    return list.slice(0, 60).map(({ c }) => ({
      key: c.name, label: `/${c.name}`, hint: c.argumentHint || undefined, description: c.description, kind: "command" as const,
    }));
  }, [slash, commands]);

  useEffect(() => {
    if (!mention || !projectId) {
      setMentionResults([]);
      return;
    }
    const ctrl = new AbortController();
    const t = window.setTimeout(async () => {
      try {
        const res = await api<{ results: { path: string; dir: boolean }[] }>("files/search", {
          query: { project: projectId, q: mention.query }, signal: ctrl.signal,
        });
        setMentionResults(res.results.map((r) => ({ key: r.path, label: r.path, kind: r.dir ? "dir" : "file" })));
      } catch {
        /* ignore */
      }
    }, 120);
    return () => {
      ctrl.abort();
      window.clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mention?.query, projectId]);

  const menu = menuDismissed ? null : slash !== null ? "slash" : mention ? "mention" : null;
  const menuItems = menu === "slash" ? slashItems : menu === "mention" ? mentionResults : [];
  useEffect(() => setMenuIndex(0), [menu, slash, mention?.query]);
  useEffect(() => setMenuDismissed(false), [text.startsWith("/"), mention?.start]);

  const replaceRange = (start: number, end: number, insert: string) => {
    const next = text.slice(0, start) + insert + text.slice(end);
    setText(next);
    const pos = start + insert.length;
    requestAnimationFrame(() => {
      ta.current?.setSelectionRange(pos, pos);
      setCaret(pos);
    });
    return next;
  };

  const pick = (s: Suggestion, submitIfComplete: boolean) => {
    if (menu === "slash") {
      const cmd = commands.find((c) => c.name === s.key);
      const insert = `/${s.key} `;
      const rest = text.slice(caret).replace(/^\S*/, "");
      const next = insert + rest.trimStart();
      if (submitIfComplete && !cmd?.argumentHint && !rest.trim()) {
        void submit(`/${s.key}`);
        return;
      }
      setText(next);
      requestAnimationFrame(() => {
        ta.current?.setSelectionRange(insert.length, insert.length);
        setCaret(insert.length);
      });
    } else if (menu === "mention" && mention) {
      replaceRange(mention.start, caret, `@${s.label}${s.kind === "dir" ? "" : " "}`);
    }
  };

  // ---- actions ---------------------------------------------------------------
  const interrupt = useCallback(async () => {
    if (!sessionId) return;
    try {
      await api(`sessions/${sessionId}/interrupt`, { body: {} });
    } catch (err) {
      toast((err as Error).message, "error");
    }
  }, [sessionId]);

  const changeMode = async (next: string) => {
    if (hosted && sessionId) {
      try {
        await api(`sessions/${sessionId}/mode`, { body: { mode: next } });
      } catch (err) {
        toast((err as Error).message, "error");
      }
    } else {
      setLocalMode(next);
    }
  };

  const changeModel = async (next: string) => {
    if (hosted && sessionId) {
      try {
        await api(`sessions/${sessionId}/model`, { body: { model: next } });
      } catch (err) {
        toast((err as Error).message, "error");
      }
    } else {
      setLocalModel(next);
    }
  };

  const changeEffort = async (next: string) => {
    if (hosted && sessionId) {
      try {
        await api(`sessions/${sessionId}/effort`, { body: { effort: next } });
      } catch (err) {
        toast((err as Error).message, "error");
      }
    } else {
      setLocalEffort(next);
    }
  };

  const submit = async (override?: string) => {
    const value = override ?? text;
    if (attachments.uploading) {
      toast("附件仍在上传", "warning");
      return;
    }
    if (!value.trim() && attachments.ids.length === 0) return;
    if (sending) return;
    setSending(true);
    const body: Record<string, unknown> = { text: value, attachments: attachments.ids, uuid: uuid4() };
    if (!hosted) {
      if (localModel) body.model = localModel;
      if (localMode) body.permissionMode = localMode;
      if (localEffort) body.effort = localEffort;
    }
    try {
      if (sessionId) {
        await api(`sessions/${sessionId}/messages`, { body });
      } else {
        if (!projectId) throw new Error("请先选择项目");
        const res = await api<{ sessionId: string }>("sessions", {
          body: {
            ...body, project: projectId, model: localModel ?? defaultModel ?? undefined,
            permissionMode: localMode ?? defaultMode ?? undefined, effort: localEffort ?? undefined,
          },
        });
        storage.remove(draftKey);
        onCreated?.(res.sessionId);
      }
      setText("");
      storage.remove(draftKey);
      attachments.clear();
      history.reset();
    } catch (err) {
      toast((err as Error).message, "error");
    } finally {
      setSending(false);
    }
  };

  const onKeyDown = async (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.nativeEvent.isComposing || e.keyCode === 229) return;
    if (menu && menuItems.length) {
      if (e.key === "ArrowDown" || (e.key === "n" && e.ctrlKey)) {
        e.preventDefault();
        setMenuIndex((i) => (i + 1) % menuItems.length);
        return;
      }
      if (e.key === "ArrowUp" || (e.key === "p" && e.ctrlKey)) {
        e.preventDefault();
        setMenuIndex((i) => (i - 1 + menuItems.length) % menuItems.length);
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey)) {
        e.preventDefault();
        pick(menuItems[menuIndex], e.key === "Enter");
        return;
      }
    }
    if (e.key === "Escape") {
      if (menu) {
        e.preventDefault();
        setMenuDismissed(true);
        return;
      }
      if (busy && hosted) {
        e.preventDefault();
        void interrupt();
      }
      return;
    }
    if (e.key === "Tab" && e.shiftKey) {
      e.preventDefault();
      void changeMode(cycleMode(mode, Boolean(config?.allowBypass)));
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.altKey && !isCoarse()) {
      e.preventDefault();
      void submit();
      return;
    }
    const el = e.currentTarget;
    if (e.key === "ArrowUp" && el.selectionStart === 0 && el.selectionEnd === 0) {
      const prev = await history.step(1, text);
      if (prev !== null) {
        e.preventDefault();
        setText(prev);
      }
      return;
    }
    if (e.key === "ArrowDown" && el.selectionStart === text.length) {
      const next = await history.step(-1, text);
      if (next !== null) {
        e.preventDefault();
        setText(next);
      }
    }
  };

  const onPaste = (e: ClipboardEvent<HTMLTextAreaElement>) => {
    const files = Array.from(e.clipboardData.files ?? []);
    if (files.length) {
      e.preventDefault();
      void attachments.add(files);
    }
  };

  const voice = useVoiceInput(Boolean(config?.transcription), (spoken, isInterim) => {
    if (isInterim) {
      setInterim(spoken);
      return;
    }
    setInterim("");
    const el = ta.current;
    const pos = el?.selectionStart ?? text.length;
    const sep = pos > 0 && !/\s$/.test(text.slice(0, pos)) ? " " : "";
    replaceRange(pos, el?.selectionEnd ?? pos, sep + spoken);
  });

  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  /** Phones and tablets: the plain file input opens the camera / file browser, so offer the photo library directly. */
  const photoButton = isMobile || isCoarse();
  const canSend = (text.trim().length > 0 || attachments.ids.length > 0) && !sending;
  const showStop = busy && hosted && !canSend;
  const defaultPlaceholder = external
    ? busy
      ? "终端里的会话正在工作，消息会在它空闲后送达"
      : "发送后，这个终端会话会转到这里继续"
    : busy
      ? "继续输入，消息会在合适时机送达…"
      : "给 Claude 发消息，/ 打开命令，@ 引用文件";

  const chips = (
    <>
      <ModePicker value={mode} allowBypass={Boolean(config?.allowBypass)} onChange={changeMode} />
      <ModelPicker value={model} effort={effort} models={models} onChange={changeModel} onEffort={changeEffort} />
    </>
  );

  return (
    <div className="rz-composer-wrap">
      {/* Phones: session controls sit above the box so the input row never wraps. */}
      {isMobile && <div className="rz-composer-chips">{chips}</div>}
      {menu && (menu === "slash" ? true : mentionResults.length > 0) && (
        <SuggestMenu
          items={menuItems}
          active={menuIndex}
          onPick={(s) => pick(s, true)}
          onHover={setMenuIndex}
          title={menu === "slash" ? "命令" : "文件"}
          empty={menu === "slash" ? "没有匹配的命令" : "没有匹配的文件"}
        />
      )}
      <div className={`rz-composer${voice.recording ? " recording" : ""}`}>
        {attachments.items.length > 0 && (
          <div className="rz-attachments">
            {attachments.items.map((a) => (
              <div key={a.localId} className={`rz-attachment ${a.status}`}>
                {a.previewUrl ? <img src={a.previewUrl} alt="" /> : <FileText size={18} />}
                <div className="rz-attachment-info">
                  <span className="clamp-1">{a.name}</span>
                  <span className="dim">{a.status === "uploading" ? "上传中…" : a.status === "error" ? "失败" : fileSize(a.size)}</span>
                </div>
                <button type="button" aria-label="移除" onClick={() => attachments.remove(a.localId)}><X size={13} /></button>
              </div>
            ))}
          </div>
        )}
        <textarea
          ref={ta}
          rows={1}
          value={interim ? `${text}${text && !/\s$/.test(text) ? " " : ""}${interim}` : text}
          placeholder={placeholder ?? defaultPlaceholder}
          onChange={(e) => {
            setText(e.target.value);
            setCaret(e.target.selectionStart);
            caretTouched.current = true;
          }}
          onSelect={(e) => {
            setCaret(e.currentTarget.selectionStart);
            caretTouched.current = true;
          }}
          onKeyDown={onKeyDown}
          onPaste={onPaste}
          enterKeyHint={isCoarse() ? "enter" : "send"}
          spellCheck={false}
        />
        <div className="rz-composer-bar">
          <Tooltip title="添加图片或文件">
            <IconButton size="small" aria-label="添加文件" onClick={() => fileInput.current?.click()}><Paperclip size={17} /></IconButton>
          </Tooltip>
          {photoButton && (
            <>
              <IconButton size="small" aria-label="从相册选择照片" onClick={() => photoInput.current?.click()}><ImageIcon size={17} /></IconButton>
              {/* accept="image/*" without `capture`: Android and iOS open the photo library instead of the file browser. */}
              <input
                ref={photoInput}
                type="file"
                accept="image/*"
                multiple
                hidden
                onChange={(e) => {
                  void attachments.add(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
              />
            </>
          )}
          <input
            ref={fileInput}
            type="file"
            multiple
            hidden
            onChange={(e) => {
              void attachments.add(Array.from(e.target.files ?? []));
              e.target.value = "";
            }}
          />
          {!isMobile && chips}
          <div className="rz-spacer" />
          {sessionId && <ContextMeter sessionId={sessionId} model={state?.model} hosted={hosted} />}
          {voice.available && (
            <Tooltip title={voice.recording ? "停止录音" : "语音输入"}>
              <IconButton size="small" className={voice.recording ? "rz-mic-on" : ""} onClick={() => void voice.toggle()} disabled={voice.transcribing}>
                {voice.transcribing ? <LoaderCircle size={17} className="spin" /> : <Mic size={17} />}
                {voice.recording && <span className="rz-mic-time">{voice.elapsed}s</span>}
              </IconButton>
            </Tooltip>
          )}
          {showStop ? (
            <Tooltip title="中断（Esc）">
              <IconButton className="rz-send stop" onClick={() => void interrupt()}><Square size={14} fill="currentColor" /></IconButton>
            </Tooltip>
          ) : (
            <Tooltip title={busy ? "加入队列" : "发送"}>
              <span>
                <IconButton className="rz-send" disabled={!canSend} onClick={() => void submit()}>
                  {sending ? <LoaderCircle size={17} className="spin" /> : <ArrowUp size={18} />}
                </IconButton>
              </span>
            </Tooltip>
          )}
        </div>
      </div>
    </div>
  );
}
