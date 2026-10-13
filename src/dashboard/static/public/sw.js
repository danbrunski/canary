// CANary dashboard service worker (Task 6.12) — offline shell caching.
//
// Strategy split: the built static shell (HTML / hashed JS+CSS / icons) is
// cache-first so the dashboard opens instantly and survives a flaky truck
// hotspot or a Pi reboot mid-drive. Everything dynamic (REST `/status`,
// `/anomalies`, `/control/*`, `/telemetry/*`) is network-only and never
// cached — stale telemetry is worse than no telemetry, and the app already
// surfaces freshness/reconnect state itself. WebSocket upgrades bypass the
// SW entirely.

// The shell is cache-first, so a changed shell file reaches an installed app
// only when this name changes (activate drops the old cache). v2: the P2.1
// manifest (no landscape lock) and its PNG icons.
const CACHE = "canary-shell-v2";
// `cache.addAll` fails the whole install on one 404: every entry must exist in
// public/ (manifest.test.ts checks it).
const SHELL = [
  "/",
  "/index.html",
  "/manifest.webmanifest",
  "/canary.svg",
  "/icon-192.png",
  "/icon-512.png",
  "/icon-maskable-512.png",
  "/apple-touch-icon.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// Dynamic paths the SW must never serve from cache.
function isDynamic(url) {
  return (
    url.pathname.startsWith("/status") ||
    url.pathname.startsWith("/config") ||
    url.pathname.startsWith("/anomalies") ||
    url.pathname.startsWith("/telemetry") ||
    url.pathname.startsWith("/obd") ||
    url.pathname.startsWith("/bindings") ||
    url.pathname.startsWith("/models") ||
    url.pathname.startsWith("/control") ||
    url.pathname.startsWith("/ws")
  );
}

self.addEventListener("fetch", (event) => {
  const { request } = event;
  if (request.method !== "GET") return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin || isDynamic(url)) return; // network

  if (request.mode === "navigate") {
    // App shell: try network, fall back to the cached index for offline.
    event.respondWith(fetch(request).catch(() => caches.match("/index.html")));
    return;
  }

  // Hashed build assets: cache-first, populate on first hit.
  event.respondWith(
    caches.match(request).then(
      (hit) =>
        hit ||
        fetch(request).then((resp) => {
          if (resp.ok) {
            const copy = resp.clone();
            caches.open(CACHE).then((c) => c.put(request, copy));
          }
          return resp;
        }),
    ),
  );
});
