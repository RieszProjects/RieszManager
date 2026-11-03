/**
 * Keeps an installed app (PWA) up to date.
 *
 * Browsers only look for a new service worker when a page is navigated to.
 * An installed app on a phone is mostly resumed from the background instead,
 * so without help it keeps running an old version for days. Here we:
 *
 * - check for a new version whenever the app comes back to the foreground,
 *   and every 30 minutes while it stays open;
 * - apply it straight away if the app was just resumed (or is in the
 *   background), since nothing is in progress then; composer drafts are saved
 *   in local storage and survive the reload;
 * - otherwise offer it in a banner, and apply it the next time the app goes
 *   to the background.
 */
import { registerSW } from "virtual:pwa-register";
import { create } from "zustand";

const CHECK_EVERY_MS = 30 * 60 * 1000;
/** A new version found this soon after resuming is applied without asking. */
const RESUME_WINDOW_MS = 10 * 1000;

interface UpdateStore {
  ready: boolean;
  apply: () => void;
}

export const useAppUpdate = create<UpdateStore>(() => ({ ready: false, apply: () => undefined }));

function busyTyping(): boolean {
  const el = document.activeElement;
  return (el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement) && Boolean(el.value);
}

export function setupPwaUpdates(): void {
  if (!("serviceWorker" in navigator)) return;
  let resumedAt = performance.now();
  let applying = false;

  const updateSW = registerSW({
    immediate: true,
    onNeedRefresh() {
      const apply = () => {
        if (applying) return;
        applying = true;
        void updateSW(true);
      };
      const justResumed = performance.now() - resumedAt < RESUME_WINDOW_MS;
      if (document.visibilityState === "hidden" || (justResumed && !busyTyping())) apply();
      else useAppUpdate.setState({ ready: true, apply });
    },
    onRegisteredSW(_url, registration) {
      if (!registration) return;
      const check = () => {
        if (navigator.onLine && !registration.installing) void registration.update().catch(() => undefined);
      };
      window.setInterval(check, CHECK_EVERY_MS);
      document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible") {
          resumedAt = performance.now();
          check();
        } else if (useAppUpdate.getState().ready) {
          useAppUpdate.getState().apply();
        }
      });
    },
  });
}
