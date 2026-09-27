    /* ── v3: every dashboard section resizable (right / bottom / corner handles) ── */
    (function(){
        const KEY = 'elfak-dock-v1';
        const main = document.querySelector('.main');
        if (!main) return;
        const isMobile = () => window.innerWidth <= 1100;
        const sections = () => [...main.querySelectorAll(':scope > .dock-section')];
        const store = {
            load() { try { return JSON.parse(localStorage.getItem(KEY) || '{}'); } catch (_) { return {}; } },
            save(patch) { try { localStorage.setItem(KEY, JSON.stringify({ ...this.load(), ...patch })); } catch (_) {} }
        };
        function applyHeights() {
            if (isMobile()) return;
            const st = store.load();
            if (st.heights) sections().forEach(s => {
                const h = st.heights[s.dataset.dock];
                if (h) s.style.height = h + 'px';
            });
        }
        function ensureHandles() {
            sections().forEach(sec => {
                if (getComputedStyle(sec).position === 'static') sec.style.position = 'relative';
                ['e', 's', 'se'].forEach(dir => {
                    if (!sec.querySelector(`:scope > .dock-resize-${dir}`)) {
                        const h = document.createElement('div');
                        h.className = `dock-resize dock-resize-${dir}`;
                        h.title = dir === 'e' ? 'Drag to resize width' : dir === 's' ? 'Drag to resize height' : 'Drag to resize';
                        sec.appendChild(h);
                        wireHandle(sec, h, dir);
                    }
                });
            });
        }
        function wireHandle(sec, handle, dir) {
            handle.addEventListener('pointerdown', e => {
                if (isMobile() || e.button !== 0) return;
                e.preventDefault(); e.stopPropagation();
                handle.setPointerCapture(e.pointerId);
                handle.classList.add('active'); sec.classList.add('resizing');
                // Freeze current size so flex doesn't fight the drag
                const r = sec.getBoundingClientRect();
                const mainW = main.clientWidth || 1;
                sec.style.flex = `0 0 ${r.width / mainW * 100}%`;
                sec.style.maxWidth = (r.width / mainW * 100) + '%';
                if (!sec.style.height) sec.style.height = r.height + 'px';
                const startX = e.clientX, startY = e.clientY;
                const startW = r.width, startH = r.height;
                const move = ev => {
                    if (dir === 'e' || dir === 'se') {
                        const w = Math.min(Math.max(startW + (ev.clientX - startX), 220), mainW * 0.75);
                        sec.style.flex = `0 0 ${w / mainW * 100}%`;
                        sec.style.maxWidth = (w / mainW * 100) + '%';
                    }
                    if (dir === 's' || dir === 'se') {
                        const h = Math.min(Math.max(startH + (ev.clientY - startY), 240), (main.clientHeight || 800));
                        sec.style.height = h + 'px';
                    }
                };
                const up = () => {
                    handle.removeEventListener('pointermove', move);
                    handle.removeEventListener('pointerup', up);
                    handle.removeEventListener('pointercancel', up);
                    handle.classList.remove('active'); sec.classList.remove('resizing');
                    const widths = {}, heights = {};
                    sections().forEach(s => {
                        widths[s.dataset.dock] = +(s.getBoundingClientRect().width / (main.clientWidth || 1) * 100).toFixed(2);
                        if (s.style.height) heights[s.dataset.dock] = Math.round(parseFloat(s.style.height));
                    });
                    store.save({ widths, heights });
                };
                handle.addEventListener('pointermove', move);
                handle.addEventListener('pointerup', up);
                handle.addEventListener('pointercancel', up);
            });
        }
        ensureHandles();
        applyHeights();
        new MutationObserver(() => ensureHandles()).observe(main, { childList: true });
        window.addEventListener('resize', () => { if (isMobile()) sections().forEach(s => { s.style.height = ''; }); });
    })();
