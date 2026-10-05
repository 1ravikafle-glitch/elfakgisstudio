/* ================================================================
   Cross-site hand-off to Forestry PSC Preparation.
   ------------------------------------------------------------
   The "Forestry PSC" badge in the header sends the visitor to the
   sibling app. Without help they land on its sign-in page even though
   they are already authenticated here, because the two apps keep
   sessions in different places: this one in a Flask session cookie,
   the other in localStorage, which a redirect cannot populate.

   So on click we ask the server for a short-lived token naming the
   other app and put it in the URL. The sibling app verifies it, writes
   its own session and navigates on. The plain href is left in place so
   the link still works if this script fails, and middle-click and
   "open in new tab" behave exactly as before.
   ================================================================ */
(function () {
    'use strict';
    var FORESTRY = 'https://forestry-pscpreparation.onrender.com';

    document.addEventListener('click', function (e) {
        var badge = e.target && e.target.closest && e.target.closest('#forestry-badge');
        if (!badge) return;
        // Let the browser handle modified clicks (new tab, new window).
        if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;

        var username = badge.getAttribute('data-username') || '';
        if (!username) return; // not signed in: the plain link is correct

        e.preventDefault();

        // Open the tab while the click is still a user gesture. Awaiting the
        // hand-off first would lose the gesture and the popup blocker would
        // eat the window.
        //
        // The feature string must NOT contain "noopener": per spec that makes
        // window.open return null, and a null handle means the token we fetch
        // can never be applied to the tab, so it would sit on the sign-in page
        // with nothing to show for the request. Opening plainly and severing
        // opener ourselves gives identical protection with a usable handle.
        var w = window.open('', '_blank');
        if (!w) return;             // popup blocked: nothing sensible to do
        try { w.opener = null; } catch (err) { /* already severed */ }

        // Paint the blank tab so it does not sit as a white void for a moment.
        try {
            w.document.open();
            w.document.write('<!doctype html><meta charset="utf-8">' +
                '<title>Opening Forestry PSC</title>' +
                '<body style="font:14px system-ui,sans-serif;padding:2rem;color:#666">' +
                'Signing you in...</body>');
            w.document.close();
        } catch (err) { /* nothing to paint */ }

        fetch('/sso/forestry-handoff', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!w || w.closed) return;
                w.location.replace(
                    d && d.token
                        ? FORESTRY + '/auth/sso/exchange?t=' + encodeURIComponent(d.token)
                        : FORESTRY + '/desktop/'
                );
            })
            .catch(function () {
                if (w && !w.closed) w.location.replace(FORESTRY + '/desktop/');
            });
    });
})();
