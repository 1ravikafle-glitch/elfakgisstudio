/* ================================================================
   Elfak GIS Pro Studio — sign-in page
   The studio itself is behind a real session, so this file's only job is
   to get a valid session and then hand the browser to "/". A hard refresh
   while signed in never reaches this page.
   ================================================================ */

const BASE = window.location.origin;

async function fetchJSON(url, options = {}) {
    const res = await fetch(url, options);
    if (!res.ok) {
        let msg = `Request failed (${res.status})`;
        try {
            const j = await res.json();
            if (j && j.error) msg = j.error;
        } catch (_e) { /* body was not JSON */ }
        throw new Error(msg);
    }
    try { return await res.json(); } catch (_e) { return {}; }
}

// The reference swaps the label for a spinner while the request is in
// flight. Same idea, no innerHTML round-trip on the success path.
const _SPIN = '<span class="spinner" aria-hidden="true"></span> Signing in…';

function _setBtn(btn, text) {
    if (!btn) return;
    btn.textContent = text;
    btn.classList.toggle('is-loading', text === 'Signing in…');
}

let _hintTimer = null;

// The reference form reveals the password from a 32px button sitting in
// the field's right gutter; the studio keeps that control.
function togglePw() {
    const pw = document.getElementById('login-pw');
    const btn = document.getElementById('pw-toggle');
    if (!pw) return;
    const show = pw.type === 'password';
    pw.type = show ? 'text' : 'password';
    if (btn) {
        btn.setAttribute('aria-pressed', show ? 'true' : 'false');
        btn.setAttribute('aria-label', show ? 'Hide password' : 'Show password');
    }
    const eye = document.getElementById('pw-eye');
    if (eye) {
        // Same 24px grid, minus the pupil when hidden — the strikethrough
        // version of the same icon rather than a second artwork set.
        eye.innerHTML = show
            ? '<path d="M3 3l18 18"/><path d="M10.6 5.1A9.8 9.8 0 0 1 12 5c6.4 0 10 7 10 7a17.7 17.7 0 0 1-3.4 4.2"/><path d="M6.6 6.7A17.6 17.6 0 0 0 2 12s3.6 7 10 7a9.7 9.7 0 0 0 4.2-.9"/>'
            : '<path d="M2 12s3.6-7 10-7 10 7 10 7-3.6 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/>';
    }
    pw.focus();
}

function _checkUsernameHint(val) {
    const hint = document.getElementById('login-hint');
    if (!hint) return;
    clearTimeout(_hintTimer);
    if (!val || val.length < 2) { hint.textContent = ''; return; }
    if (val.length > 40) {
        hint.textContent = 'Maximum 40 characters';
        hint.style.color = 'var(--red)';
        return;
    }
    if (!/^[A-Za-z0-9][A-Za-z0-9 _-]*$/.test(val)) {
        hint.textContent = 'Letters, numbers, spaces, - or _ only. Must start with letter/digit.';
        hint.style.color = 'var(--red)';
        return;
    }
    hint.textContent = '✓ Valid username format';
    hint.style.color = 'var(--accent)';
}

let _suggs = [];

function trySugg(i) {
    const n = _suggs[i];
    if (!n) return;
    const inp = document.getElementById('login-inp');
    inp.value = n;
    inp.classList.remove('taken');
    document.getElementById('lerr').style.display = 'none';
    document.getElementById('sugg').style.display = 'none';
    _checkUsernameHint(n);
    doLogin();
}

function _genSuggs(base) {
    const clean = base.replace(/[_-]?\d+$/, '').trim() || base;
    const yr = new Date().getFullYear() % 100;
    const rn = Math.floor(Math.random() * 89 + 10);
    return [clean + '_' + rn, clean + yr, clean + '_GIS'];
}

function _showError(msg) {
    const errEl = document.getElementById('lerr');
    errEl.className = 'login-err';
    errEl.textContent = msg;
    errEl.style.display = 'block';
}

async function doLogin() {
    const inp = document.getElementById('login-inp');
    const pwEl = document.getElementById('login-pw');
    const name = inp.value.trim();
    const password = pwEl ? pwEl.value : '';
    const errEl = document.getElementById('lerr');
    const sugg = document.getElementById('sugg');
    const btn = document.getElementById('login-go');
    const hint = document.getElementById('login-hint');

    inp.classList.remove('taken');
    errEl.className = 'login-err';
    errEl.style.display = 'none';
    sugg.style.display = 'none';

    if (!name) { _showError('Please enter a username.'); return; }
    if (name.length < 2) { _showError('Username must be at least 2 characters.'); return; }
    if (!/^[A-Za-z0-9][A-Za-z0-9 _-]*$/.test(name)) {
        _showError('Invalid characters. Use letters, numbers, spaces, - or _.');
        return;
    }
    if (!password) {
        if (pwEl) { pwEl.classList.add('taken'); pwEl.focus(); }
        _showError('Please enter your password.');
        return;
    }
    pwEl.classList.remove('taken');

    btn.disabled = true;
    btn.innerHTML = _SPIN;
    if (hint) { hint.textContent = 'Connecting to server…'; hint.style.color = 'var(--muted)'; }

    try {
        const data = await fetchJSON(`${BASE}/login`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ username: name, password: password })
        });

        if (data.taken) {
            inp.classList.add('taken');
            errEl.className = 'login-err taken-msg';
            errEl.textContent = data.error || `Could not sign in as "${name}".`;
            errEl.style.display = 'block';
            _suggs = _genSuggs(name);
            const spans = sugg.querySelectorAll('span');
            _suggs.forEach((s, i) => { if (spans[i]) spans[i].textContent = s; });
            sugg.style.display = 'block';
            if (hint) { hint.textContent = 'Choose a different name below:'; hint.style.color = 'var(--amber-text)'; }
            btn.disabled = false;
            _setBtn(btn, 'Sign in');
            return;
        }

        if (data.error) throw new Error(data.error);

        // Don't leave the password sitting in the DOM.
        pwEl.value = '';
        if (hint) {
            hint.textContent = data.is_new ? '✓ Account created!' : `✓ Welcome back, ${data.username}!`;
            hint.style.color = 'var(--accent)';
        }
        _setBtn(btn, 'Enter Studio →');
        // Hand over to the app. The session (and the 30-day cookie) is set,
        // so the studio renders directly — no login markup ever appears.
        setTimeout(() => { window.location.replace('/'); }, 260);
    } catch (e) {
        _showError(e.message || 'Connection error. Please try again.');
        if (hint) hint.textContent = '';
        btn.disabled = false;
        _setBtn(btn, 'Sign in');
    }
}

document.addEventListener('DOMContentLoaded', () => {
    // The username field carries autofocus, matching the reference form.
    // Only reach for the password when the username is already filled in
    // (a returning visitor's browser autofill, or ?u= prefill).
    const inp = document.getElementById('login-inp');
    const pw = document.getElementById('login-pw');
    if (inp && pw && inp.value) pw.focus();
});
