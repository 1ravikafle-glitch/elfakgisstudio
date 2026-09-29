/* ================================================================
   Elfak GIS Studio — appearance (shared by /login and the studio)
   Loaded first on every page so the sign-in page and the app can never
   disagree about light or dark.

   Forestry PSC offers three modes — Light, Dark and System — behind a
   segmented control, and "System" is the default there. The studio keeps
   that exact model: the header's pill is a two-way shortcut, the sign-in
   page shows the full three-way selector, and both write the same
   'elfak-theme' key so a visitor's choice follows them between pages.
   ================================================================ */

// ── Apply theme IMMEDIATELY (before first paint, no flash) ──
// No stored choice means "system": follow the OS, like Forestry PSC.
(function () {
    const stored = localStorage.getItem('elfak-theme');
    const dark = stored === 'dark' ||
        (stored !== 'light' && window.matchMedia('(prefers-color-scheme: dark)').matches);
    if (dark) document.documentElement.setAttribute('data-theme', 'dark');
})();

// 'light' | 'dark' | 'system' — 'system' is the default and keeps
// tracking the OS live, so a device that flips at sunset flips here too.
const _sysDark = window.matchMedia('(prefers-color-scheme: dark)');
let _themeMode = localStorage.getItem('elfak-theme') || 'system';
let _dark = _themeMode === 'dark' || (_themeMode !== 'light' && _sysDark.matches);
_sysDark.addEventListener('change', e => {
    if (_themeMode === 'system') _applyTheme(e.matches, false, false);
});

// Browser chrome on mobile follows the app, so the address bar does not
// stay light while the page is dark.
function _syncThemeColor(dark) {
    const meta = document.querySelector('meta[name="theme-color"]:not([media])')
        || _upsertThemeColor();
    if (meta) meta.setAttribute('content', dark ? '#18181b' : '#fafafa');
}

function _upsertThemeColor() {
    const m = document.createElement('meta');
    m.setAttribute('name', 'theme-color');
    document.head.appendChild(m);
    return m;
}

function _applyTheme(dark, animate, persist = true) {
    if (!animate) { _swapTheme(dark, persist); return; }
    _transitionTheme(() => _swapTheme(dark, persist));
}

// The actual mutation. Kept separate from _applyTheme so it can be handed
// to startViewTransition as a callback, which requires the swap to be a
// single synchronous block.
function _swapTheme(dark, persist) {
    _dark = dark;
    const html = document.documentElement;
    const icon = document.getElementById('theme-icon');
    const label = document.getElementById('theme-label');

    // The old #theme-flash overlay is deliberately not used here. A
    // full-screen element inside a View Transition gets baked into the
    // snapshot, so it would paint a flat rectangle over the reveal
    // instead of letting the circle show the new theme through.

    if (dark) {
        html.setAttribute('data-theme', 'dark');
        if (icon) { icon.textContent = '☀️'; icon.style.transform = 'rotate(180deg) scale(1.2)'; }
        if (label) label.textContent = 'Light';
    } else {
        html.removeAttribute('data-theme');
        if (icon) { icon.textContent = '🌙'; icon.style.transform = 'rotate(0deg) scale(1)'; }
        if (label) label.textContent = 'Dark';
    }

    // Tell the truth about the mode while nobody has chosen yet: the button
    // still shows the action, but the tooltip admits it is following the
    // device, so the label is never a lie.
    const btn = document.getElementById('theme-btn');
    if (btn) {
        btn.title = _themeMode === 'system'
            ? `Following your device (${dark ? 'dark' : 'light'}) — click to switch`
            : `Switch to ${dark ? 'light' : 'dark'}`;
        btn.setAttribute('aria-label', btn.title);
    }

    // An OS-driven change must not become a stored choice, or a "system"
    // visitor would silently get pinned on the first flip.
    if (persist) {
        localStorage.setItem('elfak-theme', dark ? 'dark' : 'light');
        _themeMode = dark ? 'dark' : 'light';
    }

    _syncThemeColor(dark);
    _syncSegButtons();

    // Basemap tiles are photographic: a touch of transparency in dark mode
    // keeps the chrome from glaring against them.
    try {
        if (typeof leafMap !== 'undefined' && leafMap) {
            leafMap.eachLayer(l => { if (l && l._url) l.setOpacity(dark ? 0.80 : 1.0); });
        }
    } catch (_e) {}
}

// ── The three-way selector (sign-in page) ──
// The mode the thumb was last drawn for. Used to tell a genuine change
// of selection (slide) from a re-render of the same one (no motion), so
// first paint and OS-driven updates stay still.
let _segRenderedMode = null;

function _syncSegButtons() {
    document.querySelectorAll('.seg-btn[data-mode]').forEach(b => {
        b.setAttribute('aria-pressed', b.dataset.mode === _themeMode ? 'true' : 'false');
    });
    const changed = _segRenderedMode !== _themeMode;
    _segRenderedMode = _themeMode;
    _moveSegThumbs(changed);
}

/* ── Travelling selection highlight ───────────────────────────────
   The control used to repaint the newly selected button in place, so
   choosing a different option was a blink rather than a move. One
   .seg-thumb is measured against the real button geometry and then
   translated, so the highlight slides across the options in between
   and settles on the target with the same spring as everything else.

   Geometry is read from the DOM rather than computed from a ratio, so
   the thumb stays exact when the labels are different widths, when the
   font loads late, or when the window resizes. */
function _segReduceMotion() { return _reduceMotion(); }

function _placeThumb(seg, btn, animate) {
    const thumb = seg.querySelector('.seg-thumb');
    if (!thumb || !btn) return;
    if (!animate) {
        // Suppress the transition for this frame so repositioning after a
        // resize or first paint is instant rather than a long slide.
        thumb.style.transition = 'none';
    }
    thumb.style.width = btn.offsetWidth + 'px';
    thumb.style.height = btn.offsetHeight + 'px';
    thumb.style.transform = 'translate3d(' + btn.offsetLeft + 'px,' + btn.offsetTop + 'px,0)';
    if (!animate) {
        // Force a style flush before restoring, otherwise the browser
        // coalesces both writes and the thumb slides on first paint.
        void thumb.offsetWidth;
        thumb.style.transition = '';
    }
}

function _moveSegThumbs(animate) {
    document.querySelectorAll('.seg').forEach(seg => {
        if (!seg.querySelector('.seg-thumb')) {
            const t = document.createElement('span');
            t.className = 'seg-thumb';
            t.setAttribute('aria-hidden', 'true');
            seg.appendChild(t);
        }
        const active = seg.querySelector('.seg-btn[aria-pressed="true"]')
                   || seg.querySelector('.seg-btn[data-mode="' + _themeMode + '"]');
        _placeThumb(seg, active, animate && !_segReduceMotion());
    });
}

function _initSegThumbs() {
    _moveSegThumbs(false);
    // Font loading and container changes both alter the button boxes.
    if (document.fonts && document.fonts.ready) {
        document.fonts.ready.then(() => _moveSegThumbs(false)).catch(() => {});
    }
    if (typeof ResizeObserver === 'function') {
        const ro = new ResizeObserver(() => _moveSegThumbs(false));
        document.querySelectorAll('.seg').forEach(s => ro.observe(s));
    } else {
        window.addEventListener('resize', () => _moveSegThumbs(false));
    }
}

function setThemeMode(mode) {
    if (mode !== 'light' && mode !== 'dark' && mode !== 'system') return;
    _themeMode = mode;
    localStorage.setItem('elfak-theme', mode);
    _applyTheme(mode === 'dark' || (mode === 'system' && _sysDark.matches), true, false);
}

// The header pill: a two-way shortcut that pins the opposite of what is
// on screen. Before the first press the app was tracking the OS, and this
// is where that ends.
function toggleTheme() {
    _applyTheme(!_dark, true);
}

/* ── Animated theme swap ──────────────────────────────────────────
   A theme change repaints every colour in one frame, which is what
   makes a light/dark toggle feel cheap.

   The fix is to let the palette *interpolate*, so the interface is
   seen travelling from one mode to the other through every
   intermediate shade. motion.css registers the design tokens with
   @property and puts one transition on :root; simply toggling the
   attribute then tweens the whole system at once, and every element
   consuming a token follows along.

   A scoped cross-fade stands in on engines without @property, and
   reduced-motion skips both and swaps instantly.
   */
function _reduceMotion() {
    try { return matchMedia('(prefers-reduced-motion: reduce)').matches; }
    catch (_e) { return false; }
}

function _supportsTween() {
    return typeof CSS !== 'undefined'
        && typeof CSS.registerProperty === 'function';
}

function _transitionTheme(mutate) {
    if (_reduceMotion()) { mutate(); return; }

    if (_supportsTween()) {
        // Arming the class is what makes the change animate; it stays on
        // afterwards so later toggles are equally smooth.
        document.documentElement.classList.add('theme-tween');
        mutate();
        return;
    }

    document.body.classList.add('theme-fade');
    mutate();
    setTimeout(() => document.body.classList.remove('theme-fade'), 460);
}

// Sync the controls with what is actually on screen once the DOM is ready.
// persist=false: a render, not a user choice.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.seg-btn[data-mode]').forEach(b => {
        b.addEventListener('click', () => setThemeMode(b.dataset.mode));
    });
    _applyTheme(_dark, false, false);
    // Drawn after the first paint so the thumb lands without animating in.
    _initSegThumbs();
});
