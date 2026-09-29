/* ================================================================
   Elfak GIS Studio — travelling selection
   ------------------------------------------------------------
   One highlight that MOVES between options instead of each option
   repainting its own selected state.

   The pattern comes from the desktop shell in question-bank-unified,
   where the sidebar renders a single absolutely-positioned pill inside
   the active item and gives it a Framer Motion `layoutId`. The shared
   id makes the browser morph the pill's box from wherever it was to
   wherever it now is, so the selection is seen travelling across the
   options it passes rather than blinking from one place to another.

   This is the same idea without a framework: one empty element, sized
   and positioned from the real geometry of the active item, translated
   with a compositor-friendly transform. Geometry is measured rather
   than calculated so the thumb stays exact when labels differ in
   width, when a webfont swaps in late, or when the window resizes.

   Markup contract:
     <div data-travel data-travel-active=".is-active">
       <div data-travel-item>…</div>
       <div data-travel-item class="is-active">…</div>
     </div>
   ================================================================ */

(function (global) {
    'use strict';

    function reduceMotion() {
        try { return matchMedia('(prefers-reduced-motion: reduce)').matches; }
        catch (_e) { return false; }
    }

    // Size and move the thumb. Suppressing the transition for a single
    // frame is how first paint, resizes and font swaps stay instant
    // instead of animating a long slide to a position that was always
    // correct.
    function place(box, item, animate) {
        if (!box || !item) return;
        if (!animate) box.style.transition = 'none';
        box.style.width = item.offsetWidth + 'px';
        box.style.height = item.offsetHeight + 'px';
        box.style.transform =
            'translate3d(' + item.offsetLeft + 'px,' + item.offsetTop + 'px,0)';
        if (!animate) {
            // Force a style flush before restoring, or the browser
            // coalesces both writes and the first paint slides.
            void box.offsetWidth;
            box.style.transition = '';
        }
    }

    function ensure(host) {
        if (host._travelThumb) return host._travelThumb;
        const t = document.createElement('span');
        t.className = 'travel-thumb';
        t.setAttribute('aria-hidden', 'true');
        host.appendChild(t);
        host._travelThumb = t;
        return t;
    }

    function activeItem(host) {
        const sel = host.getAttribute('data-travel-active');
        if (sel) {
            const found = host.querySelector(sel);
            if (found) return found;
        }
        return host.querySelector('[data-travel-item][aria-pressed="true"]')
            || host.querySelector('[data-travel-item].active');
    }

    /** Move the highlight of one host. animate=false snaps. */
    function sync(host, animate) {
        if (!host) return;
        // The thumb is positioned absolutely, so the host must establish
        // a containing block or it would resolve against the page.
        if (getComputedStyle(host).position === 'static') host.style.position = 'relative';
        place(ensure(host), activeItem(host), animate && !reduceMotion());
    }

    function hosts() {
        return document.querySelectorAll('[data-travel]');
    }

    /** Re-measure every host. animate=false for layout-driven changes. */
    function syncAll(animate) {
        hosts().forEach(h => sync(h, animate));
    }

    function init() {
        const list = hosts();
        if (!list.length) return;
        syncAll(false);

        // A late webfont changes every box, so the thumb would otherwise
        // be sized against the fallback face.
        if (document.fonts && document.fonts.ready) {
            document.fonts.ready.then(() => syncAll(false)).catch(() => {});
        }

        if (typeof ResizeObserver === 'function') {
            const ro = new ResizeObserver(() => syncAll(false));
            list.forEach(h => ro.observe(h));
        } else {
            global.addEventListener('resize', () => syncAll(false));
        }
    }

    global.Travel = { init: init, sync: sync, syncAll: syncAll, hosts: hosts };

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
})(window);
