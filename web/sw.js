// Service worker: makes Sift installable, opens offline with the last news it saw,
// and shows breaking-news / briefing push notifications.
const CACHE = "sift-v1";
const SHELL = ["/", "/manifest.webmanifest", "/icons/icon-192.png"];

self.addEventListener("install", event => {
  event.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", event => {
  event.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

// Network first, so news is never stale while online; fall back to the last copy when offline.
// Live updates (/api/stream) and anything that isn't a GET always go straight to the network.
self.addEventListener("fetch", event => {
  const req = event.request, url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin || url.pathname === "/api/stream") return;
  if (!(req.mode === "navigate" || url.pathname === "/api/bundle" || SHELL.includes(url.pathname))) return;
  event.respondWith(fetch(req).then(res => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then(c => c.put(req.mode === "navigate" ? "/" : req, copy)); }
    return res;
  }).catch(() => caches.match(req.mode === "navigate" ? "/" : req)));
});

self.addEventListener("push", event => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = { title: event.data && event.data.text() }; }
  event.waitUntil(self.registration.showNotification(data.title || "Sift", {
    body: data.body || "Breaking development",
    icon: "/icons/icon-192.png",
    badge: "/icons/badge-96.png",
    tag: data.url || "sift",          // one notification per story; updates replace it
    renotify: true,
    data: { url: data.url || "/" }
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const target = event.notification.data.url;
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then(list => {
    for (const c of list) { if ("focus" in c) { return c.focus().then(w => (w && "navigate" in w) ? w.navigate(target) : w); } }
    return clients.openWindow(target);
  }));
});
