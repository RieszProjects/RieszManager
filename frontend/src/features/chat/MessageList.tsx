import { ArrowDown, LoaderCircle } from "lucide-react";
import { forwardRef, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { Item } from "@/api/types";
import { ItemView } from "./MessageItems";

/** Large starting index so prepending older items (even across negative handoff seqs) never hits 0. */
const START_INDEX = 1_000_000;

interface Props {
  sessionKey: string;
  items: Item[];
  hasMore: boolean;
  loadingOlder: boolean;
  resetSeq?: number;
  onLoadOlder: () => void;
  footer: ReactNode;
  emptyState?: ReactNode;
}

const List = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(function List(props, ref) {
  return <div {...props} ref={ref} className="rz-msg-list" />;
});

interface ListContext {
  footer: ReactNode;
  hasMore: boolean;
  loadingOlder: boolean;
  onFooterResize: () => void;
}

function HeaderSlot({ context }: { context?: ListContext }) {
  return (
    <div className="rz-msg-header">
      {context?.loadingOlder ? <LoaderCircle size={16} className="spin" /> : context?.hasMore ? <span className="dim">向上滚动加载更早的消息</span> : null}
    </div>
  );
}

function FooterSlot({ context }: { context?: ListContext }) {
  const el = useRef<HTMLDivElement>(null);
  const cb = useRef(context?.onFooterResize);
  cb.current = context?.onFooterResize;
  useEffect(() => {
    if (!el.current) return;
    const ro = new ResizeObserver(() => cb.current?.());
    ro.observe(el.current);
    return () => ro.disconnect();
  }, []);
  return <div ref={el} className="rz-msg-footer">{context?.footer}</div>;
}

const COMPONENTS = { List, Header: HeaderSlot, Footer: FooterSlot };

interface IndexTracker {
  sessionKey: string;
  itemsRef: Item[] | null;
  keys: string[];
  firstIndex: number;
}

/**
 * Compute Virtuoso's `firstItemIndex` by exact item count delta anchored on `item.key`,
 * rather than `Math.max(0, items[0].seq)`. Clamping negative handoff seqs to 0 or having
 * seq gaps violates Virtuoso's prepend/shift invariant and jumps scroll to the top.
 */
function nextFirstIndex(tracker: IndexTracker, sessionKey: string, items: Item[]): number {
  if (tracker.sessionKey === sessionKey && tracker.itemsRef === items) {
    return tracker.firstIndex;
  }
  let idx = tracker.firstIndex;
  if (tracker.sessionKey !== sessionKey || !tracker.keys.length || !items.length) {
    idx = START_INDEX;
  } else if (items[0].key !== tracker.keys[0]) {
    const prepended = items.findIndex((it) => it.key === tracker.keys[0]);
    if (prepended > 0) {
      idx = Math.max(0, tracker.firstIndex - prepended);
    } else {
      const trimmed = tracker.keys.indexOf(items[0].key);
      if (trimmed > 0) {
        idx = tracker.firstIndex + trimmed;
      }
      // Disjoint replacement (e.g. session/load regenerated keys): keep firstIndex unchanged
      // so Virtuoso does not emit a negative shiftWithOffset yank.
    }
  }
  tracker.sessionKey = sessionKey;
  tracker.itemsRef = items;
  tracker.keys = items.map((it) => it.key);
  tracker.firstIndex = idx;
  return idx;
}

export function MessageList({ sessionKey, items, hasMore, loadingOlder, resetSeq = 0, onLoadOlder, footer, emptyState }: Props) {
  const ref = useRef<VirtuosoHandle>(null);
  const [atBottom, setAtBottom] = useState(true);
  const tracker = useRef<IndexTracker>({ sessionKey, itemsRef: null, keys: [], firstIndex: START_INDEX });
  const firstIndex = nextFirstIndex(tracker.current, sessionKey, items);

  const scrollToBottom = useCallback(() => {
    ref.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" });
  }, []);

  // We position the list at the end ourselves (Virtuoso's initialTopMostItemIndex can
  // leave the list hidden forever). The list stays transparent until it has settled.
  const [settled, setSettled] = useState(false);
  const settledRef = useRef(false);
  settledRef.current = settled;
  const ready = useRef(false);
  const settleTimer = useRef<number | undefined>(undefined);
  const pinnedUntil = useRef(Date.now() + 2500);
  const atBottomRef = useRef(true);
  atBottomRef.current = atBottom;

  // While a finger or the wheel is moving the list (including the momentum after
  // the finger lifts), never pull it back to the bottom: on phones that made the
  // first swipes of a running session snap back, as if the list were stuck.
  const touching = useRef(false);
  const handsOffUntil = useRef(0);
  const userScrolling = () => touching.current || Date.now() < handsOffUntil.current;
  const handsOff = (ms: number) => {
    handsOffUntil.current = Math.max(handsOffUntil.current, Date.now() + ms);
  };

  // Capture whether we were at the bottom *before* Virtuoso commits a server-driven reset
  // (e.g. an idle Antigravity/Codex/dsh driver waking up and replaying history with new keys).
  const prevResetSeq = useRef(resetSeq);
  const pinAfterReset = useRef(false);
  if (resetSeq !== prevResetSeq.current) {
    prevResetSeq.current = resetSeq;
    if (ready.current && atBottomRef.current && !userScrolling()) {
      pinAfterReset.current = true;
      pinnedUntil.current = Math.max(pinnedUntil.current, Date.now() + 1200);
    }
  }

  useEffect(() => {
    ready.current = false;
    settledRef.current = false;
    setSettled(false);
    pinAfterReset.current = false;
    window.clearTimeout(settleTimer.current);
    pinnedUntil.current = Date.now() + 2500;
    const release = () => {
      pinnedUntil.current = 0;
    };
    window.addEventListener("wheel", release, { once: true, passive: true });
    window.addEventListener("touchstart", release, { once: true, passive: true });
    return () => {
      window.clearTimeout(settleTimer.current);
      window.removeEventListener("wheel", release);
      window.removeEventListener("touchstart", release);
    };
  }, [sessionKey]);

  const toEnd = useCallback(() => ref.current?.scrollTo({ top: Number.MAX_SAFE_INTEGER }), []);

  useEffect(() => {
    if (!pinAfterReset.current) return;
    pinAfterReset.current = false;
    const raf = requestAnimationFrame(toEnd);
    const t = window.setTimeout(() => {
      if (!userScrolling()) toEnd();
    }, 60);
    return () => {
      cancelAnimationFrame(raf);
      window.clearTimeout(t);
    };
  }, [resetSeq, toEnd]);

  const settle = useCallback(() => {
    if (!ready.current || userScrolling()) return;
    if (atBottomRef.current || Date.now() < pinnedUntil.current) requestAnimationFrame(toEnd);
  }, [toEnd]);

  const onItemsRendered = useCallback(
    (rendered: unknown[]) => {
      if (!rendered.length || ready.current) return;
      ready.current = true;
      toEnd();
      window.clearTimeout(settleTimer.current);
      settleTimer.current = window.setTimeout(() => {
        toEnd();
        settledRef.current = true;
        setSettled(true);
      }, 60);
    },
    [toEnd],
  );

  const context = useMemo<ListContext>(
    () => ({ footer, hasMore, loadingOlder, onFooterResize: settle }),
    [footer, hasMore, loadingOlder, settle],
  );

  if (!items.length && emptyState) {
    return (
      <div className="rz-msg-scroll-empty">
        {emptyState}
        <div className="rz-msg-footer">{footer}</div>
      </div>
    );
  }

  return (
    <div
      className={`rz-msg-wrap${settled ? "" : " settling"}`}
      onTouchStart={() => { touching.current = true; }}
      onTouchEnd={() => { touching.current = false; handsOff(700); }}
      onTouchCancel={() => { touching.current = false; handsOff(700); }}
      onWheel={() => handsOff(350)}
      onScrollCapture={() => Date.now() < handsOffUntil.current && handsOff(150)}
    >
      <Virtuoso
        key={sessionKey}
        ref={ref}
        className="rz-msg-scroller"
        data={items}
        firstItemIndex={firstIndex}
        computeItemKey={(_, item) => item.key}
        itemContent={(_, item) => (
          <div className="rz-msg-row">
            <ItemView item={item} />
          </div>
        )}
        followOutput={(bottom) => (bottom && !userScrolling() ? "auto" : false)}
        atBottomStateChange={setAtBottom}
        atBottomThreshold={80}
        startReached={() => {
          if (settledRef.current && ready.current && hasMore) onLoadOlder();
        }}
        increaseViewportBy={{ top: 800, bottom: 400 }}
        context={context}
        itemsRendered={onItemsRendered}
        totalListHeightChanged={settle}
        components={COMPONENTS}
      />
      {!atBottom && (
        <button type="button" className="rz-jump" onClick={scrollToBottom} aria-label="回到底部">
          <ArrowDown size={16} />
        </button>
      )}
    </div>
  );
}
