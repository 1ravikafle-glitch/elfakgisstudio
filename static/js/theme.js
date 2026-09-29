/* ================================================================
   Elfak GIS Pro Studio — appearance (shared by /login and the studio)
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
    _dark = dark;
    const html = document.documentElement;
    const flash = document.getElementById('theme-flash');
    const icon = document.getElementById('theme-icon');
    const label = document.getElementById('theme-label');

    if (animate && flash) {
        flash.style.opacity = '1';
        setTimeout(() => { flash.style.opacity = '0'; }, 260);
    }

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
function _syncSegButtons() {
    document.querySelectorAll('.seg-btn[data-mode]').forEach(b => {
        b.setAttribute('aria-pressed', b.dataset.mode === _themeMode ? 'true' : 'false');
    });
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
    const btn = document.getElementById('theme-btn');
    if (btn) {
        const r = document.createElement('span');
        r.className = 'theme-ripple';
        r.style.cssText = 'width:80px;height:80px;left:50%;top:50%;margin:-40px 0 0 -40px';
        btn.appendChild(r);
        setTimeout(() => r.remove(), 500);
    }
    _applyTheme(!_dark, true);
}

// Sync the controls with what is actually on screen once the DOM is ready.
// persist=false: a render, not a user choice.
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.seg-btn[data-mode]').forEach(b => {
        b.addEventListener('click', () => setThemeMode(b.dataset.mode));
    });
    _applyTheme(_dark, false, false);
});
