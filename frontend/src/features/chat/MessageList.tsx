import { ArrowDown, LoaderCircle } from "lucide-react";
import { forwardRef, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { Virtuoso, type VirtuosoHandle } from "react-virtuoso";
import type { Item } from "@/api/types";
import { ItemView } from "./MessageItems";

interface Props {
  sessionKey: string;
  items: Item[];
  hasMore: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  footer: ReactNode;
  emptyState?: ReactNode;
}

const List = forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement>>(function List(props, ref) {
  return <div {...props} ref={ref} className="rz-msg-list" />;
});

interface FooterContext {
  footer: ReactNode;
  onFooterResize: () => void;
}

function FooterSlot({ context }: { context?: FooterContext }) {
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

export function MessageList({ sessionKey, items, hasMore, loadingOlder, onLoadOlder, footer, emptyState }: Props) {
  const ref = useRef<VirtuosoHandle>(null);
  const [atBottom, setAtBottom] = useState(true);
  const firstIndex = items.length ? items[0].seq : 1;

  const scrollToBottom = useCallback(() => {
    ref.current?.scrollToIndex({ index: "LAST", align: "end", behavior: "smooth" });
  }, []);

  // We position the list at the end ourselves (Virtuoso's initialTopMostItemIndex can
  // leave the list hidden forever). The list stays transparent until it has settled.
  const [settled, setSettled] = useState(false);
  const ready = useRef(false);
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

  useEffect(() => {
    ready.current = false;
    setSettled(false);
    pinnedUntil.current = Date.now() + 2500;
    const release = () => {
      pinnedUntil.current = 0;
    };
    window.addEventListener("wheel", release, { once: true, passive: true });
    window.addEventListener("touchstart", release, { once: true, passive: true });
    return () => {
      window.removeEventListener("wheel", release);
      window.removeEventListener("touchstart", release);
    };
  }, [sessionKey]);

  const toEnd = useCallback(() => ref.current?.scrollTo({ top: Number.MAX_SAFE_INTEGER }), []);

  const settle = useCallback(() => {
    if (!ready.current || userScrolling()) return;
    if (atBottomRef.current || Date.now() < pinnedUntil.current) requestAnimationFrame(toEnd);
  }, [toEnd]);

  const onItemsRendered = useCallback(
    (rendered: unknown[]) => {
      if (!rendered.length || ready.current) return;
      ready.current = true;
      toEnd();
      window.setTimeout(() => {
        toEnd();
        setSettled(true);
      }, 60);
    },
    [toEnd],
  );

  const context = useMemo(() => ({ footer, onFooterResize: settle }), [footer, settle]);

  const Header = useCallback(
    () => (
      <div className="rz-msg-header">
        {loadingOlder ? <LoaderCircle size={16} className="spin" /> : hasMore ? <span className="dim">向上滚动加载更早的消息</span> : null}
      </div>
    ),
    [hasMore, loadingOlder],
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
        firstItemIndex={Math.max(0, firstIndex)}
        computeItemKey={(_, item) => item.key}
        itemContent={(_, item) => (
          <div className="rz-msg-row">
            <ItemView item={item} />
          </div>
        )}
        followOutput={(bottom) => (bottom && !userScrolling() ? "auto" : false)}
        atBottomStateChange={setAtBottom}
        atBottomThreshold={80}
        startReached={() => hasMore && onLoadOlder()}
        increaseViewportBy={{ top: 800, bottom: 400 }}
        context={context}
        itemsRendered={onItemsRendered}
        totalListHeightChanged={settle}
        components={{ List, Header, Footer: FooterSlot }}
      />
      {!atBottom && (
        <button type="button" className="rz-jump" onClick={scrollToBottom} aria-label="回到底部">
          <ArrowDown size={16} />
        </button>
      )}
    </div>
  );
}
