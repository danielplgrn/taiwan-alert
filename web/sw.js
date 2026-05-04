const VERSION = "v4";
const SHELL_CACHE = `tw-alert-shell-${VERSION}`;
const DATA_CACHE = `tw-alert-data-${VERSION}`;
const SHELL_ASSETS = [
  "./",
  "./index.html",
  "./style.css?v=4",
  "./app.js?v=4",
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
  if (url.origin !== location.origin) return;

  const isStateJson = url.pathname.endsWith("state.json");
  // app.js, style.css, index.html, sw.js itself — network-first with cache
  // fallback so version bumps propagate within a single page load. Otherwise
  // installed PWAs / cached browser sessions would stick on the previous
  // bundle until the SW happens to be re-fetched.
  const isShellEntry = (
    isStateJson === false && (
      url.pathname.endsWith("app.js") ||
      url.pathname.endsWith("style.css") ||
      url.pathname.endsWith("index.html") ||
      url.pathname.endsWith("sw.js") ||
      url.pathname === "/" || url.pathname.endsWith("/")
    )
  );

  if (isStateJson) {
    event.respondWith(networkFirst(STATE_CACHE_KEY, DATA_CACHE));
  } else if (isShellEntry) {
    event.respondWith(networkFirst(req, SHELL_CACHE));
  } else {
    // icons, manifest, fonts — rarely change; cache-first is fine
    event.respondWith(cacheFirst(req));
  }
});

async function networkFirst(req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const fresh = await fetch(req, { cache: "no-store" });
    if (fresh.ok) cache.put(req, fresh.clone());
    return fresh;
  } catch {
    const cached = await cache.match(req);
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
