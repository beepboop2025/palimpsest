/* Palimpsest service worker — network-first so the observatory always shows the
   freshest censorship data when online, and falls back to the last cached copy
   only when offline. Never serve stale data to a connected user. */
const CACHE = "palimpsest-v1";
const SHELL = [
  "/",
  "/dashboards/ddti_observatory.html",
  "/dashboards/ddti_dashboard.html",
  "/brand/palimpsest-icon.svg",
  "/brand/palimpsest-icon-512.png",
];

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL).catch(() => {})));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET" || new URL(req.url).origin !== location.origin) return;
  e.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req))
  );
});
