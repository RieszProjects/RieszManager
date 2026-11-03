/*
 * Push notifications, imported into the generated service worker.
 *
 * The server sends {title, body, url, tag, kind}. Nothing is shown when a
 * window of the app is in front and already showing that page. Tapping a
 * notification focuses an open window (which then navigates, see
 * src/lib/push.ts) or opens a new one.
 */
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const url = data.url || "/";
  event.waitUntil(
    (async () => {
      const windows = await self.clients.matchAll({ type: "window", includeUncontrolled: true });
      const watching = windows.some(
        (c) => c.visibilityState === "visible" && c.focused && new URL(c.url).pathname === url,
      );
      if (watching && data.kind !== "test") return;
      const urgent = data.kind === "permission" || data.kind === "question";
      await self.registration.showNotification(data.title || "Riesz", {
        body: data.body || "",
        tag: data.tag || undefined,
        renotify: Boolean(data.tag),
        requireInteraction: urgent,
        icon: "/icons/icon-192.png",
        badge: "/icons/badge-96.png",
        data: { url },
      });
    })(),
  );
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
