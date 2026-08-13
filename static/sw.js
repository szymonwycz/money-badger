// Money Badger service worker.
//
// Static shell: network-first, falling back to cache — so the app still opens offline.
// API data: network ONLY. It used to be cached like everything else, which meant a
// two-second Wi-Fi drop mid-load served last week's balances with nothing in the UI
// saying so. Wrong financial figures shown as current are worse than no figures.
// Writes (POST/PUT/DELETE) never touch the cache; mobile.js queues those itself.
const CACHE = 'money-badger-v3';
const SHELL = ['/static/style.css', '/static/design_tokens.css',
               '/static/badger.png', '/static/icon-192.png', '/static/icon-512.png',
               '/static/common.js', '/static/mobile.js', '/static/manifest.webmanifest'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

const offlineJSON = () => new Response(
  JSON.stringify({ error: 'offline — no cached data' }),
  { status: 503, headers: { 'Content-Type': 'application/json' } }
);

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;

  const url = new URL(e.request.url);

  if (url.pathname.startsWith('/api/')) {
    // Never serve stale money. A failure surfaces as an error the UI can show.
    e.respondWith(fetch(e.request).catch(offlineJSON));
    return;
  }

  e.respondWith(
    fetch(e.request)
      .then(res => {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
        return res;
      })
      // caches.match resolves to undefined for anything uncached, and
      // respondWith(undefined) throws — the browser then shows its own error page
      // instead of the app. Always hand back a real Response.
      .catch(() => caches.match(e.request).then(r => r || offlineJSON()))
  );
});
