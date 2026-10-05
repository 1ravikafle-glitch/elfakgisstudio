/* ================================================================
   Handlers that were multi-statement inline bodies.
   ------------------------------------------------------------
   CSP forbids inline handlers, so these eight could not stay as
   onchange="..." attribute text. They are the only ones in the app that
   were not a single function call, so they get real functions here and the
   templates point at them by name via the data- attributes handled by
   actions.js. Bodies are otherwise unchanged from the originals.
   ================================================================ */
(function (global) {
    'use strict';

    /* onchange on the zone select: 'EPSG:326' + value. */
    global.__setCrs = function () {
        var sel = document.getElementById('crs-select') || this;
        var hdr = document.getElementById('hdr-crs');
        if (hdr) hdr.textContent = 'EPSG:326' + sel.value;
    };

    /* oninput on the edge-node count: clamp 2..15 and mirror the value. */
    global.__clampEdgeN = function () {
        var el = this;
        el.value = Math.max(2, Math.min(15, parseInt(el.value) || 4));
        var out = document.getElementById('e-n-disp');
        if (out) out.textContent = el.value;
    };

    /* onchange on the DEM file input: show the filename, reveal the drop zone. */
    global.__demChosen = function () {
        var input = this;
        if (!input.files || !input.files[0]) return;
        global._demCacheKey = '';
        var lbl = document.getElementById('f-dem-lbl');
        if (lbl) lbl.textContent = '\u{1F4C4} ' + input.files[0].name;
        var dz = document.getElementById('dz-F-dem');
        if (dz) { dz.style.opacity = '1'; dz.style.filter = 'none'; }
    };

    /* Navigating must not lose the in-page state first. */
    global.__downloadRun = function () {
        global.location.href = '/download/' + global.currentRunId;
    };

    /* A styled label cannot open a hidden file input on its own. */
    global.__pickLayout = function () {
        var input = document.getElementById('layout-file-input');
        if (input) input.click();
    };

    /* popup with noopener so the opened tab cannot reach back into ours. */
    global.__openOutImage = function () {
        var img = document.getElementById('out-img');
        if (!img) return;
        var w = global.open(img.src, '_blank', 'noopener,noreferrer');
        if (w) w.opener = null;
    };

    /* Rows handle their own clicks; the container must not also react. */
    global.__stopBubble = function (event) {
        if (event) event.stopPropagation();
    };
})(window);