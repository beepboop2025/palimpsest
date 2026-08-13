/* Palimpsest service worker — network-first so the observatory always shows the
   freshest censorship data when online, and falls back to the last cached copy
   only when offline. Never serve stale data to a connected user. */
/* Bump CACHE whenever the shell assets change shape, so a returning reader is
   not left holding a cached page that points at a stylesheet we no longer ship. */
const CACHE = "palimpsest-v13";
const LIVE_ROLLUP = "/readings/osint-china-latest.json";
const LIVE_NEWSROOM = "/readings/newsroom-latest.json";
const LIVE_EVIDENCE_READINGS = new Set([
  "/readings/newswire-latest.json",
  "/readings/china-economic-pulse-latest.json",
  "/readings/investigations-latest.json",
  "/readings/evidence-mesh-latest.json",
  "/readings/machine-investigations-latest.json",
  "/readings/primary-documents-latest.json",
  "/readings/corroboration-latest.json",
  "/readings/network-rounds-latest.json",
  "/readings/source-workflow-latest.json",
  "/readings/editorial-readiness-latest.json",
]);
const LIVE_INVESTIGATION_CASE = /^\/news\/investigations\/[a-z0-9]+(?:-[a-z0-9]+)*\/case\.json$/;
const LIVE_MACHINE_ANALYSIS_REPORT = /^\/news\/analysis\/[a-z0-9]+(?:-[a-z0-9]+)*\/report\.json$/;
const LIVE_EVENT_ANALYSIS = /^\/news\/wire\/event-[0-9a-f]{24}\/analysis\.json$/;
const LIVE_NEWSROOM_SYNDICATION = new Set(["/news/feed.json", "/news/feed.xml"]);
const SHELL = [
  "/",
  "/osint-china.html",
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

function expectedContentType(url, response, request) {
  if (!response || !response.ok || response.type === "opaque") return false;
  const type = (response.headers.get("content-type") || "")
    .split(";", 1)[0]
    .trim()
    .toLowerCase();
  const path = url.pathname.toLowerCase();
  if (path.endsWith(".json")) return type === "application/json" || type.endsWith("+json");
  if (path.endsWith(".css")) return type === "text/css";
  if (path.endsWith(".js")) {
    return type === "text/javascript" || type === "application/javascript";
  }
  if (path.endsWith(".svg")) return type === "image/svg+xml";
  if (path.endsWith(".png")) return type === "image/png";
  if (path === "/" || path.endsWith(".html") || request.mode === "navigate") {
    return type === "text/html";
  }
  return false;
}

async function cacheVerified(cache, request, key) {
  const response = await fetch(request, { cache: "no-cache" });
  const url = new URL(request.url || request, self.location.origin);
  if (expectedContentType(url, response, request)) {
    await cache.put(key || url.origin + url.pathname, response.clone());
  }
  return response;
}

self.addEventListener("install", (e) => {
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE).then((cache) =>
      Promise.all(SHELL.map((path) =>
        cacheVerified(cache, new Request(new URL(path, self.location.origin))).catch(() => null)
      ))
    )
  );
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
  // This feed is a live health document. A cached response would turn a failed refresh
  // into an apparent success, so it is network-only. The page retains and visibly ages
  // its last verified in-memory document when this request fails.
  if (url.pathname === LIVE_ROLLUP) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // These are mutable heads over append-only/version-aware evidence. Serving an
  // older head as current would conceal a failed pull, a revision or an explicit
  // coverage gap, so every document uses the same network-only boundary as the roll-up.
  if (LIVE_EVIDENCE_READINGS.has(url.pathname)) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // A case.json is the mutable head for one research lead. Its sibling
  // revisions/*.json records are content-addressed historical receipts and may
  // use the normal network-first/offline-fallback path below.
  if (LIVE_INVESTIGATION_CASE.test(url.pathname)) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // report.json is likewise a mutable machine-analysis head. Its immutable
  // revisions remain cacheable, but a correction or a new abstention must not
  // be hidden behind an older offline response.
  if (LIVE_MACHINE_ANALYSIS_REPORT.test(url.pathname)) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // analysis.json is the mutable editorial head for one wire event. Its
  // content-addressed analysis/revisions/*.json siblings remain cacheable.
  if (LIVE_EVENT_ANALYSIS.test(url.pathname)) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // The newsroom is derived from the roll-up in the same publication run. Falling
  // back to an older edition would hide a failed build and detach a story from the
  // evidence currently served at its source URL, so this JSON feed also fails closed.
  if (url.pathname === LIVE_NEWSROOM) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // Syndication clients must see the same edition boundary as API clients. An
  // installed PWA may cache article pages for offline reading, but it must never
  // present an old JSON Feed or RSS document as the current edition.
  if (LIVE_NEWSROOM_SYNDICATION.has(url.pathname)) {
    e.respondWith(fetch(req, { cache: "no-store" }));
    return;
  }
  // Several signal pages bust their own cache with ?_=<timestamp>. Keyed by the
  // full URL those would mint a fresh entry on every load — the cache grows
  // without bound and the offline fallback never matches the next request. Key
  // every entry by origin + pathname instead, and read it back ignoring search,
  // so the last good copy is still there when the reader goes offline.
  const key = url.origin + url.pathname;
  e.respondWith(
    fetch(req)
      .then((res) => {
        if (expectedContentType(url, res, req)) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(key, copy)).catch(() => {});
        }
        return res;
      })
      .catch(() => caches.match(req, { ignoreSearch: true }))
  );
});
