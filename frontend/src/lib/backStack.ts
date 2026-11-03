/**
 * Phones: Back closes the topmost open window (dialog, menu, drawer) instead
 * of leaving the page.
 *
 * Every open overlay gets a history entry with the same URL. Pressing Back
 * pops one, and we close the topmost overlay: MUI modals (dialogs, menus,
 * popovers, drawers) are sent an Escape key, which they treat as a request to
 * close; the phone drawers are closed through the UI store. When overlays are
 * closed by other means (a close button, tapping the backdrop) the entries
 * they no longer need are removed again with history.go(-n), so Back never
 * has to be pressed twice.
 *
 * Overlays are found in the DOM, so this covers every dialog and menu without
 * each one opting in. Navigating while an overlay is open turns its entry
 * into the new page's entry (see takeOverlayEntry), so Back then returns to
 * the page the overlay was opened on.
 */
import { useUi } from "@/store/ui";

const OVERLAYS = "body > .MuiModal-root:not(.MuiModal-hidden), .rz-mdrawer.open";

/** History entries we pushed for overlays that are still open (ours, on top of the stack). */
let depth = 0;
/** popstate events caused by our own history.go(), to be ignored. */
let ignorePops = 0;
/** After a navigation took over an overlay entry: no new entries until every overlay has closed. */
let suspended = false;
let scheduled = false;

function countOverlays(): number {
  return document.querySelectorAll(OVERLAYS).length;
}

function sync(): void {
  scheduled = false;
  const count = countOverlays();
  if (suspended) {
    if (count === 0) suspended = false;
    return;
  }
  if (count > depth) {
    for (; depth < count; depth++) window.history.pushState({ ...(window.history.state ?? {}), rzOverlay: depth + 1 }, "");
  } else if (count < depth) {
    const extra = depth - count;
    depth = count;
    ignorePops++;
    window.history.go(-extra);
  }
}

/** Let MUI finish mounting, or animating out and unmounting, the overlay first. */
function scheduleSync(delay = 60): void {
  if (scheduled) return;
  scheduled = true;
  window.setTimeout(sync, delay);
}

function closeTopmost(): void {
  const modals = document.querySelectorAll<HTMLElement>("body > .MuiModal-root:not(.MuiModal-hidden)");
  const top = modals[modals.length - 1];
  if (top) {
    const target = top.contains(document.activeElement) ? (document.activeElement as HTMLElement) : top;
    target.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", code: "Escape", bubbles: true, cancelable: true }));
    return;
  }
  const ui = useUi.getState();
  if (ui.leftOpen) ui.setLeftOpen(false);
  else if (ui.rightOverlay) ui.setRightOverlay(false);
}

function onPopState(): void {
  if (ignorePops > 0) {
    ignorePops--;
    return;
  }
  if (depth > 0) {
    depth--;
    closeTopmost();
    // Once its closing animation is over, an overlay that refused to close
    // (e.g. a required setup step) gets its entry back.
    scheduleSync(450);
  }
}

/**
 * Called by navigation. Returns true when the current history entry belongs to
 * an overlay, in which case the caller should replace it rather than push.
 */
export function takeOverlayEntry(): boolean {
  if (depth === 0) return false;
  depth = 0;
  suspended = true;
  return true;
}

export function setupBackStack(): void {
  if (!window.matchMedia?.("(pointer: coarse)").matches) return;
  // Runs before the app's own popstate handler (registered later).
  window.addEventListener("popstate", onPopState);
  new MutationObserver(() => scheduleSync()).observe(document.body, { childList: true });
  useUi.subscribe((s, prev) => {
    if (s.leftOpen !== prev.leftOpen || s.rightOverlay !== prev.rightOverlay) scheduleSync();
  });
}
