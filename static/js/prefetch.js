/* ================================================================
   Elfak GIS Studio — background prefetch engine
   ------------------------------------------------------------
   The studio is heavy: ~160 KB of app JS, two stylesheets, Leaflet
   and four optional vendor libraries that the browser would
   otherwise only discover at the moment the user clicks the button
   that needs them.

   This file warms all of that BEFORE it is needed. It runs on the
   sign-in page, so the download overlaps the seconds a human spends
   typing a username and password.

   How it behaves:
   · One file at a time. Never parallel — the user is still typing
     and their connection belongs to them.
   · Large files are streamed and reported in 25% steps, with a
     5-10 s pause between steps. This keeps the transfer from
     saturating a slow or metered link.
   · Everything is polite: it waits for an idle moment, yields to a
     real user action, and stops entirely on a metered connection
     or in a background tab.
   · Nothing here is required. If any step fails the app still works,
     it just loads the library the old way when it is first clicked.
   ================================================================ */

(function (global) {
    'use strict';

    // Quarter-steps, and the window we wait between them.
    const STEP = 0.25;
    const GAP_MIN_MS = 5000;
    const GAP_MAX_MS = 10000;
    // Only pace a file big enough for pacing to be worth the complexity.
    const PACE_ABOVE_BYTES = 120 * 1024;

    const LS_KEY = 'elfak.prefetch.v1';
    const API = global.__elfakPrefetch = {
        done: {},          // url -> true, mirrors localStorage across loads
        listeners: [],
        cancelled: false,
        running: false,
    };

    // ── Asset manifest ────────────────────────────────────────────
    // Order is priority: everything the first screen needs comes first,
    // optional libraries last. The ?v= values are generated, so these
    // URLs are immutable and safe to cache for a year.
    const ASSETS = [
        { url: '/static/css/app.css?v=20260929-2',     kind: 'css' },
        { url: '/static/css/forestry.css?v=20260929-9', kind: 'css' },
        { url: '/static/js/dock.js?v=20260927-1',       kind: 'js'  },
        { url: '/static/js/dock-resize.js?v=20260927-1',kind: 'js'  },
        { url: '/static/js/app.js?v=20260929-8',        kind: 'js', big: true },
        { url: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js', kind: 'js' },
        { url: 'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css', kind: 'css' },
        { url: 'https://cdnjs.cloudflare.com/ajax/libs/jszip/3.10.1/jszip.min.js', kind: 'js' },
        { url: 'https://unpkg.com/@geoman-io/leaflet-geoman-free@2.16.0/dist/leaflet-geoman.min.js', kind: 'js' },
        { url: 'https://unpkg.com/@geoman-io/leaflet-geoman-free@2.16.0/dist/leaflet-geoman.css', kind: 'css' },
        { url: 'https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js', kind: 'js' },
        { url: 'https://cdnjs.cloudflare.com/ajax/libs/xlsx/0.18.5/xlsx.full.min.js', kind: 'js', big: true },
    ];

    // Subscribing is cheap and safe to do from any page that loads this
    // file; listeners only ever receive progress for the current session.
    API.onProgress = function (fn) { if (typeof fn === 'function') API.listeners.push(fn); };
    function emit(evt) {
        for (let i = 0; i < API.listeners.length; i++) {
            try { API.listeners[i](evt); } catch (_) { /* a bad listener must not stop the queue */ }
        }
    }

    // ── Persist across page loads ────────────────────────────────
    function loadState() {
        try {
            const raw = global.localStorage.getItem(LS_KEY);
            if (raw) return JSON.parse(raw) || {};
        } catch (_) { /* private mode / disabled storage */ }
        return {};
    }
    function saveState(state) {
        try { global.localStorage.setItem(LS_KEY, JSON.stringify(state)); } catch (_) {}
    }
    API.done = loadState();
    API.reset = function () {
        API.done = {};
        saveState({});
    };

    // ── Politeness checks ────────────────────────────────────────
    // A metered or slow connection belongs to the user's data plan, and a
    // background tab is not where we should spend it.
    function meteredOrSlow() {
        const c = global.navigator && global.navigator.connection;
        if (!c) return false;
        if (c.saveData === true) return true;
        return /(^|-)2g$/.test(c.effectiveType || '');
    }

    function sleep(ms) {
        return new Promise(function (resolve) { global.setTimeout(resolve, ms); });
    }
    function gap() {
        return GAP_MIN_MS + Math.random() * (GAP_MAX_MS - GAP_MIN_MS);
    }
    function whenIdle(timeout) {
        return new Promise(function (resolve) {
            if ('requestIdleCallback' in global) {
                global.requestIdleCallback(function () { resolve(); }, { timeout: timeout || 2500 });
            } else {
                global.setTimeout(resolve, 400);
            }
        });
    }

    // A prefetch must never be the reason the page feels busy. Before each
    // step we ask the scheduler whether the browser is waiting on real user
    // input; if it is, or the tab is hidden, the queue stands down and
    // resumes on the next idle moment rather than pushing through.
    function userIsBusy() {
        if (API.cancelled) return true;
        if (global.document && global.document.hidden) return true;
        if (meteredOrSlow()) return true;
        const s = global.navigator && global.navigator.scheduling;
        if (s && typeof s.isInputPending === 'function') {
            try { if (s.isInputPending()) return true; } catch (_) {}
        }
        return false;
    }
    API.resume = function () { API.cancelled = false; };

    // ── The paced downloader ─────────────────────────────────────
    // Reads the response as a stream and reports at every quarter. The
    // 5-10 s pause between quarters is the point: a 900 KB library lands
    // over roughly half a minute of background time instead of one burst
    // that competes with whatever the user is doing.
    async function fetchPaced(url, label) {
        const res = await global.fetch(url, {
            mode: 'cors',
            credentials: 'omit',
            // Let the HTTP cache do its job; this is a warm, not a download
            // we intend to use directly.
            cache: 'default',
        });
        if (!res.ok) throw new Error(label + ' → HTTP ' + res.status);

        const total = Number(res.headers.get('Content-Length')) || 0;
        const pace = total > PACE_ABOVE_BYTES && res.body && res.body.getReader;

        if (!pace) {
            // Small file: take it whole, no ceremony.
            await res.arrayBuffer();
            return { bytes: total, paced: false };
        }

        const reader = res.body.getReader();
        let received = 0;
        let nextStep = STEP;
        for (;;) {
            const chunk = await reader.read();
            if (chunk.done) break;
            received += chunk.value.length;
            const frac = total ? received / total : 1;
            // A single chunk can cross several quarter marks at once, so
            // report each boundary it passes rather than the overshoot —
            // the readout steps 25 → 50 → 75 → 100 as specified.
            //
            // Content-Length is the *encoded* size while the reader hands
            // back decoded bytes, so frac can exceed 1. The counter is
            // therefore monotonic and bounded, never driven by frac alone.
            while (nextStep <= 1 && frac >= nextStep) {
                const pct = Math.round(nextStep * 100);
                nextStep += STEP;
                emit({ label: label, pct: pct });
                if (nextStep > 1) break;   // last quarter, no pause needed
                // Hand the connection back for 5-10 s between quarters, and
                // stand the whole queue down if the user needs the thread.
                await sleep(gap());
                if (userIsBusy()) {
                    try { await reader.cancel(); } catch (_) {}
                    return { bytes: received, paced: true, aborted: true };
                }
            }
        }
        emit({ label: label, pct: 100 });
        return { bytes: received, paced: true };
    }

    // Fallback for browsers without streaming bodies: preload quietly.
    function preloadTag(asset) {
        return new Promise(function (resolve, reject) {
            const el = document.createElement(asset.kind === 'css' ? 'link' : 'script');
            if (asset.kind === 'css') {
                el.rel = 'stylesheet';
                el.href = asset.url;
            } else {
                el.src = asset.url;
                el.async = true;
            }
            el.onload = function () { resolve(); };
            el.onerror = function () { reject(new Error(asset.url)); };
            document.head.appendChild(el);
        });
    }

    // ── Queue ────────────────────────────────────────────────────
    async function runOne(asset) {
        if (API.done[asset.url]) return;
        const label = asset.url.split('/').pop().split('?')[0];
        try {
            await whenIdle(2500);
            if (userIsBusy()) return;
            emit({ label: label, pct: 0 });
            if (global.fetch && global.ReadableStream) {
                await fetchPaced(asset.url, label);
            } else {
                await preloadTag(asset);
            }
            API.done[asset.url] = 1;
            saveState(API.done);
            emit({ label: label, pct: 100, done: true });
        } catch (e) {
            // A miss is not a failure. The library loads on demand later.
        }
    }

    /**
     * Warm the given assets (default: the whole manifest), one at a time.
     * Safe to call more than once; already-warmed URLs are skipped.
     */
    API.warm = async function (assets) {
        if (API.running) return;
        API.running = true;
        API.resume();
        try {
            for (let i = 0; i < assets.length; i++) {
                if (API.cancelled) break;
                await runOne(assets[i]);
            }
        } finally {
            API.running = false;
        }
    };

    /** True when every URL in the list is already warm. */
    API.isWarm = function (list) {
        return (list || ASSETS).every(function (a) { return !!API.done[a.url]; });
    };

    API.assets = ASSETS;

    // The app shell is what the first screen needs, so it goes first.
    API.critical = ASSETS.filter(function (a) { return a.url.indexOf('https://') === -1; });

})(window);
