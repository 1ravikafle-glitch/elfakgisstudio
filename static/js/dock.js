    /* ── Draggable dashboard: reorder sections + resize widths (Apple: 1:1 track, interruptible) ── */
    (function(){
        const KEY = 'elfak-dock-v1';
        // Owned by dock-resize.js. Declared here only so "reset layout" can
        // clear it; dock.js must never write it or the two clobber each other.
        const HEIGHT_KEY = 'elfak-dock-heights-v1';
        const main = document.querySelector('.main');
        if (!main) return;
        const reduceMotion = () => matchMedia('(prefers-reduced-motion: reduce)').matches;
        const sections = () => [...main.querySelectorAll(':scope > .dock-section')];
        const dividers = () => [...main.querySelectorAll(':scope > .dock-divider')];

        function save(order, widths) {
            // Merge rather than overwrite: dock-resize.js also writes `widths`
            // into this same key, and a bare overwrite would drop fields the
            // other module owns.
            try {
                let cur = {};
                try { cur = JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) {}
                localStorage.setItem(KEY, JSON.stringify({ ...cur, order, widths }));
            } catch (_) {}
        }
        function load() {
            try { return JSON.parse(localStorage.getItem(KEY) || 'null'); } catch (_) { return null; }
        }
        function applyState(st) {
            if (!st) return;
            if (Array.isArray(st.order)) {
                const byKey = {};
                sections().forEach(s => byKey[s.dataset.dock] = s);
                st.order.forEach(k => { if (byKey[k]) main.appendChild(byKey[k]); });
                // re-insert dividers between sections
                dividers().forEach(d => d.remove());
                const secs = sections();
                secs.forEach((s, i) => {
                    if (i < secs.length - 1) {
                        const d = document.createElement('div');
                        d.className = 'dock-divider';
                        d.dataset.dockDivider = '';
                        d.title = 'Drag to resize';
                        s.after(d);
                    }
                });
            }
            if (st.widths) {
                sections().forEach(s => {
                    const w = st.widths[s.dataset.dock];
                    if (w && window.innerWidth > 1100) { s.style.flex = `0 0 ${w}%`; s.style.maxWidth = w + '%'; }
                });
            }
            wireAll();
        }
        function currentState() {
            const order = sections().map(s => s.dataset.dock);
            const widths = {};
            const total = main.clientWidth || 1;
            sections().forEach(s => { widths[s.dataset.dock] = +(s.getBoundingClientRect().width / total * 100).toFixed(2); });
            return { order, widths };
        }

        // — Reorder: drag a section by its handle, drop between sections —
        function wireReorder() {
            main.querySelectorAll('[data-dock-handle]').forEach(h => {
                const sec = h.closest('.dock-section');
                h.addEventListener('pointerdown', e => {
                    if (e.button !== 0) return;
                    e.preventDefault();
                    h.setPointerCapture(e.pointerId);
                    sec.classList.add('dock-dragging');
                    const ghost = sec.cloneNode(false);
                    let target = null;
                    const move = ev => {
                        const secs = sections().filter(s => s !== sec);
                        target = null;
                        for (const s of secs) {
                            const r = s.getBoundingClientRect();
                            const horizontal = window.innerWidth > 1100;
                            const after = horizontal
                                ? (ev.clientX > r.left + r.width / 2)
                                : (ev.clientY > r.top + r.height / 2);
                            s.classList.remove('dock-drop-target');
                            if ((horizontal && ev.clientX > r.left - 20 && ev.clientX < r.right + 20) ||
                                (!horizontal && ev.clientY > r.top - 20 && ev.clientY < r.bottom + 20)) {
                                target = { el: s, after };
                            }
                        }
                        if (target) target.el.classList.add('dock-drop-target');
                    };
                    const up = ev => {
                        h.removeEventListener('pointermove', move);
                        h.removeEventListener('pointerup', up);
                        h.removeEventListener('pointercancel', up);
                        sec.classList.remove('dock-dragging');
                        sections().forEach(s => s.classList.remove('dock-drop-target'));
                        if (target) {
                            if (target.after) target.el.after(sec);
                            else target.el.before(sec);
                            // rebuild dividers
                            document.querySelectorAll('.dock-divider').forEach(d => d.remove());
                            const secs = sections();
                            secs.forEach((s, i) => {
                                if (i < secs.length - 1) {
                                    const d = document.createElement('div');
                                    d.className = 'dock-divider';
                                    d.title = 'Drag to resize';
                                    s.after(d);
                                }
                            });
                            wireResize();
                            const st = currentState();
                            save(st.order, load()?.widths || st.widths);
                        }
                        try { ghost.remove(); } catch (_) {}
                    };
                    h.addEventListener('pointermove', move);
                    h.addEventListener('pointerup', up);
                    h.addEventListener('pointercancel', up);
                });
                h.addEventListener('dblclick', () => {
                    // Reset means reset: heights live in their own key (owned by
                    // dock-resize.js) and must go too, or a "reset" layout keeps
                    // the stale section heights.
                    try {
                        localStorage.removeItem(KEY);
                        localStorage.removeItem(HEIGHT_KEY);
                    } catch (_) {}
                    location.reload();
                });
                h.title = 'Drag to reorder · Double-click to reset layout';
            });
        }

        // — Resize: drag dividers to flex widths (desktop row only) —
        function wireResize() {
            main.querySelectorAll('.dock-divider').forEach(div => {
                div.onpointerdown = e => {
                    if (window.innerWidth <= 1100) return;
                    e.preventDefault();
                    div.setPointerCapture(e.pointerId);
                    div.classList.add('active');
                    const prev = div.previousElementSibling;
                    const next = div.nextElementSibling;
                    if (!prev || !next) return;
                    const mainRect = main.getBoundingClientRect();
                    const startX = e.clientX;
                    const prevW = prev.getBoundingClientRect().width;
                    const nextW = next.getBoundingClientRect().width;
                    const move = ev => {
                        const dx = ev.clientX - startX;
                        const total = mainRect.width || 1;
                        const pw = Math.min(Math.max(prevW + dx, 200), total - 400);
                        const nw = Math.min(Math.max(nextW - dx, 280), total - 400);
                        prev.style.flex = `0 0 ${pw / total * 100}%`;
                        prev.style.maxWidth = (pw / total * 100) + '%';
                        next.style.flex = `0 0 ${nw / total * 100}%`;
                        next.style.maxWidth = (nw / total * 100) + '%';
                    };
                    const up = () => {
                        div.removeEventListener('pointermove', move);
                        div.removeEventListener('pointerup', up);
                        div.classList.remove('active');
                        const st = currentState();
                        save(st.order, st.widths);
                    };
                    div.addEventListener('pointermove', move);
                    div.addEventListener('pointerup', up);
                    div.addEventListener('pointercancel', up);
                };
            });
        }
        function wireAll() { wireReorder(); wireResize(); }
        applyState(load());
        wireAll();
        if (reduceMotion()) main.classList.add('reduce-motion');
    })();
