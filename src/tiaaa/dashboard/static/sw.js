/* TI-AAA Web Push worker. It stores no candidate or application data. */

self.addEventListener("push", event => {
  let message = {};
  try { message = event.data ? event.data.json() : {}; } catch (_) { /* use safe defaults */ }
  const title = message.title || "TI-AAA update";
  event.waitUntil(self.registration.showNotification(title, {
    body: message.body || "The Auto-mode application queue changed.",
    tag: message.tag || "tiaaa-auto-mode",
    renotify: true,
    data: { url: message.url || "/?view=live" },
  }));
});

self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = new URL(event.notification.data?.url || "/?view=live", self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then(windows => {
    const existing = windows.find(client => client.url.startsWith(self.location.origin));
    if (existing) {
      return existing.navigate(target).then(client => client.focus());
    }
    return self.clients.openWindow(target);
  }));
});
