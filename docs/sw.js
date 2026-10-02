// Insider Radar service worker: app works offline and opens instantly from the home screen.
const CACHE = "insider-radar-v5";
const SHELL = ["./", "index.html", "manifest.webmanifest", "icons/icon-180.png", "icons/icon-192.png", "icons/icon-512.png", "icons/favicon-32.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin || url.pathname.includes("/api/")) return;
  if (url.pathname.includes("/data/")) {
    // Data: always try the network first; fall back to the last saved copy when offline.
    const key = url.origin + url.pathname;
    e.respondWith(fetch(e.request).then((res) => {
      if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(key, copy)); }
      return res;
    }).catch(() => caches.match(key).then((hit) => {
      if (!hit) return new Response("offline", { status: 503 });
      const h = new Headers(hit.headers); h.set("X-From-Cache", "1");
      return hit.blob().then((b) => new Response(b, { status: 200, headers: h }));
    })));
    return;
  }
  // App shell: network first (so updates show up), cached copy when offline.
  e.respondWith(fetch(e.request).then((res) => {
    if (res.ok) { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); }
    return res;
  }).catch(() => caches.match(e.request).then((hit) => hit || caches.match("index.html"))));
});
