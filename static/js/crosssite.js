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
        var w = window.open(FORESTRY + '/desktop/', '_blank', 'noopener,noreferrer');
        if (w) w.opener = null;

        fetch('/sso/forestry-handoff', { credentials: 'same-origin' })
            .then(function (r) { return r.ok ? r.json() : null; })
            .then(function (d) {
                if (!w || !d || !d.token) return;
                w.location.replace(
                    FORESTRY + '/auth/sso/exchange?t=' + encodeURIComponent(d.token)
                );
            })
            .catch(function () { /* plain destination already open */ });
    });
})();
