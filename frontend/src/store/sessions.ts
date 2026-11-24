/** Per-session live data: transcript items, process state and the streaming overlay. */
import { create } from "zustand";
import { api } from "@/api/client";
import type { Item, PartialBlock, ServerEvent, SessionState } from "@/api/types";

export interface OverlayBlock extends PartialBlock {
  index: number;
  done: boolean;
}

export interface StreamOverlay {
  msgId: string | null;
  blocks: OverlayBlock[];
}

export interface EphemeralNotice {
  id: number;
  level: string;
  text: string;
  ts: number;
}

export interface SessionData {
  items: Item[];
  hasMore: boolean;
  loaded: boolean;
  loadingOlder: boolean;
  /** Incremented whenever the server replaces the item list (reset or disjoint snapshot). */
  resetSeq: number;
  state: SessionState | null;
  stream: StreamOverlay | null;
  notices: EphemeralNotice[];
  turnStartedAt: number | null;
  exited: { code: number; stderr: string[] } | null;
}

const empty = (): SessionData => ({
  items: [],
  hasMore: false,
  loaded: false,
  loadingOlder: false,
  resetSeq: 0,
  state: null,
  stream: null,
  notices: [],
  turnStartedAt: null,
  exited: null,
});

/**
 * Reconcile a fresh `snapshot` with already-loaded items (e.g. after a WebSocket reconnect).
 * Preserves older items the user already had loaded before the snapshot window so the list's
 * front never shifts forward unexpectedly and yanks scroll upward.
 */
export function mergeSnapshot(
  current: Item[],
  incoming: Item[],
  curHasMore: boolean,
  inHasMore: boolean,
): { items: Item[]; hasMore: boolean; replaced: boolean } {
  if (!current.length) return { items: incoming, hasMore: inHasMore, replaced: false };
  if (!incoming.length) return { items: incoming, hasMore: inHasMore, replaced: true };
  const incomingKeys = new Set(incoming.map((it) => it.key));
  const overlaps = current.some((it) => incomingKeys.has(it.key));
  if (!overlaps) return { items: incoming, hasMore: inHasMore, replaced: true };
  const firstInSeq = incoming[0].seq;
  if (current[0].seq < firstInSeq) {
    const older = current.filter((it) => it.seq < firstInSeq && !incomingKeys.has(it.key));
    if (older.length) {
      return { items: [...older, ...incoming], hasMore: curHasMore, replaced: false };
    }
  }
  return { items: incoming, hasMore: inHasMore, replaced: false };
}

function mergeItems(current: Item[], incoming: Item[]): Item[] {
  if (incoming.length === 0) return current;
  const byKey = new Map<string, Item>();
  for (const it of current) byKey.set(it.key, it);
  let needsSort = false;
  const last = current.length ? current[current.length - 1].seq : -Infinity;
  for (const it of incoming) {
    if (!byKey.has(it.key) && it.seq < last) needsSort = true;
    byKey.set(it.key, it);
  }
  const merged = Array.from(byKey.values());
  if (needsSort || merged.length !== current.length + incoming.length) merged.sort((a, b) => a.seq - b.seq);
  return merged;
}

function overlayFromPartial(state: SessionState | null): StreamOverlay | null {
  const partial = state?.partial;
  if (!partial) return null;
  const blocks = Object.entries(partial.blocks)
    .map(([index, b]) => ({ ...b, index: Number(index), done: false }))
    .sort((a, b) => a.index - b.index);
  return { msgId: partial.msgId, blocks };
}

let noticeId = 0;

/** True if a streamed block is already present in the persisted assistant item. */
function represented(item: Item | undefined, block: OverlayBlock): boolean {
  if (!item || item.kind !== "assistant") return false;
  if (block.type === "tool_use" || block.type === "server_tool_use") {
    return item.blocks.some((b) => b.type === "tool" && b.id === block.id);
  }
  if (block.type === "text") {
    const t = block.text.trim();
    return item.blocks.some((b) => b.type === "text" && t.length > 0 && b.text.trim().startsWith(t.slice(0, 200)));
  }
  if (block.type === "thinking") return block.done && item.blocks.some((b) => b.type === "thinking");
  return block.done;
}

function pruneOverlay(stream: StreamOverlay | null, items: Item[]): StreamOverlay | null {
  if (!stream?.msgId) return stream;
  const key = `a:${stream.msgId}`;
  const item = items.find((it) => it.key === key);
  if (!item) return stream;
  const blocks = stream.blocks.filter((b) => !represented(item, b));
  if (blocks.length === stream.blocks.length) return stream;
  return blocks.length ? { ...stream, blocks } : null;
}

interface SessionsStore {
  data: Record<string, SessionData>;
  get: (sid: string) => SessionData;
  handleEvent: (event: ServerEvent) => void;
  loadOlder: (sid: string) => Promise<void>;
  setState: (sid: string, state: SessionState) => void;
  dismissNotice: (sid: string, id: number) => void;
}

export const useSessions = create<SessionsStore>((set, get) => {
  const update = (sid: string, fn: (d: SessionData) => Partial<SessionData>) =>
    set((s) => {
      const current = s.data[sid] ?? empty();
      return { data: { ...s.data, [sid]: { ...current, ...fn(current) } } };
    });

  return {
    data: {},
    get: (sid) => get().data[sid] ?? empty(),

    setState: (sid, state) =>
      update(sid, (d) => ({
        state,
        turnStartedAt: state.busy ? d.turnStartedAt ?? Date.now() : null,
        stream: state.busy ? d.stream : null,
      })),

    dismissNotice: (sid, id) => update(sid, (d) => ({ notices: d.notices.filter((n) => n.id !== id) })),

    loadOlder: async (sid) => {
      const d = get().get(sid);
      if (!d.hasMore || d.loadingOlder || d.items.length === 0) return;
      update(sid, () => ({ loadingOlder: true }));
      try {
        const res = await api<{ items: Item[]; hasMore: boolean }>(`sessions/${sid}/items`, {
          query: { before: d.items[0].seq, limit: 160 },
        });
        update(sid, (cur) => ({ items: mergeItems(cur.items, res.items), hasMore: res.hasMore, loadingOlder: false }));
      } catch {
        update(sid, () => ({ loadingOlder: false }));
      }
    },

    handleEvent: (event) => {
      if (!("sessionId" in event) || !event.sessionId) return;
      const sid = event.sessionId;
      switch (event.ev) {
        case "snapshot":
          update(sid, (d) => {
            const merged = mergeSnapshot(d.items, event.items, d.hasMore, event.hasMore);
            return {
              items: merged.items,
              hasMore: merged.hasMore,
              loaded: true,
              resetSeq: merged.replaced ? d.resetSeq + 1 : d.resetSeq,
              state: event.state,
              stream: overlayFromPartial(event.state),
              turnStartedAt: event.state.busy ? d.turnStartedAt ?? Date.now() : null,
              notices: event.state.busy ? d.notices : [],
            };
          });
          break;
        case "reset":
          update(sid, (d) => ({
            items: event.items,
            hasMore: event.hasMore,
            loaded: true,
            resetSeq: d.resetSeq + 1,
          }));
          break;
        case "items":
          update(sid, (d) => {
            const items = mergeItems(d.items, event.items);
            // A reply (or its error) arrived: the request a retry notice was about is over.
            const answered = event.items.some((it) => it.kind === "assistant" || it.kind === "notice" || it.kind === "interrupted");
            return { items, stream: pruneOverlay(d.stream, items), ...(answered && d.notices.length ? { notices: [] } : {}) };
          });
          break;
        case "state":
          get().setState(sid, event.state);
          if (event.state.alive) update(sid, () => ({ exited: null }));
          break;
        case "stream":
          update(sid, (d) => {
            if (event.op === "start") return { stream: { msgId: event.msgId, blocks: [] }, notices: [] };
            const stream: StreamOverlay = d.stream && d.stream.msgId === event.msgId
              ? { ...d.stream, blocks: [...d.stream.blocks] }
              : { msgId: event.msgId, blocks: [] };
            const idx = stream.blocks.findIndex((b) => b.index === event.index);
            if (event.op === "block") {
              const block: OverlayBlock = {
                index: event.index ?? 0, type: event.blockType ?? "text", text: "", name: event.name, id: event.id, done: false,
              };
              if (idx >= 0) stream.blocks[idx] = block;
              else stream.blocks.push(block);
            } else if (event.op === "delta" && idx >= 0) {
              stream.blocks[idx] = { ...stream.blocks[idx], text: stream.blocks[idx].text + (event.text ?? "") };
            } else if (event.op === "block_stop" && idx >= 0) {
              stream.blocks[idx] = { ...stream.blocks[idx], done: true };
              return { stream: pruneOverlay(stream, d.items), turnStartedAt: d.turnStartedAt ?? Date.now(), notices: [] };
            }
            // Output is arriving, so the retry worked.
            return { stream, turnStartedAt: d.turnStartedAt ?? Date.now(), notices: [] };
          });
          break;
        case "result":
          update(sid, (d) => ({
            stream: null,
            turnStartedAt: null,
            notices: [],
            state: d.state ? { ...d.state, lastResult: event.result } : d.state,
          }));
          break;
        case "notice":
          // What the request in progress is going through ("overloaded, retrying (2/7)"): only the latest counts,
          // and it goes away when output arrives or the turn ends (above and below).
          update(sid, () => ({ notices: [{ id: ++noticeId, level: event.level, text: event.text, ts: Date.now() }] }));
          break;
        case "exit":
          update(sid, () => ({ exited: { code: event.code, stderr: event.stderr }, stream: null, turnStartedAt: null, notices: [] }));
          break;
        default:
          break;
      }
    },
  };
});
