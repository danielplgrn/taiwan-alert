const VERSION = "v2";
const SHELL_CACHE = `tw-alert-shell-${VERSION}`;
const DATA_CACHE = `tw-alert-data-${VERSION}`;
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./style.css?v=3",
  "./app.js?v=3",
  "./manifest.webmanifest",
  "./icon.svg",
  "./icon-maskable.svg",
];
const STATE_CACHE_KEY = new Request("state.json");

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE).then((cache) => cache.addAll(SHELL_ASSETS)),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(
        keys
          .filter((k) => k !== SHELL_CACHE && k !== DATA_CACHE)
          .map((k) => caches.delete(k)),
      ),
    ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);
  const isStateJson = url.pathname.endsWith("state.json");

  if (isStateJson) {
    event.respondWith(networkFirst());
  } else if (url.origin === location.origin) {
    event.respondWith(cacheFirst(req));
  }
});

async function networkFirst() {
  const cache = await caches.open(DATA_CACHE);
  try {
    const fresh = await fetch(STATE_CACHE_KEY, { cache: "no-store" });
    if (fresh.ok) cache.put(STATE_CACHE_KEY, fresh.clone());
    return fresh;
  } catch {
    const cached = await cache.match(STATE_CACHE_KEY);
    if (cached) return cached;
    return new Response(JSON.stringify({ error: "offline" }), {
      status: 503,
      headers: { "Content-Type": "application/json" },
    });
  }
}

async function cacheFirst(req) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const fresh = await fetch(req);
    if (fresh.ok) cache.put(req, fresh.clone());
    return fresh;
  } catch {
    return cached || Response.error();
  }
}
