/*
 * Push notifications, imported into the generated service worker.
 *
 * The server sends {title, body, url, tag, kind}. Nothing is shown when a
 * window of the app is in front and already showing that page. At most
 * MAX_NOTIFICATIONS remain active at once; older ones are closed first (FIFO),
 * those waiting for an answer (permission, question) last.
 * Tapping a notification focuses an open window (which then navigates, see
 * src/lib/push.ts) or opens a new one.
 */
const MAX_NOTIFICATIONS = 3;

let lastNotificationTs = 0;
let pushQueue = Promise.resolve();

function nextNotificationTs() {
  lastNotificationTs = Math.max(Date.now(), lastNotificationTs + 1);
  return lastNotificationTs;
}

function notificationTime(notification) {
  if (notification.data && typeof notification.data.ts === "number") {
    return notification.data.ts;
  }
  if (typeof notification.timestamp === "number") {
    return notification.timestamp;
  }
  return 0;
}

async function pruneNotifications(incomingTag) {
  if (typeof self.registration.getNotifications !== "function") return;
  const active = await self.registration.getNotifications();
  const others = incomingTag ? active.filter((n) => n.tag !== incomingTag) : active.slice();
  const excess = others.length - (MAX_NOTIFICATIONS - 1);
  if (excess <= 0) return;
  // Oldest first, but ones waiting for the user (a permission, a question) only once no other is left:
  // the agent is stuck until they are answered.
  const ordered = others
    .map((n, idx) => ({ n, ts: notificationTime(n), idx, urgent: Boolean(n.data && n.data.urgent) }))
    .sort((a, b) => Number(a.urgent) - Number(b.urgent) || a.ts - b.ts || a.idx - b.idx);
  for (let i = 0; i < excess; i++) {
    ordered[i].n.close();
  }
}

async function handlePush(data) {
  const url = data.url || "/";
  const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
  const watching = windows.some(
    (c) => c.visibilityState === "visible" && c.focused && new URL(c.url).pathname === url,
  );
  if (watching && data.kind !== "test") return;

  const urgent = data.kind === "permission" || data.kind === "question";
  const tag = data.tag || undefined;
  const ts = nextNotificationTs();

  try {
    await pruneNotifications(tag);
  } catch {
    /* Never block showing the incoming notification if pruning fails. */
  }

  await self.registration.showNotification(data.title || "Riesz Manager", {
    body: data.body || "",
    tag,
    renotify: Boolean(tag),
    requireInteraction: urgent,
    timestamp: ts,
    icon: "/icons/icon-192.png",
    badge: "/icons/badge-96.png",
    data: { url, ts, urgent },
  });
}

self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const run = pushQueue.then(() => handlePush(data));
  pushQueue = run.catch(() => {});
  event.waitUntil(run);
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const target = windows.find((c) => new URL(c.url).origin === self.location.origin);
      if (target) {
        await target.focus();
        target.postMessage({ type: "rz-open", url });
        return;
      }
      await self.clients.openWindow(url);
    })(),
  );
});
