/* ================================================================
   Elfak GIS Studio — boot
   ------------------------------------------------------------
   The last script on the page. It does two jobs that must not delay
   anything the user can see:

   1. Registers the service worker, so the next visit is served from
      cache instead of the network.
   2. Tops up the background warm-up for anything the sign-in page
      did not get to — typically because the visitor signed in fast,
      or arrived on a direct link.

   Registration is deliberately lazy and failure-tolerant: a browser
   without service workers, or a user who blocks them, simply runs
   without the cache and loses nothing.
   ================================================================ */

(function () {
    'use strict';

    // ── Service worker ───────────────────────────────────────────
    if ('serviceWorker' in navigator && location.protocol !== 'file:') {
        window.addEventListener('load', function () {
            navigator.serviceWorker.register('/sw.js', { scope: '/' })
                .catch(function () { /* private mode, blocked, or no TLS */ });
        });
    }

    // ── Top up the warm-up ───────────────────────────────────────
    function topUp() {
        var P = window.__elfakPrefetch;
        if (!P) return;
        // The sign-in page usually did this already; warm() skips
        // anything it recorded and only fetches the remainder.
        P.warm(P.assets).catch(function () {});
    }

    if (document.readyState === 'complete') topUp();
    else window.addEventListener('load', function () { setTimeout(topUp, 400); });
})();
