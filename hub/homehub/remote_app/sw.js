const SHELL = "home-shell-v1";
const SHELL_FILES = ["/", "/app.css", "/app.js", "/mc.js", "/packages.js", "/register.js", "/manifest.json", "/icon.svg",
  "/icon-192.png", "/apple-touch-icon.png"];
const NETWORK_WAIT_MS = 4000;

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(SHELL).then((cache) => cache.addAll(SHELL_FILES)).catch(() => {}).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys()
    .then((names) => Promise.all(names.filter((name) => name !== SHELL).map((name) => caches.delete(name))))
    .then(() => self.clients.claim()));
});

function shellKey(request) {
  if (request.method !== "GET") return null;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return null;
  if (url.pathname === "/" || url.pathname === "/index.html") return "/";
  return SHELL_FILES.includes(url.pathname) ? url.pathname : null;
}

function saveCopy(key, response) {
  if (!response.ok || response.redirected || response.type !== "basic") return Promise.resolve();
  const copy = response.clone();
  return caches.open(SHELL).then((cache) => cache.put(key, copy));
}

async function answer(key, network) {
  const late = new Promise((done) => setTimeout(() => done(null), NETWORK_WAIT_MS));
  const first = await Promise.race([network, late]).catch(() => null);
  if (first) return first;
  const saved = await caches.match(key, { cacheName: SHELL });
  return saved || network;
}

self.addEventListener("fetch", (event) => {
  const key = shellKey(event.request);
  if (!key) return;
  const network = fetch(event.request);
  event.waitUntil(network.then((response) => saveCopy(key, response)).catch(() => null));
  event.respondWith(answer(key, network));
});
