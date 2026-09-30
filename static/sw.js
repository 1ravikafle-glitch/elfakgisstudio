/* ================================================================
   Elfak GIS Studio — service worker
   ------------------------------------------------------------
   Purpose: make a second visit feel instant, and make the studio
   usable when the network is slow or briefly gone.

   What it caches:
   · the app shell (CSS/JS) and the vendor libraries — none of these
     change without a deploy, and every one of them is version-pinned
     or served immutable
   · static fonts and icons

   What it must NEVER cache, and does not:
   · HTML pages — they are per-session and must reflect the server
   · /login, /me, /history, /logout — session state
   · /thesis_options, /dem_catalog — read-only reference data that
     is refreshed in the background (stale-while-revalidate) so the
     dropdowns still update on deploy
   · any non-GET request, and any response that is not OK
   · /outputs/*, /download/*, /progress/* — a user's own run data

   The rule that keeps this safe: only same-origin /static/ URLs and
   the known CDN libraries are ever stored. Everything else is passed
   through untouched.
   ================================================================ */

const VERSION = 'v2';
const SHELL_CACHE = `elfak-shell-${VERSION}`;
const VENDOR_CACHE = `elfak-vendor-${VERSION}`;
const DATA_CACHE = `elfak-data-${VERSION}`;
const KEEP = [SHELL_CACHE, VENDOR_CACHE, DATA_CACHE];

// Same-origin static assets. These are version-pinned in the templates, and
// the SW matches on the FULL url including the ?v= query, so this list must
// stay byte-identical to the templates or the cache serves a stale copy of a
// file the page already asked a newer version of. VERSION invalidates the
// whole set on deploy.
const SHELL = [
    '/static/css/fonts.css?v=20260930-1',
    '/static/fonts/inter-latin.woff2',
    '/static/fonts/inter-latin-ext.woff2',
    '/static/css/app.css?v=20260930-1',
    '/static/css/forestry.css?v=20260929-9',
    '/static/css/motion.css?v=20260929-2',
    '/static/js/theme.js?v=20260929-3',
    '/static/js/app.js?v=20260929-8',
    '/static/js/dock.js?v=20260930-1',
    '/static/js/dock-resize.js?v=20260930-1',
    '/static/js/prefetch.js?v=20260929-2',
    '/static/js/boot.js?v=20260929-1',
    '/static/js/login.js?v=20260929-3',
];

// Third-party libraries the studio loads on demand.
const VENDOR = [
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
    'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
    'https://unpkg.com/@geoman-io/leaflet-geoman-free@2.16.0/dist/leaflet-geoman.min.js',
    'https://unpkg.com/@geoman-io/leaflet-geoman-free@2.16.0/dist/leaflet-geoman.css',
    'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js',
    'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js',
];

// Read-only reference data: serve instantly, refresh in the background.
const DATA = ['/thesis_options', '/dem_catalog'];

self.addEventListener('install', (event) => {
    event.waitUntil((async () => {
        const shell = await caches.open(SHELL_CACHE);
        // One failure must not abort the install, so each entry is
        // added on its own terms.
        await Promise.all(SHELL.map((u) => shell.add(u).catch(() => {})));
        await self.skipWaiting();
    })());
});

self.addEventListener('activate', (event) => {
    event.waitUntil((async () => {
        const names = await caches.keys();
        await Promise.all(names.filter((n) => !KEEP.includes(n)).map((n) => caches.delete(n)));
        await self.clients.claim();
    })());
});

function isShell(url) {
    return url.origin === self.location.origin && url.pathname.startsWith('/static/');
}
function isVendor(url) {
    return url.hostname === 'unpkg.com' || url.hostname === 'cdnjs.cloudflare.com';
}
function isData(url) {
    return url.origin === self.location.origin && DATA.includes(url.pathname);
}

self.addEventListener('fetch', (event) => {
    const req = event.request;

    // Only GET is cacheable. Anything that changes state (login, runs,
    // deletes) goes straight to the network, untouched.
    if (req.method !== 'GET') return;

    const url = new URL(req.url);

    // A CDN request made with no-cors yields an opaque response, which is
    // exactly what a <script src> load would receive. Caching it lets the
    // library be reused offline.
    if (isVendor(url)) {
        event.respondWith(cacheFirst(req, VENDOR_CACHE));
        return;
    }
    if (isShell(url)) {
        event.respondWith(cacheFirst(req, SHELL_CACHE));
        return;
    }
    if (isData(url)) {
        event.respondWith(staleWhileRevalidate(req, DATA_CACHE));
        return;
    }
    // Everything else — HTML pages, API responses, run output — is never
    // intercepted. The browser handles it normally.
});

async function cacheFirst(req, cacheName) {
    const cache = await caches.open(cacheName);
    const hit = await cache.match(req, { ignoreVary: true });
    if (hit) return hit;
    try {
        const res = await fetch(req);
        // Opaque responses have status 0; those are still worth keeping.
        if (res && (res.ok || res.type === 'opaque')) {
            cache.put(req, res.clone()).catch(() => {});
        }
        return res;
    } catch (e) {
        const stale = await cache.match(req, { ignoreVary: true });
        if (stale) return stale;
        throw e;
    }
}

async function staleWhileRevalidate(req, cacheName) {
    const cache = await caches.open(cacheName);
    const hit = await cache.match(req, { ignoreVary: true });
    const network = fetch(req)
        .then((res) => {
            if (res && res.ok) cache.put(req, res.clone()).catch(() => {});
            return res;
        })
        .catch(() => null);

    if (hit) {
        // Refresh for next time, but answer now from cache.
        event_safe(network);
        return hit;
    }
    const res = await network;
    if (res) return res;
    return new Response(JSON.stringify({ error: 'Offline and no cached copy.' }), {
        status: 503,
        headers: { 'Content-Type': 'application/json' },
    });
}

// Swallow a background refresh failure: the user already has their data.
function event_safe(p) { if (p && p.catch) p.catch(() => {}); }

self.addEventListener('message', (event) => {
    if (event.data === 'skip-waiting') self.skipWaiting();
    if (event.data === 'clear-caches') {
        event.waitUntil(Promise.all(KEEP.map((n) => caches.delete(n))));
    }
});
