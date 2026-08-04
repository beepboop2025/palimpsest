/* Palimpsest service worker — network-first so the observatory always shows the
   freshest censorship data when online, and falls back to the last cached copy
   only when offline. Never serve stale data to a connected user. */
/* Bump CACHE whenever the shell assets change shape, so a returning reader is
   not left holding a cached page that points at a stylesheet we no longer ship. */
const CACHE = "palimpsest-v5";
const SHELL = [
  "/",
  "/osint-china.html",
  "/readings/osint-china-latest.json",
  "/dashboards/ddti_observatory.html",
  "/dashboards/ddti_dashboard.html",
  /* The stylesheets and behaviour the pages above depend on. Without these an
     offline reader got the markup and none of the presentation, which on a page
     whose whole job is to distinguish a reading from its evidence is not a
     degraded experience so much as a misleading one. */
  "/dashboards/assets/tikto.css",
  "/assets/shell.css",
  "/assets/shell.js",
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
  const url = new URL(req.url);
  if (req.method !== "GET" || url.origin !== location.origin) return;
  // Several signal pages bust their own cache with ?_=<timestamp>. Keyed by the
  // full URL those would mint a fresh entry on every load — the cache grows
  // without bound and the offline fallback never matches the next request. Key
  // every entry by origin + pathname instead, and read it back ignoring search,
  // so the last good copy is still there when the reader goes offline.
  const key = url.origin + url.pathname;
  e.respondWith(
    fetch(req)
      .then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
        return res;
      })
      .catch(() => caches.match(req, { ignoreSearch: true }))
  );
});
