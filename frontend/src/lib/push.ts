/**
 * Web Push for this browser: subscribe with the server's VAPID key, keep the
 * server's copy current, and open the right session when a notification is
 * tapped. Notifications themselves are shown by the service worker
 * (public/push-sw.js).
 */
import { api } from "@/api/client";
import { storage } from "@/lib/storage";
import { parseRoute, useUi } from "@/store/ui";

export type PushState = "unsupported" | "denied" | "off" | "on";

const ENABLED_KEY = "push.enabled";

function supported(): boolean {
  return window.isSecureContext && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

async function registration(): Promise<ServiceWorkerRegistration | null> {
  if (!supported()) return null;
  return (await navigator.serviceWorker.getRegistration()) ?? null;
}

function keyBytes(b64url: string): Uint8Array<ArrayBuffer> {
  const pad = "=".repeat((4 - (b64url.length % 4)) % 4);
  const raw = atob((b64url + pad).replace(/-/g, "+").replace(/_/g, "/"));
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

function sameKey(a: ArrayBuffer | null, b: Uint8Array): boolean {
  if (!a || a.byteLength !== b.length) return false;
  const view = new Uint8Array(a);
  return view.every((v, i) => v === b[i]);
}

export async function pushState(): Promise<PushState> {
  if (!supported()) return "unsupported";
  if (Notification.permission === "denied") return "denied";
  const reg = await registration();
  if (!reg) return "unsupported";
  const sub = await reg.pushManager.getSubscription();
  return sub && Notification.permission === "granted" ? "on" : "off";
}

async function subscribe(): Promise<PushSubscription> {
  const reg = await navigator.serviceWorker.ready;
  const { publicKey } = await api<{ publicKey: string }>("push/config");
  const key = keyBytes(publicKey);
  let sub = await reg.pushManager.getSubscription();
  // A subscription made with a different server key (e.g. after a reinstall) cannot be reused.
  if (sub && !sameKey(sub.options.applicationServerKey, key)) {
    await sub.unsubscribe();
    sub = null;
  }
  sub ??= await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: key });
  await api("push/subscribe", { body: { subscription: sub.toJSON() } });
  return sub;
}

export async function enablePush(): Promise<PushState> {
  if (!supported()) return "unsupported";
  const permission = await Notification.requestPermission();
  if (permission !== "granted") return permission === "denied" ? "denied" : "off";
  await subscribe();
  storage.set(ENABLED_KEY, true);
  return "on";
}

export async function disablePush(): Promise<PushState> {
  storage.remove(ENABLED_KEY);
  const sub = await (await registration())?.pushManager.getSubscription();
  if (sub) {
    await api("push/unsubscribe", { body: { endpoint: sub.endpoint } }).catch(() => undefined);
    await sub.unsubscribe();
  }
  return "off";
}

export async function testPush(): Promise<void> {
  const sub = await (await registration())?.pushManager.getSubscription();
  if (!sub) throw new Error("推送还没有开启");
  await api("push/test", { body: { endpoint: sub.endpoint } });
}

/**
 * After login: re-send this browser's subscription (the server may have lost
 * it, or the browser renewed it), re-subscribing if the browser dropped it.
 */
export async function syncPush(): Promise<void> {
  if (!storage.get(ENABLED_KEY, false) || !supported() || Notification.permission !== "granted") return;
  try {
    await subscribe();
  } catch {
    /* offline or push service unavailable; try again next time */
  }
}

/** The service worker asks an open window to show a session when its notification is tapped. */
export function listenForNotificationClicks(): void {
  if (!("serviceWorker" in navigator)) return;
  navigator.serviceWorker.addEventListener("message", (event: MessageEvent) => {
    const data = event.data as { type?: string; url?: string } | null;
    if (data?.type === "rz-open" && typeof data.url === "string") {
      useUi.getState().navigate(parseRoute(new URL(data.url, window.location.origin).pathname));
    }
  });
}
