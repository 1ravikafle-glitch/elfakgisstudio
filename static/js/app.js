        /* ================================================================
           ELFAK GIS PRO STUDIO — FULL APPLICATION JS
           (Includes layout editor, export, modules A-H, login, history, etc.)
           ================================================================ */

        // ── Apply theme IMMEDIATELY ──
        (function() {
            if (localStorage.getItem('elfak-theme') === 'dark') {
                document.documentElement.setAttribute('data-theme', 'dark');
            }
        })();

        const BASE = window.location.origin;

        // ── robust JSON fetch wrapper ──
        async function fetchJSON(url, options = {}) {
            const res = await fetch(url, options);
            if (!res.ok) {
                const text = await res.text();
                let errMsg = `Server error ${res.status}`;
                try {
                    const data = JSON.parse(text);
                    errMsg = data.error || errMsg;
                } catch (_) { /* ignore */ }
                throw new Error(errMsg);
            }
            const contentType = res.headers.get('content-type') || '';
            if (!contentType.includes('application/json')) {
                const text = await res.text();
                throw new Error(`Expected JSON but got ${contentType}: ${text.slice(0, 100)}`);
            }
            return res.json();
        }

        // ── parse columns from CSV or Excel ──────────────────────────────
        async function parseFileColumns(file) {
            const name = file.name.toLowerCase();
            if (name.endsWith('.csv')) {
                const txt = await file.text();
                const clean = txt.replace(/^\uFEFF/, '').replace(/\r/g, '');
                const line = clean.split('\n').find(l => l.trim()) || '';
                let d = ',';
                if (line.includes(';')) d = ';';
                else if (line.includes('\t')) d = '\t';
                return line.split(d).map(x => x.trim()).filter(Boolean);
            } else if (name.endsWith('.xlsx') || name.endsWith('.xls')) {
                const data = await file.arrayBuffer();
                const wb = XLSX.read(data, { type: 'array' });
                const ws = wb.Sheets[wb.SheetNames[0]];
                const json = XLSX.utils.sheet_to_json(ws, { header: 1, defval: '' });
                if (json.length === 0) return [];
                return json[0].map(c => String(c).trim()).filter(Boolean);
            }
            return [];
        }

        // ── Helper: get fractional position of an overlay element ──────
        function getRelativePosition(el) {
            const wrap = document.getElementById('canvas-wrap-static');
            if (!wrap) return null;
            const rect = wrap.getBoundingClientRect();
            const elRect = el.getBoundingClientRect();
            return [
                (elRect.left + elRect.width / 2 - rect.left) / rect.width,
                (elRect.top + elRect.height / 2 - rect.top) / rect.height
            ];
        }

        let _dark = localStorage.getItem('elfak-theme') === 'dark';

        function _applyTheme(dark, animate) {
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
                if (icon) { icon.textContent = '☀️';
                    icon.style.transform = 'rotate(180deg) scale(1.2)'; }
                if (label) label.textContent = 'Light';
            } else {
                html.removeAttribute('data-theme');
                if (icon) { icon.textContent = '🌙';
                    icon.style.transform = 'rotate(0deg) scale(1)'; }
                if (label) label.textContent = 'Dark';
            }
            localStorage.setItem('elfak-theme', dark ? 'dark' : 'light');
            try {
                if (typeof leafMap !== 'undefined' && leafMap) {
                    leafMap.eachLayer(l => {
                        if (l && l._url) l.setOpacity(dark ? 0.80 : 1.0);
                    });
                }
            } catch (_e) {}
        }

        function toggleTheme() {
            const btn = document.getElementById('theme-btn');
            const r = document.createElement('span');
            r.className = 'theme-ripple';
            r.style.cssText = 'width:80px;height:80px;left:50%;top:50%;margin:-40px 0 0 -40px';
            btn.appendChild(r);
            setTimeout(() => r.remove(), 500);
            _applyTheme(!_dark, true);
        }

        function _addNavRipple(el, e) {
            const rect = el.getBoundingClientRect();
            el.style.setProperty('--rx', ((e.clientX - rect.left) / rect.width * 100) + '%');
            el.style.setProperty('--ry', ((e.clientY - rect.top) / rect.height * 100) + '%');
        }
        document.addEventListener('click', e => {
            const ni = e.target.closest('.nav-item');
            if (ni) _addNavRipple(ni, e);
        });

        let _lastPct = 0;

        function _animatePct(target) {
            const el = document.getElementById('prog-pct');
            if (!el) return;
            const start = _lastPct;
            const delta = target - start;
            const dur = Math.min(600, Math.abs(delta) * 8);
            const t0 = performance.now();

            function step(now) {
                const p = Math.min(1, (now - t0) / (dur || 1));
                const cur = Math.round(start + delta * p);
                el.textContent = cur + '%';
                if (p < 1) requestAnimationFrame(step);
                else _lastPct = target;
            }
            requestAnimationFrame(step);
        }

        let activeModule = 'A',
            cMode = 'A',
            dMode = 'A',
            eMode = 'A',
            fMode = 'A';
        let currentRunId = null,
            leafMap = null,
            leafLayers = [],
            editHistory = [];
        let activeTool = null,
            selectedLayer = null,
            currentGeoJSON = null;

        const ALIAS = {
            X: new Set(['x', 'xcoord', 'xcoordinate', 'xcord', 'east', 'easting', 'eastings', 'lon', 'long', 'longitude', 'lng',
                'pointx', 'coordx', 'utme', 'utmx'
            ]),
            Y: new Set(['y', 'ycoord', 'ycoordinate', 'ycord', 'north', 'northing', 'northings', 'lat', 'latitude', 'pointy',
                'coordy', 'utmn', 'utmy'
            ]),
            Order: new Set(['order', 'id', 'sn', 'sno', 'serial', 'serialno', 'seq', 'sequence', 'index', 'rowid', 'fid',
                'no', 'num', 'number', 'plotid', 'plotno', 'pointid', 'pointno', 'pid'
            ]),
            Forest: new Set(['forest', 'forestname', 'forestid', 'forestno', 'fname', 'forestblock', 'block']),
            Compartment: new Set(['compartment', 'comp', 'compartmentno', 'compartmentid', 'compno', 'compid', 'comp_id',
                'comp_no', 'section', 'sectionno'
            ])
        };
        const norm = s => (s || '').toString().toLowerCase().replace(/[^a-z0-9]/g, '');

        function detectCols(cols) {
            const m = { X: null, Y: null, Order: null, Forest: null, Compartment: null };
            cols.forEach(c => { const n = norm(c);
                Object.entries(ALIAS).forEach(([k, a]) => { if (!m[k] && a.has(n)) m[k] = c; }); });
            return m;
        }

        function fillSelects(card, cols) {
            const map = detectCols(cols);
            document.querySelectorAll(`select[data-card="${card}"]`).forEach(sel => {
                const key = sel.dataset.key;
                sel.innerHTML = '';
                if (key === 'Order' || key === 'Compartment') { const o = document.createElement('option');
                    o.value = '';
                    o.textContent = '— none —';
                    sel.appendChild(o); }
                cols.forEach(c => { const o = document.createElement('option');
                    o.value = c;
                    o.textContent = c;
                    sel.appendChild(o); });
                if (map[key]) sel.value = map[key];
                else if (key !== 'Order' && key !== 'Compartment' && cols.length > 0) sel.value = cols[0];
            });
        }

        function parseDbfCols(buf) {
            try {
                const v = new DataView(buf);
                const nc = Math.floor((v.getUint16(8, true) - 32) / 32);
                const cols = [];
                for (let i = 0; i < nc; i++) {
                    let n = '';
                    for (let j = 0; j < 11; j++) {
                        const b = v.getUint8(32 + i * 32 + j);
                        if (b === 0) break;
                        n += String.fromCharCode(b);
                    }
                    if (n) cols.push(n.trim());
                }
                return cols;
            } catch { return []; }
        }

        async function handleZipShps(file, selEl, colSelEl) {
            const zip = await JSZip.loadAsync(await file.arrayBuffer());
            const shps = Object.keys(zip.files).filter(p => p.toLowerCase().endsWith('.shp'));
            selEl.innerHTML = shps.length ? shps.map(f => `<option>${f}</option>`).join('') : '<option>No SHP found</option>';
            if (colSelEl) {
                const dbfs = Object.keys(zip.files).filter(p => p.toLowerCase().endsWith('.dbf'));
                if (dbfs.length) {
                    try {
                        const data = await zip.files[dbfs[0]].async('arraybuffer');
                        const cols = parseDbfCols(data);
                        if (cols.length) {
                            colSelEl.innerHTML = '<option value="">— auto detect —</option>' + cols.map(c =>
                                `<option>${c}</option>`).join('');
                            const m = detectCols(cols);
                            if (m.Forest) colSelEl.value = m.Forest;
                        }
                    } catch {}
                }
            }
        }

        document.querySelectorAll('.fi').forEach(inp => {
            inp.addEventListener('change', async function() {
                const file = this.files[0];
                if (!file) return;
                const card = this.dataset.card;
                const dzText = document.querySelector(`#dz-${card} .dz-text`);
                if (dzText) dzText.textContent = '📄 ' + file.name;
                if (card === 'C') {
                    const isZip = file.name.toLowerCase().endsWith('.zip');
                    document.getElementById('c-coord-fields').classList.toggle('hidden', isZip);
                    document.getElementById('c-zip-notice').classList.toggle('hidden', !isZip);
                    if (isZip) { await handleZipShps(file, document.getElementById('c-shp-sel'), null); return; }
                }
                if (card === 'E') {
                    const isZip = file.name.toLowerCase().endsWith('.zip');
                    document.getElementById('e-csv-fields').classList.toggle('hidden', isZip);
                    document.getElementById('e-zip-notice').classList.toggle('hidden', !isZip);
                    if (isZip) { await handleZipShps(file, document.getElementById('e-shp-sel'), document
                            .getElementById('e-zip-fcol')); return; }
                }
                let cols = ['X', 'Y', 'Order', 'Forest', 'Compartment'];
                const detected = await parseFileColumns(file);
                if (detected && detected.length > 0) cols = detected;
                fillSelects(card, cols);
            });
        });

        document.getElementById('fi-F-bnd').addEventListener('change', async function() {
            const file = this.files[0];
            if (!file) return;
            document.getElementById('f-bnd-lbl').textContent = '📄 ' + file.name;
            const isZip = file.name.toLowerCase().endsWith('.zip');
            document.getElementById('f-csv-fields').classList.toggle('hidden', isZip);
            document.getElementById('f-zip-notice').classList.toggle('hidden', !isZip);
            if (isZip) { await handleZipShps(file, document.getElementById('f-shp-sel'), null); return; }
            const cols = await parseFileColumns(file);
            if (cols && cols.length) {
                const m = detectCols(cols);
                ['f-xc', 'f-yc', 'f-oc'].forEach(id => {
                    const sel = document.getElementById(id);
                    const k = { 'f-xc': 'X', 'f-yc': 'Y', 'f-oc': 'Order' } [id];
                    sel.innerHTML = (k === 'Order' ? '<option value="">— none —</option>' : '') + cols.map(c =>
                        `<option>${c}</option>`).join('');
                    if (m[k]) sel.value = m[k];
                });
                const cs = document.getElementById('f-csv-comp');
                cs.innerHTML = '<option value="">— auto detect —</option>' + cols.map(c =>
                    `<option>${c}</option>`).join('');
                if (m.Forest) cs.value = m.Forest;
                else if (m.Compartment) cs.value = m.Compartment;
            }
        });
        document.getElementById('fi-F-dem').addEventListener('change', function() {
            const f = this.files[0];
            if (f) document.getElementById('f-dem-lbl').textContent = '🏔 ' + f.name;
        });

        // Group H file handlers
        document.querySelectorAll('#card-H .fi').forEach(inp => {
            inp.addEventListener('change', function() {
                const file = this.files[0];
                if (!file) return;
                const dz = this.closest('.dz');
                if (dz) {
                    const text = dz.querySelector('.dz-text');
                    if (text) text.textContent = '📄 ' + file.name;
                }
            });
        });

        document.getElementById('fi-G').addEventListener('change', async function() {
            const f = this.files[0];
            if (!f) return;
            document.querySelector('#dz-G .dz-text').textContent = '📄 ' + f.name;
            if (f.name.toLowerCase().endsWith('.zip')) {
                const sel = document.getElementById('g-shp-sel-wrap');
                const selEl = document.getElementById('g-shp-sel');
                if (sel) sel.style.display = 'none';
                const fd2 = new FormData();
                fd2.append('file', f);
                try {
                    const d = await fetchJSON(`${BASE}/zip_inspect`, { method: 'POST', body: fd2 });
                    if (d.shp_files && d.shp_files.length > 0 && sel && selEl) {
                        selEl.innerHTML = d.shp_files.map(s => `<option value="${s}">${s.split('/').pop()}</option>`)
                            .join('');
                        sel.style.display = 'block';
                        sel.style.animation = 'staggerIn .3s var(--ease) both';
                    } else if (sel) {
                        sel.style.display = 'none';
                    }
                } catch (e) { console.warn('zip_inspect failed:', e);
                    sel.style.display = 'none'; }
            } else {
                const sel = document.getElementById('g-shp-sel-wrap');
                if (sel) sel.style.display = 'none';
            }
        });

        document.querySelectorAll('.dz').forEach(dz => {
            const inp = dz.querySelector('input[type=file]');
            if (!inp) return;
            ['dragenter', 'dragover'].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault();
                dz.classList.add('over'); }));
            ['dragleave', 'drop'].forEach(e => dz.addEventListener(e, ev => { ev.preventDefault();
                dz.classList.remove('over'); }));
            dz.addEventListener('drop', ev => { if (ev.dataTransfer?.files?.length) { inp.files = ev.dataTransfer
                        .files;
                    inp.dispatchEvent(new Event('change', { bubbles: true })); } });
        });

        const MOD_LABELS = {
            A: 'A · Boundary',
            B: 'B · Segmented',
            C: 'C · Sample Plot',
            D: 'D · Multi-Forest',
            E: 'E · Subdivider',
            F: 'F · Slope Analysis',
            G: 'G · Survey Points',
            H: 'H · Sample Point Based'
        };

        function switchTab(t) {
            activeModule = t;
            const old = document.querySelector('.card.active');
            if (old && old.id !== 'card-' + t) {
                old.style.opacity = '0';
                old.style.transform = 'translateY(-5px)';
                setTimeout(() => { old.style.opacity = '';
                    old.style.transform = '';
                    old.classList.remove('active'); }, 150);
            }
            setTimeout(() => {
                document.querySelectorAll('.card').forEach(c => c.classList.remove('active'));
                const card = document.getElementById('card-' + t);
                if (card) card.classList.add('active');
            }, old && old.id !== 'card-' + t ? 100 : 0);
            document.querySelectorAll('.mtab').forEach(m => m.classList.toggle('active', m.id === 'mt-' + t));
            document.querySelectorAll('.nav-item').forEach(n => n.classList.toggle('active', n.getAttribute(
                'onclick') === `switchTab('${t}')`));
            const hm = document.getElementById('hdr-mod');
            if (hm) {
                hm.style.opacity = '0';
                hm.style.transform = 'translateY(-4px)';
                setTimeout(() => { hm.textContent = MOD_LABELS[t] || t;
                    hm.style.opacity = '1';
                    hm.style.transform = ''; }, 180);
            }
            document.getElementById('comp-module').value = t;
            const rb = document.getElementById('run-btn');
            if (rb) rb.textContent = t === 'G' ? '📌 Generate Points' : '▶ Run Pipeline';
        }

        function setCMode(m) { cMode = m;
            ['A', 'B'].forEach(x => document.getElementById('ct-' + x).classList.toggle('on', x === m));
            document.getElementById('c-forest-fg').classList.toggle('hidden', m !== 'B'); }

        function setDMode(m) { dMode = m;
            ['A', 'B'].forEach(x => document.getElementById('dt-' + x).classList.toggle('on', x === m));
            document.getElementById('d-comp-fg').classList.toggle('hidden', m !== 'B'); }

        function setEMode(m) { eMode = m;
            ['A', 'B'].forEach(x => document.getElementById('et-' + x).classList.toggle('on', x === m));
            document.getElementById('e-fname-fg').classList.toggle('hidden', m === 'B');
            document.getElementById('e-fcol-fg').classList.toggle('hidden', m !== 'B'); }

        function setFMode(m) {
            fMode = m;
            ['A', 'B', 'E'].forEach(x => document.getElementById('ft-' + x)?.classList.toggle('on', x === m));
            document.getElementById('f-zip-comp-fg').classList.toggle('hidden', m === 'A');
            document.getElementById('f-csv-comp-fg').classList.toggle('hidden', m === 'A');
            const hints = { A: 'Single boundary → one slope table for the whole forest',
                B: 'Groups by Forest column → slope table per forest',
                E: 'Groups by Compartment → slope table per compartment' };
            document.getElementById('f-hint').textContent = hints[m] || hints.A;
        }

        function setProgress(lbl, msg, pct, isErr) {
            const pl = document.getElementById('prog-lbl'),
                pm = document.getElementById('prog-msg');
            const pf = document.getElementById('prog-fill');
            const runBtn = document.getElementById('run-btn');
            pl.textContent = lbl;
            pm.textContent = msg;
            pf.style.width = pct + '%';
            _animatePct(pct);
            pl.className = 'prog-label' + (isErr ? ' error' : pct > 0 && pct < 100 ? ' running' : '');
            pf.className = 'prog-bar-fill' + (isErr ? ' error' : pct > 0 && pct < 100 ? ' running' : '');
            if (runBtn) {
                runBtn.classList.toggle('running', pct > 0 && pct < 100 && !isErr);
            }
        }

        // ── Time-based progress (fills the SSE blind window) ──
        // The pipeline POST blocks until the run finishes, so the SSE stream
        // can only replay buffered events *after* completion (stuck-at-5% then
        // jump-to-100%). While awaiting the response, ease the bar 5% → 75%
        // cap using per-module learned durations, then hold until the server
        // confirms — only then show 100%.
        const ProgAnim = (() => {
            const CAP = 75, MIN = 5;
            const DEFAULTS = {A:25000,B:25000,C:40000,D:45000,E:90000,F:150000,G:60000,H:240000};
            let timer = null, t0 = 0, est = 45000, msg = '';
            const key = m => `elfak-est-${m}`;
            function getEst(m) {
                try {
                    const h = JSON.parse(localStorage.getItem(key(m)) || '[]');
                    if (h.length) return Math.max(8000, h.reduce((a,b)=>a+b,0)/h.length);
                } catch (_) {}
                return DEFAULTS[m] || 45000;
            }
            function start(m, startMsg) {
                stop();
                msg = startMsg || 'Processing…';
                t0 = Date.now(); est = getEst(m);
                timer = setInterval(() => {
                    const el = Date.now() - t0;
                    const pct = MIN + (CAP - MIN) * (1 - Math.exp(-el / (est * 0.55)));
                    const eta = el < est ? ` · ~${Math.max(1, Math.round((est - el) / 1000))}s left`
                                         : ' · finishing…';
                    setProgress('Processing…', msg + eta, Math.round(Math.min(pct, CAP)), false);
                }, 500);
            }
            function stop() { if (timer) { clearInterval(timer); timer = null; } }
            function finish(m) {
                const dur = Date.now() - (t0 || Date.now());
                stop();
                try {
                    const h = JSON.parse(localStorage.getItem(key(m)) || '[]');
                    if (dur > 1000) { h.push(dur); while (h.length > 5) h.shift(); }
                    localStorage.setItem(key(m), JSON.stringify(h));
                } catch (_) {}
            }
            return { start, stop, finish };
        })();

        // ── Preview image failure → visible message + retry (never silent) ──
        function previewImgError(img, runId, file) {
            img.style.display = 'none';
            const em = document.getElementById('empty-msg');
            if (em) {
                em.style.display = 'block';
                em.innerHTML = `⚠️ Preview image failed to load (${file}). ` +
                    `The run files may still be in <a href="${BASE}/download/${runId}">the ZIP download</a>. ` +
                    `<button id="prev-retry" class="btn-sm">↻ Retry preview</button>`;
                const rb = document.getElementById('prev-retry');
                if (rb) rb.onclick = () => {
                    em.style.display = 'none';
                    img.style.display = 'block';
                    img.src = `${BASE}/outputs/${runId}/${file}?t=${Date.now()}`;
                };
            }
            setProgress('Warning', `Map files ready, but ${file} could not be displayed.`, 100, false);
        }

        // ── SSE with ETA ──────────────────────────────────────────────────
        function startSSE(runId) {
            const startTime = Date.now();
            let estimatedRemaining = null;

            const es = new EventSource(`${BASE}/progress/${runId}`);

            es.onmessage = e => {
                try {
                    const d = JSON.parse(e.data);
                    const pct = d.pct;
                    const msg = d.msg;

                    if (pct !== undefined && pct !== null) {
                        const now = Date.now();
                        const elapsed = (now - startTime) / 1000;

                        if (pct > 0 && pct < 100) {
                            const rate = pct / elapsed;
                            const remaining = (100 - pct) / rate;
                            estimatedRemaining = Math.round(remaining);
                        } else if (pct >= 100) {
                            estimatedRemaining = 0;
                        }

                        const lbl = pct >= 100 ? 'Complete ✓' : 'Processing…';
                        const pctDisplay = Math.round(pct);
                        const timeMsg = (estimatedRemaining !== null && estimatedRemaining > 0) ?
                            ` ~${estimatedRemaining}s left` :
                            (pct >= 100 ? '' : ' …');
                        setProgress(lbl, msg + timeMsg, pctDisplay, false);
                    }

                    if (pct >= 100) {
                        es.close();
                        setTimeout(() => { setProgress('Ready', '', 0); }, 8000);
                    }

                } catch (err) {
                    console.warn('SSE parse error:', err);
                }
            };

            es.onerror = () => {
                console.warn('SSE connection closed or errored');
            };

            return es;
        }

        function buildMapping(card) {
            const m = {};
            document.querySelectorAll(`select[data-card="${card}"][data-key],input[data-card="${card}"][data-key]`)
                .forEach(el => {
                    let hidden = false;
                    let p = el.parentElement;
                    while (p && p.id !== 'card-' + card) { if (p.classList.contains('hidden')) { hidden = true;
                            break; } p = p.parentElement; }
                    if (!hidden && el.value) m[el.dataset.key] = el.value;
                });
            if (!m.X) m.X = 'X';
            if (!m.Y) m.Y = 'Y';
            if (card === 'C' && !document.getElementById('c-zip-notice').classList.contains('hidden'))
                m.target_shp = document.getElementById('c-shp-sel').value;
            if (card === 'E' && !document.getElementById('e-zip-notice').classList.contains('hidden'))
                m.target_shp = document.getElementById('e-shp-sel').value;
            return m;
        }

        async function runPipeline() {
            if (activeModule === 'G') { await runG(); return; }
            const title = document.getElementById('g-title').value.trim();
            const legendTitle = document.getElementById('g-legend').value.trim() || 'Legend';
            const labelCol = document.getElementById('g-label').value.trim();
            const zone = document.getElementById('zone').value;
            if (activeModule === 'F') { await runF(title, legendTitle, labelCol, zone); return; }
            const card = document.getElementById('card-' + activeModule);
            const file = card.querySelector('input[type=file]')?.files?.[0];
            if (!file) { alert('Please upload a file first.'); return; }

            const noZipModules = ['A', 'B', 'D'];
            if (noZipModules.includes(activeModule) && file.name.toLowerCase().endsWith('.zip')) {
                alert('ZIP files are not supported for this module. Please upload a CSV or Excel file.');
                return;
            }

            document.getElementById('run-btn').disabled = true;
            setProgress('Processing…', 'Starting pipeline…', 5);
            ProgAnim.start(activeModule, 'Starting pipeline…');
            const fd = new FormData();
            fd.append('file', file);
            fd.append('module', activeModule);
            fd.append('mode', activeModule === 'C' ? cMode : activeModule);
            fd.append('zone', zone);
            fd.append('title', title);
            fd.append('legend_title', legendTitle);
            fd.append('label_col', labelCol);
            if (activeModule === 'D') fd.append('d_mode', dMode);
            if (activeModule === 'E') {
                fd.append('e_mode', eMode);
                fd.append('n_compartments', document.getElementById('e-n').value);
                fd.append('area_tol_ha', document.getElementById('e-tol').value);
                fd.append('e_method', document.getElementById('e-method').value);
                const fzc = document.getElementById('e-zip-fcol')?.value;
                if (fzc) fd.append('forest_col_name', fzc);
            }
            if (activeModule === 'C') {
                fd.append('w', document.getElementById('cw').value || 50);
                fd.append('h', document.getElementById('ch').value || 50);
                fd.append('rows', document.getElementById('cr').value || 10);
                fd.append('cols', document.getElementById('cc2').value || 10);
            }
            const mapping = buildMapping(activeModule);
            const forestInp = card.querySelector('input[type=text][data-key=forest]');
            if (forestInp?.value?.trim()) fd.append('forest', forestInp.value.trim());
            fd.append('mapping', JSON.stringify(mapping));
            await sendRequest(fd);
        }

        async function runF(title, legendTitle, labelCol, zone) {
            const bFile = document.getElementById('fi-F-bnd')?.files?.[0];
            const dFile = document.getElementById('fi-F-dem')?.files?.[0];
            if (!bFile) { alert('Upload a boundary file.'); return; }
            if (!dFile) { alert('Upload a DEM GeoTIFF file.'); return; }
            document.getElementById('run-btn').disabled = true;
            setProgress('Processing…', 'Starting Group F…', 5);
            ProgAnim.start('F', 'Starting Group F…');
            const fd = new FormData();
            fd.append('file', bFile);

            if (_demCacheKey) {
                fd.append('dem_cache_key', _demCacheKey);
            } else {
                const manualDem = document.getElementById('fi-F-dem')?.files?.[0];
                if (manualDem) {
                    fd.append('dem_file', manualDem);
                } else {
                    const demCatSel = document.getElementById('f-dem-catalog');
                    const demCatPath = (demCatSel && demCatSel.value) ? demCatSel.value.trim() : '';
                    if (demCatPath) {
                        fd.append('dem_catalog_path', demCatPath);
                    }
                }
            }
            fd.append('module', 'F');
            fd.append('f_mode', fMode);
            fd.append('zone', zone);
            fd.append('title', title);
            fd.append('legend_title', legendTitle);
            fd.append('label_col', labelCol);
            fd.append('f_forest', document.getElementById('f-fname')?.value || 'FOREST');
            const fa = document.getElementById('f-fa')?.value?.trim();
            if (fa && !isNaN(+fa)) fd.append('field_area_ha', fa);
            const isFZip = !document.getElementById('f-zip-notice').classList.contains('hidden');
            const mapping = {};
            if (!isFZip) {
                const xv = document.getElementById('f-xc')?.value;
                if (xv) mapping.X = xv;
                const yv = document.getElementById('f-yc')?.value;
                if (yv) mapping.Y = yv;
                const ov = document.getElementById('f-oc')?.value;
                if (ov) mapping.Order = ov;
                const cv = document.getElementById('f-csv-comp')?.value;
                if (cv) fd.append('comp_col', cv);
            } else {
                const sv = document.getElementById('f-shp-sel')?.value;
                if (sv) mapping.target_shp = sv;
                const cv = document.getElementById('f-zip-comp-col')?.value;
                if (cv) fd.append('comp_col', cv);
            }
            fd.append('mapping', JSON.stringify(mapping));
            await sendRequest(fd);
        }

        async function runG() {
            const file = document.getElementById('fi-G').files?.[0];
            if (!file) { alert('Please upload a compartment shapefile or ZIP first.'); return; }
            const spacing = parseFloat(document.getElementById('g-spacing').value);
            if (!spacing || spacing <= 0) { alert('Please enter a valid point spacing (metres).'); return; }
            const compCol = document.getElementById('g-comp-col').value.trim();
            const mapTitle = document.getElementById('g-map-title').value.trim() ||
                document.getElementById('g-title').value.trim() ||
                'Forest Survey Points';
            const zone = document.getElementById('zone').value;
            document.getElementById('run-btn').disabled = true;
            setProgress('Processing…', 'Starting Group G…', 5);
            ProgAnim.start('G', 'Starting Group G…');
            document.getElementById('g-result-box').classList.remove('visible');

            const fd = new FormData();
            fd.append('file', file);
            fd.append('zone', zone);
            fd.append('spacing', spacing);
            fd.append('title', mapTitle);
            if (compCol) fd.append('comp_col', compCol);
            const selEl = document.getElementById('g-shp-sel');
            const selWrap = document.getElementById('g-shp-sel-wrap');
            if (selEl && selWrap && selWrap.style.display !== 'none' && selEl.value) {
                fd.append('target_shp', selEl.value);
            }

            try {
                const data = await fetchJSON(`${BASE}/run_g`, { method: 'POST', body: fd });
                ProgAnim.finish('G');
                if (data.error) throw new Error(data.error);
                currentRunId = data.run_id;
                startSSE(data.run_id);

                const img = document.getElementById('out-img');
                img.onload = () => { img.style.display = 'block';
                    document.getElementById('empty-msg').style.display = 'none'; };
                img.onerror = () => previewImgError(img, data.run_id, 'output.png');
                img.src = `${BASE}/outputs/${data.run_id}/output.png?t=${Date.now()}`;

                const dl = document.getElementById('dl-btn');
                dl.href = `${BASE}${data.download}`;
                dl.style.display = 'inline-block';
                document.getElementById('dl-btn2').href = dl.href;

                if (data.summary) {
                    const s = data.summary;
                    document.getElementById('g-result-box').innerHTML =
                        `<strong>✅ Generation Complete</strong><br>` +
                        `Total Points: <strong>${s.total}</strong> &nbsp;|&nbsp; Compartments: <strong>${s.compartments}</strong><br>` +
                        `Vertex: <strong>${s.vertex}</strong> &nbsp; Boundary: <strong>${s.boundary}</strong> &nbsp; Divider: <strong>${s.divider}</strong><br>` +
                        `Area: <strong>${s.area_ha} ha</strong> &nbsp;|&nbsp; Spacing: <strong>${s.spacing}m</strong> &nbsp;|&nbsp; CRS: <strong>EPSG:${s.epsg}</strong><br>` +
                        `Comp Col: <strong>${s.comp_col}</strong>`;
                    document.getElementById('g-result-box').classList.add('visible');
                }

                document.getElementById('run-meta').textContent = `Run: ${data.run_id.slice(0,8)}… | Module: G`;
                loadOSM(data.run_id, data.kmz_url || null);

                setTimeout(async () => {
                    try {
                        const hd = await fetchJSON(`${BASE}/history`);
                        if (hd.runs) renderHistory(hd.runs);
                    } catch {}
                }, 900);
            } catch (e) {
                ProgAnim.stop();
                const msg2 = e.message || 'Group G failed';
                const isRL2 = msg2.includes('Too many') || msg2.includes('retry_after');
                setProgress('Error', msg2, 0, true);
                if (!isRL2) alert('Error: ' + msg2);
            } finally {
                document.getElementById('run-btn').disabled = false;
            }
        }

        async function sendRequest(fd) {
            try {
                const data = await fetchJSON(`${BASE}/upload`, { method: 'POST', body: fd });
                ProgAnim.finish(fd.get('module') || 'A');
                if (data.error) throw new Error(data.error);
                currentRunId = data.run_id;
                startSSE(data.run_id);
                const img = document.getElementById('out-img');
                img.style.opacity = '0';
                img.style.transform = 'scale(.96)';
                img.onload = () => {
                    img.style.display = 'block';
                    document.getElementById('empty-msg').style.display = 'none';
                    img.style.transition = 'opacity .45s var(--ease),transform .45s var(--ease)';
                    requestAnimationFrame(() => { img.style.opacity = '1';
                        img.style.transform = 'scale(1)'; });
                    setTimeout(() => img.style.transition = '', 500);

                    const vfb = document.getElementById('view-full-btn');
                    if (vfb) {
                        vfb.style.display = 'block';
                        vfb.style.visibility = 'visible';
                        vfb.style.opacity = '1';
                    }
                    showOverlayForRun();
                };
                img.onerror = () => previewImgError(img, data.run_id, 'output.png');
                img.src = `${BASE}/outputs/${data.run_id}/output.png?t=${Date.now()}`;
                const dl = document.getElementById('dl-btn');
                dl.href = data.download.startsWith('http') ? data.download : `${BASE}${data.download}`;
                dl.style.display = 'inline-block';
                document.getElementById('dl-btn2').href = dl.href;
                loadOSM(data.run_id, data.kmz_url || null);
                document.getElementById('run-meta').textContent =
                    `Run: ${data.run_id.slice(0,8)}… | Module: ${fd.get('module')||'?'}`;
                document.getElementById('comp-title').value = fd.get('title') || '';
                document.getElementById('comp-legend').value = fd.get('legend_title') || 'Legend';
                document.getElementById('comp-label').value = fd.get('label_col') || '';
                setTimeout(async () => {
                    try {
                        const hd = await fetchJSON(`${BASE}/history`);
                        if (hd.runs) renderHistory(hd.runs);
                    } catch {}
                }, 800);
            } catch (e) {
                ProgAnim.stop();
                const msg = e.message || 'Pipeline failed';
                const isRL = msg.includes('Too many') || msg.includes('retry_after');
                setProgress('Error', msg, 0, true);
                if (isRL) {
                    const sec = (msg.match(/(\d+)s/) || [])[1] || 5;
                    let cd = parseInt(sec);
                    const rb = document.getElementById('run-btn');
                    rb.disabled = true;
                    rb.textContent = `⏳ Wait ${cd}s…`;
                    const iv = setInterval(() => {
                        cd--;
                        if (cd <= 0) {
                            clearInterval(iv);
                            rb.disabled = false;
                            rb.textContent = activeModule === 'G' ? '📌 Generate Points' : '▶ Run Pipeline';
                            setProgress('Ready', 'You can try again now.', 0);
                        } else {
                            rb.textContent = `⏳ Wait ${cd}s…`;
                        }
                    }, 1000);
                    return;
                } else {
                    alert('Error: ' + msg);
                }
            } finally {
                document.getElementById('run-btn').disabled = false;
            }
        }

        // ── GROUP H ──────────────────────────────────────────────────────

        async function runGroupH() {
            const btn = document.getElementById('run-h-btn');
            btn.disabled = true;
            btn.textContent = '⏳ Processing...';

            const formData = new FormData();
            const files = document.querySelectorAll('#card-H input[type="file"]');
            let missing = false;
            files.forEach(inp => {
                const key = inp.dataset.key;
                if (inp.files.length === 0) {
                    if (key !== 'survey_points') {
                        missing = true;
                        inp.style.borderColor = 'red';
                    }
                } else {
                    formData.append(key, inp.files[0]);
                }
            });
            if (missing) {
                alert('Please upload all required files.');
                btn.disabled = false;
                btn.textContent = '🚀 Generate Maps';
                return;
            }
            formData.append('crs', document.getElementById('h-crs').value);

            setProgress('Group H', 'Uploading and processing...', 10);
            ProgAnim.start('H', 'Uploading and processing...');
            try {
                const response = await fetch('/run_h', { method: 'POST', body: formData });
                const data = await response.json();
                ProgAnim.finish('H');
                if (data.error) throw new Error(data.error);
                currentRunId = data.run_id;
                startSSE(data.run_id);
                document.getElementById('dl-btn').href = `/download/${data.run_id}`;
                document.getElementById('dl-btn').style.display = 'inline-block';

                // Show gallery
                const gallery = document.getElementById('h-gallery');
                gallery.style.display = 'block';
                const base = `/outputs/${data.run_id}/`;
                const mapNames = ['Slope_Map', 'Satellite_Map', 'SubCompartment_Map', 'SamplePlot_Map', 'BoundarySurveyPoint_Map', 'SurveyPoint_Map'];
                const imgIds = ['h-preview-slope', 'h-preview-satellite', 'h-preview-sub', 'h-preview-sample', 'h-preview-boundary', 'h-preview-survey'];
                mapNames.forEach((name, i) => {
                    const img = document.getElementById(imgIds[i]);
                    if (img) {
                        img.onerror = () => { img.style.opacity = '0.25'; };
                        img.onload = () => { img.style.opacity = '1'; };
                        img.src = base + name + '.png?t=' + Date.now();
                    }
                });

                // Set main preview to Slope Map
                const hMain = document.getElementById('out-img');
                hMain.onload = () => {
                    hMain.style.display = 'block';
                    document.getElementById('empty-msg').style.display = 'none';
                };
                hMain.onerror = () => previewImgError(hMain, data.run_id, 'Slope_Map.png');
                hMain.src = base + 'Slope_Map.png?t=' + Date.now();
                hMain.style.display = 'block';
                document.getElementById('empty-msg').style.display = 'none';
                switchPView('static');
                document.getElementById('run-meta').textContent = `Group H run: ${data.run_id.slice(0,8)}…`;

                // Update composer module to H
                document.getElementById('comp-module').value = 'H';

                // Show success
                setProgress('Complete ✓', 'All six maps generated.', 100);
            } catch (e) {
                ProgAnim.stop();
                alert('Error: ' + e.message);
                setProgress('Error', e.message, 0, true);
            } finally {
                btn.disabled = false;
                btn.textContent = '🚀 Generate Maps';
            }
        }

        function downloadGroupH(format) {
            if (!currentRunId) return alert('No run available.');
            window.location.href = `/download/${currentRunId}`;
        }

        // ── View switching ──────────────────────────────────────────────

        function switchPView(v) {
            ['static', 'osm', 'compose'].forEach(t => {
                const el = document.getElementById('pv-' + t);
                if (el) {
                    if (t === v) {
                        el.style.display = 'flex';
                        el.style.opacity = '0';
                        el.style.transform = 'translateX(8px)';
                        requestAnimationFrame(() => {
                            el.style.transition = 'opacity .28s var(--ease),transform .28s var(--ease)';
                            el.style.opacity = '1';
                            el.style.transform = 'none';
                            setTimeout(() => el.style.transition = '', 300);
                        });
                    } else {
                        el.style.display = 'none';
                    }
                }
                const tab = document.getElementById('pt-' + t);
                if (tab) tab.classList.toggle('on', t === v);
            });
            const pp = document.querySelector('.preview-panel');
            if (pp) pp.classList.toggle('composing', v === 'compose');
            if (v === 'compose' && currentRunId
                && !document.querySelector('#comp-legend-rows .comp-row-inp')) {
                try { refreshComposerTexts(); } catch (_) {}
            }
            if (v === 'osm') {
                document.getElementById('osm-map').style.display = 'block';
                if (leafMap) setTimeout(() => leafMap.invalidateSize(), 160);
            }
            if (v === 'static') {
                if (currentRunId) {
                    const layer = document.getElementById('overlay-layer');
                    const img = document.getElementById('out-img');
                    if (img && img.style.display !== 'none') {
                        layer.classList.add('visible');
                    }
                }
            }
        }

        // ── OSM Map loading ─────────────────────────────────────────────

        let _osmLegendControl = null;

        function _buildOsmLegend(features, layerType) {
            if (_osmLegendControl) { try { leafMap.removeControl(_osmLegendControl); } catch {} _osmLegendControl = null; }
            const items = [];
            if (layerType === 'point') {
                const types = new Set(features.map(f => f.properties?.Point_Type).filter(Boolean));
                const typeColors = { Divider: '#9C27B0', Vertex: '#2196F3', Boundary: '#FF9800' };
                types.forEach(t => items.push({ color: typeColors[t] || '#ef4444', label: t + ' Point',
                    circle: true }));
                if (!types.size) items.push({ color: '#ef4444', label: 'Survey Points', circle: true });
            } else {
                const hasCls = features.some(f => f.properties?.Class);
                if (hasCls) {
                    [{ cls: 1, c: '#4CAF50', l: '0–19° Gentle' }, { cls: 2, c: '#FFC107', l: '19–31° Moderate' },
                    { cls: 3, c: '#FF9800', l: '31–45° Steep' }, { cls: 4, c: '#ef4444', l: '45°+ Very Steep' }
                    ].forEach(({ cls, c, l }) => { if (features.some(f => f.properties?.Class === cls)) items
                            .push({ color: c, label: l }); });
                } else {
                    items.push({ color: '#10b981', label: 'Forest Boundary', outline: true });
                }
            }
            if (!items.length) return;
            const L2 = L;
            const LegendCtrl = L2.Control.extend({
                onAdd: function() {
                    const div = L.DomUtil.create('div');
                    div.style.cssText = 'background:rgba(255,255,255,.92);border-radius:8px;padding:8px 12px;' +
                        'font-family:monospace;font-size:11px;box-shadow:0 2px 12px rgba(0,0,0,.18);' +
                        'border:1px solid rgba(0,0,0,.08);max-width:160px;backdrop-filter:blur(8px)';
                    items.forEach(item => {
                        const row = document.createElement('div');
                        row.style.cssText = 'display:flex;align-items:center;gap:7px;margin-bottom:4px';
                        const ic = document.createElement('span');
                        if (item.circle) {
                            ic.style.cssText =
                                `width:10px;height:10px;border-radius:50%;background:${item.color};flex-shrink:0;border:2px solid ${item.color}`;
                        } else if (item.outline) {
                            ic.style.cssText =
                                `width:14px;height:10px;border-radius:2px;border:2px solid ${item.color};flex-shrink:0`;
                        } else {
                            ic.style.cssText =
                                `width:14px;height:10px;border-radius:2px;background:${item.color};opacity:.75;flex-shrink:0`;
                        }
                        row.appendChild(ic);
                        const lb = document.createElement('span');
                        lb.textContent = item.label;
                        lb.style.cssText = 'color:#1a2e22;font-size:10px';
                        row.appendChild(lb);
                        div.appendChild(row);
                    });
                    return div;
                }
            });
            _osmLegendControl = new LegendCtrl({ position: 'bottomright' });
            try { _osmLegendControl.addTo(leafMap); } catch {}
        }

        function _bindPopupAndHover(feat, lyr) {
            const p = feat.properties || {};
            const lbl = p.Point_ID || p.Comp_ID || p.Forest || p.Label || p.Slope_Range || '';
            const ah = p.Area_ha ? ` — ${parseFloat(p.Area_ha).toFixed(3)} ha` : '';
            const pt = p.Point_Type ? `<br>Type: <b>${p.Point_Type}</b>` : '';
            const src2 = p.Source ? `<br>Source: ${p.Source}` : '';
            const cp = p.Compartments ? `<br>Comps: ${p.Compartments}` : '';
            const cl = p.Class ? ` [Class ${p.Class}]` : '';
            lyr.bindPopup(
                `<div style="font-family:monospace;font-size:11px"><b>${lbl}</b>${ah}${cl}${pt}${src2}${cp}</div>`
                );
            lyr.on('click', () => {
                if (selectedLayer && selectedLayer !== lyr) try { selectedLayer.setStyle({ weight: 2,
                        color: '#1a3a22' }); } catch {}
                selectedLayer = lyr;
                try { lyr.setStyle({ weight: 4, color: '#059669' }); } catch {}
            });
            lyr.on('mouseover', () => { if (lyr !== selectedLayer) try { lyr.setStyle && lyr.setStyle({ weight: 3 }); } catch {} });
            lyr.on('mouseout', () => { if (lyr !== selectedLayer) try { lyr.setStyle && lyr.setStyle({ weight: 2,
                        color: '#1a3a22' }); } catch {} });
        }

        // ── DEM Catalog ─────────────────────────────────────────────────

        let _demCatalog = [];
        let _demCacheKey = '';
        let _demActiveZone = '';

        async function _loadDemCatalog() {
            const sel = document.getElementById('f-dem-catalog');
            const info = document.getElementById('f-dem-cat-info');
            try {
                if (sel) sel.innerHTML = '<option value="" disabled>⏳ Loading catalog from GitHub…</option>';
                const d = await fetchJSON(`${BASE}/dem_catalog`);
                _demCatalog = d.files || [];
                if (info) info.textContent = `${_demCatalog.length} DEM file(s) available`;
                _filterDemList('', '');
            } catch (e) {
                console.warn('DEM catalog load failed:', e);
                if (sel) sel.innerHTML = '<option value="" disabled>⚠ Could not load catalog</option>';
                if (info) info.textContent = 'GitHub catalog unavailable — upload a .tif manually.';
            }
        }

        function _filterDemList(q, zone) {
            const sel = document.getElementById('f-dem-catalog');
            const info = document.getElementById('f-dem-cat-info');
            if (!sel) return;
            if (zone !== undefined) _demActiveZone = zone;
            const qLow = (q || document.getElementById('f-dem-search')?.value || '').toLowerCase();

            const filtered = _demCatalog.filter(f => {
                const matchQ = !qLow || f.name.toLowerCase().includes(qLow) || f.path.toLowerCase().includes(qLow);
                const matchZone = !_demActiveZone || f.zone === _demActiveZone;
                return matchQ && matchZone;
            });

            if (_demCatalog.length === 0) {
                sel.innerHTML = '<option value="" disabled>No DEM files found in GitHub repo</option>';
                if (info) info.textContent = 'Add .tif files to dem_catalog/44N/ and dem_catalog/45N/ in your GitHub repo.';
                return;
            }
            sel.innerHTML = filtered.length ?
                filtered.map(f => {
                    const zone_badge = f.zone ? `[${f.zone}] ` : '';
                    return `<option value="${f.path}" data-url="${f.url||''}" title="${f.path}">${zone_badge}${f.name} (${f.size_mb}MB)</option>`;
                }).join('') :
                '<option value="" disabled>No matches — try different search</option>';

            if (info) info.textContent = `Showing ${filtered.length} of ${_demCatalog.length} DEM file(s)`;
        }

        async function _onDemSelect(sel) {
            const path = sel.value;
            if (!path) return;
            const opt = sel.options[sel.selectedIndex];
            const url = opt?.dataset?.url || '';
            const info = document.getElementById('f-dem-cat-info');
            const dlWrap = document.getElementById('f-dem-dl-wrap');
            const dlBar = document.getElementById('f-dem-dl-bar');
            const dlLbl = document.getElementById('f-dem-dl-label');

            const f = _demCatalog.find(x => x.path === path);
            const size_mb = f?.size_mb || 0;

            if (info) info.textContent = `Selected: ${path} (${size_mb}MB) — downloading to server cache…`;

            if (dlWrap) dlWrap.style.display = 'block';
            if (dlBar) dlBar.style.width = '5%';
            if (dlLbl) dlLbl.textContent = `Downloading ${path} from GitHub… (${size_mb}MB)`;

            let prog = 5;
            const interval = setInterval(() => {
                prog = Math.min(prog + (100 / Math.max(size_mb * 2, 10)), 88);
                if (dlBar) dlBar.style.width = prog + '%';
            }, 800);

            try {
                const d = await fetchJSON(`${BASE}/dem_fetch`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ url, path })
                });
                clearInterval(interval);

                if (d.ok) {
                    _demCacheKey = d.cache_key;
                    if (dlBar) dlBar.style.width = '100%';
                    if (dlLbl) dlLbl.textContent = d.cached ?
                        `✓ Already cached: ${path} (${d.size_mb}MB) — ready` :
                        `✓ Downloaded: ${path} (${d.size_mb}MB) — ready`;
                    if (info) info.textContent = `✓ DEM ready: ${path} (${d.size_mb} MB). No manual upload needed.`;
                    const dzDem = document.getElementById('dz-F-dem');
                    if (dzDem) { dzDem.style.opacity = '0.4';
                        dzDem.style.pointerEvents = 'none'; }
                    const manualInp = document.getElementById('fi-F-dem');
                    if (manualInp) manualInp.value = '';
                    document.getElementById('f-dem-lbl').textContent = '🏔 DEM from catalog — no upload needed';
                    setTimeout(() => { if (dlWrap) dlWrap.style.display = 'none'; }, 2500);
                } else {
                    clearInterval(interval);
                    if (dlLbl) dlLbl.textContent = `❌ Download failed: ${d.error}`;
                    if (dlBar) dlBar.style.background = '#ef4444';
                    let extra = '';
                    if (d.attempted_urls && d.attempted_urls.length) {
                        extra = ` Tried ${d.attempted_urls.length} URL(s) — check the file exists at: ` +
                            d.attempted_urls[d.attempted_urls.length - 1];
                    }
                    if (info) info.textContent = `Download error: ${d.error}.${extra} Upload manually instead.`;
                    _demCacheKey = '';
                    const dzDem = document.getElementById('dz-F-dem');
                    if (dzDem) { dzDem.style.opacity = '1';
                        dzDem.style.pointerEvents = 'auto'; }
                    console.warn('DEM fetch failed. Attempted URLs:', d.attempted_urls, 'Hint:', d.hint);
                }
            } catch (e) {
                clearInterval(interval);
                if (dlLbl) dlLbl.textContent = `❌ Network error: ${e.message}`;
                _demCacheKey = '';
            }
        }

        function _ensureLeafMap() {
            if (leafMap) return;
            const el = document.getElementById('osm-map');
            el.style.display = 'block';
            leafMap = L.map('osm-map', { zoomControl: true, attributionControl: true, preferCanvas: true });
            L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
                maxZoom: 19,
                attribution: '© <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
            }).addTo(leafMap);
        }

        const COMP_COLORS_OSM = [
            '#2196F3', '#FF9800', '#4CAF50', '#9C27B0', '#F48FB1',
            '#607D8B', '#CDDC39', '#00BCD4', '#FF5722', '#795548',
            '#E91E63', '#009688', '#FFC107', '#3F51B5', '#8BC34A'
        ];

        async function loadOSM(runId, kmzInfo) {
            document.getElementById('osm-idle').style.display = 'none';
            document.getElementById('osm-map').style.display = 'block';
            document.getElementById('edit-toolbar').classList.add('visible');
            _ensureLeafMap();
            leafLayers.forEach(l => { try { leafMap.removeLayer(l); } catch {} });
            leafLayers = [];
            selectedLayer = null;

            setTimeout(async () => {
                leafMap.invalidateSize();
                try {
                    const gj = await fetchJSON(`${BASE}/geojson/${runId}`);
                    if (gj.features && gj.features.length > 0) {
                        currentGeoJSON = gj;
                        let compIdx = 0;
                        const pointFeats = gj.features.filter(f => f.geometry &&
                            (f.geometry.type === 'Point' || f.geometry.type === 'MultiPoint'));
                        const polyFeats = gj.features.filter(f => f.geometry &&
                            !['Point', 'MultiPoint'].includes(f.geometry.type));

                        if (polyFeats.length > 0) {
                            const polyGJ = { type: 'FeatureCollection', features: polyFeats };
                            const polyLayer = L.geoJSON(polyGJ, {
                                style: feat => {
                                    const p = feat.properties || {};
                                    const cls = p.Class;
                                    let fc = '#10b981';
                                    if (cls === 1) fc = '#4CAF50';
                                    else if (cls === 2) fc = '#FFC107';
                                    else if (cls === 3) fc = '#FF9800';
                                    else if (p.Comp_ID || p.Forest) fc = COMP_COLORS_OSM[compIdx++ % COMP_COLORS_OSM
                                        .length
                                    ];
                                    return { fillColor: fc, fillOpacity: 0.38, color: '#1a3a22', weight: 2 };
                                },
                                onEachFeature: (feat, lyr) => _bindPopupAndHover(feat, lyr)
                            }).addTo(leafMap);
                            leafLayers.push(polyLayer);
                            _buildOsmLegend(polyFeats, 'polygon');
                            try { leafMap.fitBounds(polyLayer.getBounds().pad(0.06)); } catch {}
                        }

                        if (pointFeats.length > 0) {
                            const ptGJ = { type: 'FeatureCollection', features: pointFeats };
                            const ptLayer = L.geoJSON(ptGJ, {
                                pointToLayer: (feat, latlng) => {
                                    const p = feat.properties || {};
                                    const col = p.Point_Type === 'Divider' ? '#9C27B0' :
                                        p.Point_Type === 'Vertex' ? '#2196F3' :
                                        p.Point_Type === 'Boundary' ? '#FF9800' : '#ef4444';
                                    return L.circleMarker(latlng, {
                                        radius: 6,
                                        color: col,
                                        fillColor: col,
                                        fillOpacity: 0.92,
                                        weight: 1.5
                                    });
                                },
                                onEachFeature: (feat, lyr) => _bindPopupAndHover(feat, lyr)
                            }).addTo(leafMap);
                            leafLayers.push(ptLayer);
                            _buildOsmLegend(pointFeats, 'point');
                        }

                        setTimeout(() => {
                            try {
                                const allBounds = leafLayers
                                    .filter(l => l.getBounds)
                                    .map(l => { try { return l.getBounds(); } catch { return null; } })
                                    .filter(Boolean);
                                if (allBounds.length > 0) {
                                    let combined = allBounds[0];
                                    allBounds.slice(1).forEach(b => { try { combined = combined.extend(b); } catch {} });
                                    leafMap.fitBounds(combined.pad(0.06));
                                }
                            } catch (e) { console.warn('fitBounds error:', e); }
                        }, 100);
                    } else if (kmzInfo && kmzInfo.lat && kmzInfo.lon) {
                        const zoom = Math.max(10, Math.min(16, Math.round(17 - Math.log2(Math.max(kmzInfo.alt / 500,
                            1)))));
                        leafMap.setView([kmzInfo.lat, kmzInfo.lon], zoom);
                        const mk = L.circleMarker([kmzInfo.lat, kmzInfo.lon], {
                            radius: 10,
                            color: '#10b981',
                            fillColor: '#34d399',
                            fillOpacity: 0.9,
                            weight: 2
                        }).bindPopup(
                            `<b>Centroid</b><br>${kmzInfo.lat.toFixed(5)}°N, ${kmzInfo.lon.toFixed(5)}°E`
                        ).addTo(leafMap);
                        leafLayers.push(mk);
                    }
                } catch (e) { console.warn('OSM GeoJSON load error:', e); }
                leafMap.invalidateSize();
            }, 280);
        }

        function setTool(t) {
            activeTool = t;
            document.querySelectorAll('.etool[id^=tool-]').forEach(el=>el.classList.remove('active-tool'));
            const el=document.getElementById('tool-'+t);
            if(el) el.classList.add('active-tool');
            if(!leafMap) return;
            if(t==='vertex'){
                const pointOnly=['C','G'].includes(activeModule);
                leafLayers.forEach(l=>{
                    try{
                        const isPoint=(()=>{
                            if(l._latlng) return true;
                            if(l.getLayers){
                                const subs=l.getLayers();
                                return subs.length>0&&subs.every(s=>s._latlng||s instanceof L.CircleMarker);
                            }
                            return false;
                        })();
                        const enable=!pointOnly||isPoint;
                        if(enable){
                            if(l.pm) l.pm.enable({allowSelfIntersection:false});
                            else if(l.getLayers) l.getLayers().forEach(s=>{try{s.pm&&s.pm.enable({allowSelfIntersection:false});}catch{}});
                        } else {
                            if(l.pm) l.pm.disable();
                            else if(l.getLayers) l.getLayers().forEach(s=>{try{s.pm&&s.pm.disable();}catch{}});
                        }
                    }catch(e){console.warn('PM:',e);}
                });
            } else {
                leafLayers.forEach(l=>{
                    try{
                        if(l.pm) l.pm.disable();
                        else if(l.getLayers) l.getLayers().forEach(s=>{try{s.pm&&s.pm.disable();}catch{}});
                    }catch{}
                });
            }
        }
        function deleteSelected() {
            if (!selectedLayer) { alert('Select a feature first (click on it).'); return; }
            if (!confirm('Delete selected feature?')) return;
            editHistory.push({ type: 'delete', layer: selectedLayer, geojson: currentGeoJSON ? JSON.parse(JSON.stringify(
                    currentGeoJSON)) : null });
            leafMap.removeLayer(selectedLayer);
            leafLayers = leafLayers.filter(l => l !== selectedLayer);
            selectedLayer = null;
        }

        function undoEdit() {
            if (!editHistory.length) { alert('Nothing to undo.'); return; }
            const last = editHistory.pop();
            if (last.type === 'delete' && last.layer) { last.layer.addTo(leafMap);
                leafLayers.push(last.layer);
                selectedLayer = last.layer; }
        }

        async function saveEdits() {
            if (!currentRunId) { alert('No active run.'); return; }
            const layers = [];
            leafLayers.forEach(l => { if (l.toGeoJSON) { try { layers.push(l.toGeoJSON()); } catch {} } });
            const fc = { type: 'FeatureCollection', features: [] };
            layers.forEach(gj => { if (gj.features) fc.features.push(...gj.features);
                else fc.features.push(gj); });
            if (!fc.features.length) { alert('No features to save.'); return; }
            setProgress('Saving edits…', 'Sending to server…', 50);
            try {
                const d = await fetchJSON(`${BASE}/save_edit/${currentRunId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ geojson: fc, title: document.getElementById('g-title').value,
                        legend_title: document.getElementById('g-legend').value })
                });
                if (d.error) throw new Error(d.error);
                setProgress('Edits Saved ✓', 'Output updated', 100);
                const img = document.getElementById('out-img');
                img.src = `${BASE}${d.png}`;
            } catch (e) { setProgress('Error', 'Save failed: ' + e.message, 0, true);
                alert('Save error: ' + e.message); }
        }

        // ── LAYOUT ENGINE ──────────────────────────────────────────────

        let _layoutEditActive = false;
        let _selectedOvItem = null;
        let _ovDragState = null;
        let _ovResizeState = null;
        let _ovRotateState = null;
        let _ovItems = [];

        function _getOvItems() {
            return document.querySelectorAll('.ov-item');
        }

        function _deselectAll() {
            _getOvItems().forEach(el => {
                el.classList.remove('ov-selected');
            });
            _selectedOvItem = null;
            document.getElementById('layer-panel')?.classList.remove('visible');
            document.getElementById('align-toolbar')?.classList.remove('visible');
        }

        function _selectOvItem(el) {
            _deselectAll();
            _selectedOvItem = el;
            if (el) {
                el.classList.add('ov-selected');
                el.style.zIndex = 50;
                _updateLayerPanel();
                document.getElementById('layer-panel')?.classList.add('visible');
                document.getElementById('align-toolbar')?.classList.add('visible');
            }
        }

        function _updateLayerPanel() {
            const panel = document.getElementById('layer-panel');
            if (!panel) return;
            const items = _getOvItems();
            if (!items.length) { panel.innerHTML = '<div style="font-size:9px;color:var(--muted);padding:4px;">No items</div>'; return; }

            let html = '<div style="font-size:8px;font-family:var(--mono);color:var(--muted);padding:2px 0 4px;border-bottom:1px solid var(--panel-border);margin-bottom:4px;">LAYERS</div>';
            const sorted = Array.from(items).sort((a, b) => (parseInt(a.style.zIndex) || 6) - (parseInt(b.style.zIndex) || 6));
            sorted.forEach(el => {
                const type = el.dataset.ovType || 'item';
                const label = type.charAt(0).toUpperCase() + type.slice(1);
                const isSelected = el === _selectedOvItem;
                const z = parseInt(el.style.zIndex) || 6;
                const vis = el.style.display !== 'none';
                html += `<div class="layer-row ${isSelected?'active-layer':''}" data-id="${el.id}" onclick="_selectOvItem(document.getElementById('${el.id}'))">
                    <span class="layer-vis" onclick="event.stopPropagation();toggleLayerVisibility('${el.id}')">${vis?'👁':'○'}</span>
                    <span class="layer-name">${label} <span style="font-size:8px;color:var(--muted);">z:${z}</span></span>
                    <button class="layer-order-btn" onclick="event.stopPropagation();moveLayerUp('${el.id}')">↑</button>
                    <button class="layer-order-btn" onclick="event.stopPropagation();moveLayerDown('${el.id}')">↓</button>
                </div>`;
            });
            panel.innerHTML = html;
        }

        function toggleLayerVisibility(id) {
            const el = document.getElementById(id);
            if (!el) return;
            const isHidden = el.style.display === 'none';
            el.style.display = isHidden ? '' : 'none';
            _updateLayerPanel();
            _ovSaveState();
        }

        function moveLayerUp(id) {
            const el = document.getElementById(id);
            if (!el) return;
            const current = parseInt(el.style.zIndex) || 6;
            const items = _getOvItems();
            let maxZ = 6;
            items.forEach(it => { const z = parseInt(it.style.zIndex) || 6; if (z > maxZ && it !== el) maxZ = z; });
            if (current >= maxZ) return;
            el.style.zIndex = current + 1;
            _updateLayerPanel();
            _ovSaveState();
        }

        function moveLayerDown(id) {
            const el = document.getElementById(id);
            if (!el) return;
            const current = parseInt(el.style.zIndex) || 6;
            if (current <= 6) return;
            el.style.zIndex = current - 1;
            _updateLayerPanel();
            _ovSaveState();
        }

        function bringToFront() {
            if (!_selectedOvItem) return;
            const items = _getOvItems();
            let maxZ = 6;
            items.forEach(it => { const z = parseInt(it.style.zIndex) || 6; if (z > maxZ) maxZ = z; });
            _selectedOvItem.style.zIndex = maxZ + 1;
            _updateLayerPanel();
            _ovSaveState();
        }

        function sendToBack() {
            if (!_selectedOvItem) return;
            _selectedOvItem.style.zIndex = 5;
            _updateLayerPanel();
            _ovSaveState();
        }

        function bringForward() {
            if (!_selectedOvItem) return;
            const current = parseInt(_selectedOvItem.style.zIndex) || 6;
            _selectedOvItem.style.zIndex = current + 1;
            _updateLayerPanel();
            _ovSaveState();
        }

        function sendBackward() {
            if (!_selectedOvItem) return;
            const current = parseInt(_selectedOvItem.style.zIndex) || 6;
            if (current > 6) {
                _selectedOvItem.style.zIndex = current - 1;
                _updateLayerPanel();
                _ovSaveState();
            }
        }

        function alignSelected(mode) {
            if (!_selectedOvItem) { alert('Select an item first.'); return; }
            const wrap = document.getElementById('canvas-wrap-static');
            if (!wrap) return;
            const wrapRect = wrap.getBoundingClientRect();
            const itemRect = _selectedOvItem.getBoundingClientRect();

            let left = parseFloat(_selectedOvItem.style.left) || 0;
            let top = parseFloat(_selectedOvItem.style.top) || 0;
            const w = itemRect.width;
            const h = itemRect.height;

            let newLeft = left,
                newTop = top;

            switch (mode) {
                case 'left':
                    newLeft = 0;
                    break;
                case 'right':
                    newLeft = wrapRect.width - w;
                    break;
                case 'top':
                    newTop = 0;
                    break;
                case 'bottom':
                    newTop = wrapRect.height - h;
                    break;
                case 'center-h':
                    newLeft = (wrapRect.width - w) / 2;
                    break;
                case 'center-v':
                    newTop = (wrapRect.height - h) / 2;
                    break;
                case 'distribute-h':
                    const items = _getOvItems();
                    const visItems = Array.from(items).filter(it => it.style.display !== 'none' && it !== _selectedOvItem);
                    if (visItems.length < 2) { alert('Need at least 2 items to distribute.'); return; }
                    const allItems = [ _selectedOvItem, ...visItems ];
                    const totalW = allItems.reduce((s, it) => s + it.getBoundingClientRect().width, 0);
                    const spacing = (wrapRect.width - totalW) / (allItems.length + 1);
                    let cx = spacing;
                    allItems.forEach((it, i) => {
                        const r = it.getBoundingClientRect();
                        const l = cx + (r.width - r.width) / 2;
                        it.style.left = l + 'px';
                        it.style.right = 'auto';
                        cx += r.width + spacing;
                    });
                    _ovSaveState();
                    return;
                case 'distribute-v':
                    const itemsV = _getOvItems();
                    const visV = Array.from(itemsV).filter(it => it.style.display !== 'none' && it !== _selectedOvItem);
                    if (visV.length < 2) { alert('Need at least 2 items to distribute.'); return; }
                    const allV = [ _selectedOvItem, ...visV ];
                    const totalH = allV.reduce((s, it) => s + it.getBoundingClientRect().height, 0);
                    const spacingV = (wrapRect.height - totalH) / (allV.length + 1);
                    let cy = spacingV;
                    allV.forEach((it, i) => {
                        const r = it.getBoundingClientRect();
                        const t = cy + (r.height - r.height) / 2;
                        it.style.top = t + 'px';
                        it.style.bottom = 'auto';
                        cy += r.height + spacingV;
                    });
                    _ovSaveState();
                    return;
                default:
                    return;
            }

            _selectedOvItem.style.left = newLeft + 'px';
            _selectedOvItem.style.right = 'auto';
            _selectedOvItem.style.top = newTop + 'px';
            _selectedOvItem.style.bottom = 'auto';
            _ovSaveState();
        }

        function _ovDragStart(e) {
            if (e.target.closest('.ov-resize-handle')) return;
            if (e.target.closest('.ov-rotate-handle')) return;
            if (e.target.closest('.ov-legend-title') && e.target.contentEditable === 'true') return;
            if (e.target.closest('.ov-legend-label') && e.target.contentEditable === 'true') return;
            if (e.target.closest('.ov-title .ov-drag-area') && e.target.contentEditable === 'true') return;
            if (e.target.closest('.ov-scale .ov-drag-area') && e.target.closest('[contenteditable]')) return;

            const el = e.currentTarget.closest('.ov-item');
            if (!el) return;
            if (!_layoutEditActive) return;

            _selectOvItem(el);

            const layer = document.getElementById('overlay-layer');
            const layerRect = layer.getBoundingClientRect();
            const elRect = el.getBoundingClientRect();

            const left = parseFloat(el.style.left) || 0;
            const top = parseFloat(el.style.top) || 0;

            _ovDragState = {
                el: el,
                layerRect: layerRect,
                offsetX: e.clientX - elRect.left,
                offsetY: e.clientY - elRect.top,
                startLeft: left,
                startTop: top,
                startX: e.clientX,
                startY: e.clientY,
            };

            el.style.cursor = 'grabbing';
            el.classList.add('dragging');
            el.setPointerCapture(e.pointerId);
            el.addEventListener('pointermove', _ovDragMove);
            el.addEventListener('pointerup', _ovDragEnd);
            el.addEventListener('pointercancel', _ovDragEnd);

            document.getElementById('ov-snap-h')?.classList.remove('visible');
            document.getElementById('ov-snap-v')?.classList.remove('visible');

            e.preventDefault();
        }

        function _ovDragMove(e) {
            if (!_ovDragState) return;
            const { el, layerRect, offsetX, offsetY, startLeft, startTop } = _ovDragState;

            let x = e.clientX - layerRect.left - offsetX;
            let y = e.clientY - layerRect.top - offsetY;

            const snapDist = 8;
            const snapH = document.getElementById('ov-snap-h');
            const snapV = document.getElementById('ov-snap-v');
            let snappedH = false,
                snappedV = false;

            if (x < snapDist) { x = 0;
                snappedV = true; }
            if (x + el.offsetWidth > layerRect.width - snapDist) { x = layerRect.width - el.offsetWidth;
                snappedV = true; }
            if (y < snapDist) { y = 0;
                snappedH = true; }
            if (y + el.offsetHeight > layerRect.height - snapDist) { y = layerRect.height - el.offsetHeight;
                snappedH = true; }

            const centerX = (layerRect.width - el.offsetWidth) / 2;
            const centerY = (layerRect.height - el.offsetHeight) / 2;
            if (Math.abs(x - centerX) < snapDist) { x = centerX;
                snappedV = true; }
            if (Math.abs(y - centerY) < snapDist) { y = centerY;
                snappedH = true; }

            const otherItems = _getOvItems();
            otherItems.forEach(other => {
                if (other === el || other.style.display === 'none') return;
                const or = other.getBoundingClientRect();
                const ol = parseFloat(other.style.left) || 0;
                const ot = parseFloat(other.style.top) || 0;
                const otherCenterX = ol + or.width / 2;
                const myCenterX = x + el.offsetWidth / 2;
                if (Math.abs(myCenterX - otherCenterX) < snapDist) {
                    x = otherCenterX - el.offsetWidth / 2;
                    snappedV = true;
                }
                const otherCenterY = ot + or.height / 2;
                const myCenterY = y + el.offsetHeight / 2;
                if (Math.abs(myCenterY - otherCenterY) < snapDist) {
                    y = otherCenterY - el.offsetHeight / 2;
                    snappedH = true;
                }
                if (Math.abs(x - ol) < snapDist) { x = ol;
                    snappedV = true; }
                if (Math.abs(x + el.offsetWidth - (ol + or.width)) < snapDist) { x = ol + or.width - el.offsetWidth;
                    snappedV = true; }
                if (Math.abs(y - ot) < snapDist) { y = ot;
                    snappedH = true; }
                if (Math.abs(y + el.offsetHeight - (ot + or.height)) < snapDist) { y = ot + or.height - el.offsetHeight;
                    snappedH = true; }
            });

            x = Math.max(0, Math.min(x, layerRect.width - el.offsetWidth));
            y = Math.max(0, Math.min(y, layerRect.height - el.offsetHeight));

            el.style.left = x + 'px';
            el.style.top = y + 'px';
            el.style.right = 'auto';
            el.style.bottom = 'auto';

            if (snapH && snapH) { snapH.style.top = y + 'px';
                snapH.classList.add('visible'); } else if (snapH) { snapH.classList.remove('visible'); }
            if (snapV && snapV) { snapV.style.left = x + 'px';
                snapV.classList.add('visible'); } else if (snapV) { snapV.classList.remove('visible'); }
        }

        function _ovDragEnd(e) {
            if (!_ovDragState) return;
            const { el } = _ovDragState;
            el.classList.remove('dragging');
            el.style.cursor = 'grab';
            el.removeEventListener('pointermove', _ovDragMove);
            el.removeEventListener('pointerup', _ovDragEnd);
            el.removeEventListener('pointercancel', _ovDragEnd);
            _ovDragState = null;

            document.getElementById('ov-snap-h')?.classList.remove('visible');
            document.getElementById('ov-snap-v')?.classList.remove('visible');

            _ovSaveState();
        }

        function _ovResizeStart(e) {
            const handle = e.currentTarget;
            const el = handle.closest('.ov-item');
            if (!el || !_layoutEditActive) return;

            _selectOvItem(el);

            const rect = el.getBoundingClientRect();
            const parentRect = el.parentElement.getBoundingClientRect();

            _ovResizeState = {
                el: el,
                handle: handle.className.split(' ').find(c => c.includes('ov-resize-handle')).replace('ov-resize-handle', '')
                    .trim() || 'se',
                startX: e.clientX,
                startY: e.clientY,
                startWidth: rect.width,
                startHeight: rect.height,
                startLeft: rect.left - parentRect.left,
                startTop: rect.top - parentRect.top,
                aspectRatio: (rect.width / rect.height) || 1,
            };

            el.setPointerCapture(e.pointerId);
            el.addEventListener('pointermove', _ovResizeMove);
            el.addEventListener('pointerup', _ovResizeEnd);
            el.addEventListener('pointercancel', _ovResizeEnd);
            e.preventDefault();
        }

        function _ovResizeMove(e) {
            if (!_ovResizeState) return;
            const { el, handle, startX, startY, startWidth, startHeight, startLeft, startTop, aspectRatio } = _ovResizeState;
            const dx = e.clientX - startX;
            const dy = e.clientY - startY;

            let newW = startWidth,
                newH = startHeight;
            let newL = startLeft,
                newT = startTop;

            const shiftKey = e.shiftKey;

            switch (handle) {
                case 'se':
                    newW = Math.max(30, startWidth + dx);
                    newH = shiftKey ? newW / aspectRatio : Math.max(20, startHeight + dy);
                    break;
                case 'sw':
                    newW = Math.max(30, startWidth - dx);
                    newH = shiftKey ? newW / aspectRatio : Math.max(20, startHeight + dy);
                    newL = startLeft + startWidth - newW;
                    break;
                case 'ne':
                    newW = Math.max(30, startWidth + dx);
                    newH = shiftKey ? newW / aspectRatio : Math.max(20, startHeight - dy);
                    newT = startTop + startHeight - newH;
                    break;
                case 'nw':
                    newW = Math.max(30, startWidth - dx);
                    newH = shiftKey ? newW / aspectRatio : Math.max(20, startHeight - dy);
                    newL = startLeft + startWidth - newW;
                    newT = startTop + startHeight - newH;
                    break;
                case 'n':
                    newH = Math.max(20, startHeight - dy);
                    newT = startTop + startHeight - newH;
                    break;
                case 's':
                    newH = Math.max(20, startHeight + dy);
                    break;
                case 'e':
                    newW = Math.max(30, startWidth + dx);
                    break;
                case 'w':
                    newW = Math.max(30, startWidth - dx);
                    newL = startLeft + startWidth - newW;
                    break;
                default:
                    return;
            }

            const keepAspect = shiftKey || el.dataset.ovType === 'north' || el.dataset.ovType === 'logo';
            if (keepAspect) {
                const ratio = startWidth / startHeight;
                if (handle.includes('e') || handle.includes('w')) {
                    newH = newW / ratio;
                } else if (handle.includes('n') || handle.includes('s')) {
                    newW = newH * ratio;
                } else {
                    if (Math.abs(dx) > Math.abs(dy)) {
                        newH = newW / ratio;
                    } else {
                        newW = newH * ratio;
                    }
                }
                if (handle.includes('n')) {
                    newT = startTop + startHeight - newH;
                }
                if (handle.includes('w')) {
                    newL = startLeft + startWidth - newW;
                }
            }

            el.style.width = newW + 'px';
            el.style.height = newH + 'px';
            el.style.left = newL + 'px';
            el.style.right = 'auto';
            el.style.top = newT + 'px';
            el.style.bottom = 'auto';

            if (el.dataset.ovType === 'north') {
                const svg = el.querySelector('.ov-north-svg');
                if (svg) {
                    const w = newW;
                    const h = newH;
                    svg.setAttribute('viewBox', `0 0 ${w} ${h}`);
                    const scale = Math.min(w / 40, h / 56);
                    svg.querySelector('text')?.setAttribute('font-size', 13 * scale);
                }
            }
        }

        function _ovResizeEnd(e) {
            if (!_ovResizeState) return;
            const { el } = _ovResizeState;
            el.removeEventListener('pointermove', _ovResizeMove);
            el.removeEventListener('pointerup', _ovResizeEnd);
            el.removeEventListener('pointercancel', _ovResizeEnd);
            _ovResizeState = null;
            _ovSaveState();
        }

        function _ovRotateStart(e) {
            const handle = e.currentTarget;
            const el = handle.closest('.ov-item');
            if (!el || !_layoutEditActive) return;

            _selectOvItem(el);

            const rect = el.getBoundingClientRect();
            const cx = rect.left + rect.width / 2;
            const cy = rect.top + rect.height / 2;

            _ovRotateState = {
                el: el,
                cx: cx,
                cy: cy,
                startAngle: parseFloat(el.dataset.rotation) || 0,
                startX: e.clientX,
                startY: e.clientY,
            };

            el.setPointerCapture(e.pointerId);
            el.addEventListener('pointermove', _ovRotateMove);
            el.addEventListener('pointerup', _ovRotateEnd);
            el.addEventListener('pointercancel', _ovRotateEnd);
            e.preventDefault();
        }

        function _ovRotateMove(e) {
            if (!_ovRotateState) return;
            const { el, cx, cy, startAngle, startX, startY } = _ovRotateState;

            const dx = e.clientX - cx;
            const dy = e.clientY - cy;
            const angle = Math.atan2(dy, dx) * 180 / Math.PI;

            const startDx = startX - cx;
            const startDy = startY - cy;
            const startAngleRaw = Math.atan2(startDy, startDx) * 180 / Math.PI;

            let delta = angle - startAngleRaw;
            while (delta < -180) delta += 360;
            while (delta > 180) delta -= 360;

            const newAngle = startAngle + delta;
            el.style.transform = `rotate(${newAngle}deg)`;
            el.dataset.rotation = newAngle;
        }

        function _ovRotateEnd(e) {
            if (!_ovRotateState) return;
            const { el } = _ovRotateState;
            el.removeEventListener('pointermove', _ovRotateMove);
            el.removeEventListener('pointerup', _ovRotateEnd);
            el.removeEventListener('pointercancel', _ovRotateEnd);
            _ovRotateState = null;
            _ovSaveState();
        }

        function toggleLayoutEdit() {
            const img = document.getElementById('out-img');
            if (!currentRunId || !img || img.style.display === 'none') {
                alert('Generate a map first, then you can reposition layout items.');
                return;
            }
            _layoutEditActive = !_layoutEditActive;
            const layer = document.getElementById('overlay-layer');
            const editBtn = document.getElementById('layout-edit-btn');
            const resetBtn = document.getElementById('layout-reset-btn');
            const hint = document.getElementById('layout-hint');

            layer.classList.toggle('editing', _layoutEditActive);
            editBtn.textContent = _layoutEditActive ? '✕ Done Editing' : '✥ Edit Layout';
            editBtn.style.background = _layoutEditActive ? 'rgba(16,185,129,.18)' : '';
            editBtn.style.borderColor = _layoutEditActive ? 'var(--mint)' : '';
            resetBtn.style.display = _layoutEditActive ? '' : 'none';
            hint.style.display = _layoutEditActive ? '' : 'none';

            if (_layoutEditActive) {
                _rebuildOverlayLegend(activeModule, currentGeoJSON);
                _wireOvEvents();
                _ovRestoreState();
                _updateLayerPanel();
                document.getElementById('layer-panel')?.classList.add('visible');
                document.getElementById('align-toolbar')?.classList.add('visible');
            } else {
                _deselectAll();
                document.getElementById('layer-panel')?.classList.remove('visible');
                document.getElementById('align-toolbar')?.classList.remove('visible');
                _ovSaveState();
            }
        }

        function _wireOvEvents() {
            _getOvItems().forEach(el => {
                if (el.dataset.wired) return;
                el.dataset.wired = '1';

                const area = el.querySelector('.ov-drag-area');
                if (area) {
                    area.addEventListener('pointerdown', _ovDragStart);
                }

                el.querySelectorAll('.ov-resize-handle').forEach(h => {
                    h.addEventListener('pointerdown', _ovResizeStart);
                });

                const rot = el.querySelector('.ov-rotate-handle');
                if (rot) {
                    rot.addEventListener('pointerdown', _ovRotateStart);
                }

                el.addEventListener('click', function(e) {
                    if (e.target.closest('.ov-resize-handle')) return;
                    if (e.target.closest('.ov-rotate-handle')) return;
                    if (e.target.closest('[contenteditable]')) return;
                    if (!_layoutEditActive) return;
                    _selectOvItem(this);
                });

                el.addEventListener('dblclick', function(e) {
                    const editable = this.querySelector('[contenteditable]');
                    if (editable && _layoutEditActive) {
                        editable.focus();
                        const sel = window.getSelection();
                        const range = document.createRange();
                        range.selectNodeContents(editable);
                        sel.removeAllRanges();
                        sel.addRange(range);
                    }
                });
            });
        }

        function _rebuildOverlayLegend(module, geojson) {
            const body    = document.getElementById('ov-legend-body');
            const titleEl = document.getElementById('ov-legend-title');
            if (!body) return;
            body.innerHTML = '';
            const COMP_COLORS = ['#2196F3','#FF9800','#4CAF50','#9C27B0','#F48FB1',
                '#607D8B','#CDDC39','#00BCD4','#FF5722','#795548','#E91E63',
                '#009688','#FFC107','#3F51B5','#8BC34A'];
            const totalHa = geojson?.features?.reduce((s,f)=>s+(parseFloat(f.properties?.Area_ha)||0),0)||0;
            let rows=[], legendTitle='Legend';

            if (module==='A') {
                legendTitle='Group A — Boundary Survey';
                rows=[{color:'#0000DD',label:'Forest Boundary',type:'line'},
                      {color:'#FF0000',label:'Survey Points',type:'circle'}];
                if(totalHa>0) rows.push({color:'',label:`Area = ${totalHa.toFixed(3)} ha`,type:'text'});

            } else if (module==='B') {
                legendTitle='Group B — Segmented Boundary';
                const seen=new Map(); let ci=0;
                geojson?.features?.forEach(f=>{
                    const id=f.properties?.Forest||f.properties?.Comp_ID||f.properties?.Name||'Segment';
                    if(!seen.has(id)) seen.set(id,COMP_COLORS[ci++%COMP_COLORS.length]);
                });
                seen.forEach((color,label)=>rows.push({color,label,type:'polygon'}));
                rows.push({color:'#0000DD',label:'Boundary',type:'line'});
                rows.push({color:'#FF0000',label:'Survey Points',type:'circle'});
                if(totalHa>0) rows.push({color:'',label:`Area = ${totalHa.toFixed(3)} ha`,type:'text'});

            } else if (module==='C') {
                legendTitle='Group C — Sample Plots';
                rows=[{color:'#0000DD',label:'Forest Boundary',type:'line'},
                      {color:'#FF0000',label:'Survey Points',type:'circle'},
                      {color:'#10b981',label:'Sample Plot Points',type:'circle'}];
                if(totalHa>0) rows.push({color:'',label:`Area = ${totalHa.toFixed(3)} ha`,type:'text'});

            } else if (module==='D') {
                legendTitle='Group D — Multi-Forest';
                const seen=new Map(); let ci=0;
                geojson?.features?.filter(f=>f.geometry?.type!=='Point'&&f.geometry?.type!=='MultiPoint').forEach(f=>{
                    const id=f.properties?.Forest||f.properties?.Comp_ID||f.properties?.Name||'Forest';
                    if(!seen.has(id)) seen.set(id,COMP_COLORS[ci++%COMP_COLORS.length]);
                });
                seen.forEach((color,label)=>rows.push({color,label,type:'polygon'}));
                rows.push({color:'#0000DD',label:'Boundary',type:'line'});
                rows.push({color:'#FF0000',label:'Survey Points',type:'circle'});
                if(totalHa>0) rows.push({color:'',label:`Total = ${totalHa.toFixed(3)} ha`,type:'text'});

            } else if (module==='E') {
                legendTitle='Group E — Compartments';
                const seen=new Map(); let ci=0;
                geojson?.features?.filter(f=>f.geometry?.type!=='Point'&&f.geometry?.type!=='MultiPoint').forEach(f=>{
                    const id=f.properties?.Comp_ID||f.properties?.Compartment||`Comp ${ci+1}`;
                    if(!seen.has(id)) seen.set(id,COMP_COLORS[ci++%COMP_COLORS.length]);
                });
                seen.forEach((color,label)=>rows.push({color,label,type:'polygon'}));
                rows.push({color:'#111',label:'Boundary',type:'line'});
                if(totalHa>0) rows.push({color:'',label:`Total = ${totalHa.toFixed(3)} ha`,type:'text'});

            } else if (module==='F') {
                legendTitle='Group F — Slope Analysis';
                rows=[{color:'#2e8b57',label:'0–19°  Gentle',type:'polygon'},
                      {color:'#ffd700',label:'19–31° Moderate',type:'polygon'},
                      {color:'#ef4444',label:'>31°   Steep',type:'polygon'},
                      {color:'#000',label:'Forest Boundary',type:'line'}];

            } else if (module==='G') {
                legendTitle='Group G — Survey Point Generator';
                rows=[{color:'#2196F3',label:'Vertex Points',type:'circle'},
                      {color:'#FF9800',label:'Boundary Points',type:'circle'},
                      {color:'#9C27B0',label:'Divider Points',type:'circle'},
                      {color:'#0000DD',label:'Forest Boundary',type:'line'}];

            } else if (module==='H') {
                legendTitle='Group H — Sample Point Based';
                rows=[{color:'#2e8b57',label:'0–19°  Gentle',type:'polygon'},
                      {color:'#ffd700',label:'19–31° Moderate',type:'polygon'},
                      {color:'#ef4444',label:'>31°   Steep',type:'polygon'},
                      {color:'#0000DD',label:'Forest Boundary',type:'line'},
                      {color:'#FF0000',label:'Survey Points',type:'circle'},
                      {color:'#10b981',label:'Sample Plot Points',type:'circle'}];
            } else {
                rows=[{color:'#0000DD',label:'Forest Boundary',type:'line'},
                      {color:'#FF0000',label:'Survey Points',type:'circle'}];
                if(totalHa>0) rows.push({color:'',label:`Area = ${totalHa.toFixed(3)} ha`,type:'text'});
            }

            if(titleEl&&!titleEl.dataset.userEdited) titleEl.textContent=legendTitle;

            rows.forEach(r=>{
                const row=document.createElement('div');
                row.className='ov-legend-row';
                if(r.type!=='text'){
                    const sw=document.createElement('div');
                    sw.className='ov-legend-swatch'+(r.type==='circle'?' circle':r.type==='line'?' line':'');
                    sw.style.background=r.color;
                    if(r.type!=='line') sw.style.borderColor=r.color;
                    row.appendChild(sw);
                }
                const lb=document.createElement('span');
                lb.className='ov-legend-label';
                lb.textContent=r.label;
                row.appendChild(lb);
                body.appendChild(row);
            });
            if(titleEl) titleEl.addEventListener('input',()=>{titleEl.dataset.userEdited='1';},{once:true});
        }

        function showOverlayForRun() {
            if (currentRunId) {
                const layer = document.getElementById('overlay-layer');
                const img = document.getElementById('out-img');
                if (img && img.style.display !== 'none') {
                    layer.classList.add('visible');
                    _rebuildOverlayLegend(activeModule, currentGeoJSON);
                    _wireOvEvents();
                    _ovRestoreState();
                    if (!_layoutEditActive) layer.classList.remove('editing');
                    const titleEl = document.getElementById('ov-title')?.querySelector('.ov-drag-area');
                    if (titleEl) {
                        const compTitle = document.getElementById('comp-title')?.value ||
                            document.getElementById('g-title')?.value || 'Map Title';
                        if (compTitle) titleEl.textContent = compTitle;
                    }
                    const areaEl = document.getElementById('ov-area')?.querySelector('.ov-drag-area');
                    if (areaEl && currentGeoJSON) {
                        const ah = currentGeoJSON.features?.reduce((s,f)=>s+(f.properties?.Area_ha||0),0)||0;
                        if (ah > 0) areaEl.textContent = `Area: ${ah.toFixed(3)} ha`;
                    }
                    const scaleEl = document.getElementById('ov-scale')?.querySelector('.ov-drag-area span');
                    if (scaleEl && currentGeoJSON) {
                        if (!scaleEl.textContent.trim() || scaleEl.textContent === '0    500    1000 m') {
                            try {
                                const bounds = currentGeoJSON.features.reduce((b,f) => {
                                    if (!f.geometry) return b;
                                    const coords = f.geometry.coordinates;
                                    if (f.geometry.type === 'Polygon') {
                                        coords[0].forEach(c => {
                                            if(c[0]<b.minX) b.minX=c[0]; if(c[0]>b.maxX) b.maxX=c[0];
                                            if(c[1]<b.minY) b.minY=c[1]; if(c[1]>b.maxY) b.maxY=c[1];
                                        });
                                    } else if (f.geometry.type === 'Point') {
                                        if(coords[0]<b.minX) b.minX=coords[0]; if(coords[0]>b.maxX) b.maxX=coords[0];
                                        if(coords[1]<b.minY) b.minY=coords[1]; if(coords[1]>b.maxY) b.maxY=coords[1];
                                    }
                                    return b;
                                }, {minX:Infinity,maxX:-Infinity,minY:Infinity,maxY:-Infinity});
                                if (isFinite(bounds.minX)) {
                                    const widthM = bounds.maxX - bounds.minX;
                                    if (widthM > 0) {
                                        const scaleKm = Math.round(widthM / 1000);
                                        const display = Math.max(1, Math.round(scaleKm / 2) * 2);
                                        scaleEl.textContent = `0    ${display/2}    ${display} km`;
                                    }
                                }
                            } catch(e) {}
                        }
                    }
                }
            }
        }

        function _ovSaveState() {
            if (!currentRunId) return;
            const state = {};
            _getOvItems().forEach(el => {
                const id = el.id;
                if (!id) return;
                state[id] = {
                    left: el.style.left || '',
                    top: el.style.top || '',
                    right: el.style.right || '',
                    bottom: el.style.bottom || '',
                    width: el.style.width || '',
                    height: el.style.height || '',
                    zIndex: el.style.zIndex || '6',
                    transform: el.style.transform || '',
                    rotation: el.dataset.rotation || '0',
                    display: el.style.display || '',
                };
                if (el.id === 'ov-legend') {
                    const body = document.getElementById('ov-legend-body');
                    const title = document.getElementById('ov-legend-title');
                    if (body) state[id]['legend-html'] = body.innerHTML;
                    if (title) state[id]['legend-title'] = title.textContent;
                }
                if (el.id === 'ov-title') {
                    const area = el.querySelector('.ov-drag-area');
                    if (area) state[id]['text'] = area.textContent;
                }
                if (el.id === 'ov-area') {
                    const area = el.querySelector('.ov-drag-area');
                    if (area) state[id]['text'] = area.textContent;
                }
                if (el.id === 'ov-scale') {
                    const span = el.querySelector('.ov-drag-area span');
                    if (span) state[id]['text'] = span.textContent;
                }
            });
            try {
                localStorage.setItem('ov-layout-' + currentRunId, JSON.stringify(state));
            } catch (e) { /* ignore */ }
        }

        function _ovRestoreState() {
            if (!currentRunId) return;
            let state;
            try { state = JSON.parse(localStorage.getItem('ov-layout-' + currentRunId) || 'null'); } catch (e) { return; }
            if (!state) return;

            _getOvItems().forEach(el => {
                const id = el.id;
                if (!id || !state[id]) return;
                const s = state[id];
                if (s.left) el.style.left = s.left;
                if (s.top) el.style.top = s.top;
                if (s.right) el.style.right = s.right;
                if (s.bottom) el.style.bottom = s.bottom;
                if (s.width) el.style.width = s.width;
                if (s.height) el.style.height = s.height;
                if (s.zIndex) el.style.zIndex = s.zIndex;
                if (s.transform) el.style.transform = s.transform;
                if (s.rotation) el.dataset.rotation = s.rotation;
                if (s.display !== undefined) el.style.display = s.display;

                if (id === 'ov-legend') {
                    if (s['legend-html']) {
                        const body = document.getElementById('ov-legend-body');
                        if (body) body.innerHTML = s['legend-html'];
                    }
                    if (s['legend-title']) {
                        const title = document.getElementById('ov-legend-title');
                        if (title) title.textContent = s['legend-title'];
                    }
                }
                if (id === 'ov-title') {
                    if (s['text']) {
                        const area = el.querySelector('.ov-drag-area');
                        if (area) area.textContent = s['text'];
                    }
                }
                if (id === 'ov-area') {
                    if (s['text']) {
                        const area = el.querySelector('.ov-drag-area');
                        if (area) area.textContent = s['text'];
                    }
                }
                if (id === 'ov-scale') {
                    if (s['text']) {
                        const span = el.querySelector('.ov-drag-area span');
                        if (span) span.textContent = s['text'];
                    }
                }
            });
            _updateLayerPanel();
        }

        function resetLayoutPositions() {
            const defaults = {
                'ov-legend': { right: '4%', bottom: '5%', width: '180px', height: 'auto', zIndex: '6' },
                'ov-north': { right: '6%', top: '4%', width: '42px', height: '58px', zIndex: '7' },
                'ov-scale': { left: '6%', bottom: '6%', width: '120px', height: '28px', zIndex: '8' },
                'ov-title': { left: '50%', top: '2%', transform: 'translateX(-50%)', width: 'auto', height: 'auto',
                    zIndex: '9' },
                'ov-area': { right: '4%', top: '12%', width: 'auto', height: 'auto', zIndex: '10' },
                'ov-logo': { left: '4%', top: '2%', width: '44px', height: '44px', zIndex: '11' },
            };

            _getOvItems().forEach(el => {
                const id = el.id;
                if (defaults[id]) {
                    const d = defaults[id];
                    el.style.left = d.left || '';
                    el.style.top = d.top || '';
                    el.style.right = d.right || '';
                    el.style.bottom = d.bottom || '';
                    el.style.width = d.width || '';
                    el.style.height = d.height || '';
                    el.style.transform = d.transform || '';
                    el.style.zIndex = d.zIndex || '6';
                    el.dataset.rotation = '0';
                    el.style.display = '';
                }
            });

            const titleEl = document.getElementById('ov-title')?.querySelector('.ov-drag-area');
            if (titleEl) titleEl.textContent = document.getElementById('comp-title')?.value || document.getElementById('g-title')
                ?.value || 'Map Title';

            const areaEl = document.getElementById('ov-area')?.querySelector('.ov-drag-area');
            if (areaEl && currentGeoJSON) {
                const ah = currentGeoJSON.features?.reduce((s, f) => s + (f.properties?.Area_ha || 0), 0) || 0;
                areaEl.textContent = ah > 0 ? `Area: ${ah.toFixed(3)} ha` : 'Area: 0.00 ha';
            }

            const scaleEl = document.getElementById('ov-scale')?.querySelector('.ov-drag-area span');
            if (scaleEl) scaleEl.textContent = '0    500    1000 m';

            if (currentRunId) try { localStorage.removeItem('ov-layout-' + currentRunId); } catch (e) {}
            _ovSaveState();
            _updateLayerPanel();
        }

        function saveLayout() {
            if (!currentRunId) { alert('No run available. Generate a map first.'); return; }
            _ovSaveState();
            const state = {};
            _getOvItems().forEach(el => {
                const id = el.id;
                if (!id) return;
                const rect = el.getBoundingClientRect();
                const parentRect = el.parentElement.getBoundingClientRect();
                state[id] = {
                    left: parseFloat(el.style.left) || 0,
                    top: parseFloat(el.style.top) || 0,
                    right: parseFloat(el.style.right) || 0,
                    bottom: parseFloat(el.style.bottom) || 0,
                    width: rect.width,
                    height: rect.height,
                    zIndex: parseInt(el.style.zIndex) || 6,
                    rotation: parseFloat(el.dataset.rotation) || 0,
                    display: el.style.display !== 'none',
                    type: el.dataset.ovType || 'item',
                };
                if (id === 'ov-legend') {
                    const body = document.getElementById('ov-legend-body');
                    const title = document.getElementById('ov-legend-title');
                    if (body) state[id].legendHtml = body.innerHTML;
                    if (title) state[id].legendTitle = title.textContent;
                }
                if (id === 'ov-title') {
                    const area = el.querySelector('.ov-drag-area');
                    if (area) state[id].text = area.textContent;
                }
                if (id === 'ov-area') {
                    const area = el.querySelector('.ov-drag-area');
                    if (area) state[id].text = area.textContent;
                }
                if (id === 'ov-scale') {
                    const span = el.querySelector('.ov-drag-area span');
                    if (span) state[id].text = span.textContent;
                }
            });

            const json = JSON.stringify({ runId: currentRunId, module: activeModule, items: state, timestamp: Date
                    .now() }, null, 2);
            const blob = new Blob([json], { type: 'application/json' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `layout_${currentRunId.slice(0,8)}.json`;
            a.click();
            URL.revokeObjectURL(url);
        }

        function loadLayoutFromFile(event) {
            const file = event.target.files[0];
            if (!file) return;
            const reader = new FileReader();
            reader.onload = function(e) {
                try {
                    const data = JSON.parse(e.target.result);
                    if (!data.items) { alert('Invalid layout file.'); return; }
                    _getOvItems().forEach(el => {
                        const id = el.id;
                        if (!id || !data.items[id]) return;
                        const s = data.items[id];
                        if (s.left !== undefined) el.style.left = s.left + 'px';
                        if (s.top !== undefined) el.style.top = s.top + 'px';
                        if (s.right !== undefined) el.style.right = s.right + 'px';
                        if (s.bottom !== undefined) el.style.bottom = s.bottom + 'px';
                        if (s.width) el.style.width = s.width + 'px';
                        if (s.height) el.style.height = s.height + 'px';
                        if (s.zIndex) el.style.zIndex = s.zIndex;
                        if (s.rotation) { el.style.transform = `rotate(${s.rotation}deg)`;
                            el.dataset.rotation = s.rotation; }
                        if (s.display !== undefined) el.style.display = s.display ? '' : 'none';

                        if (id === 'ov-legend') {
                            if (s.legendHtml) {
                                const body = document.getElementById('ov-legend-body');
                                if (body) body.innerHTML = s.legendHtml;
                            }
                            if (s.legendTitle) {
                                const title = document.getElementById('ov-legend-title');
                                if (title) title.textContent = s.legendTitle;
                            }
                        }
                        if (id === 'ov-title' && s.text) {
                            const area = el.querySelector('.ov-drag-area');
                            if (area) area.textContent = s.text;
                        }
                        if (id === 'ov-area' && s.text) {
                            const area = el.querySelector('.ov-drag-area');
                            if (area) area.textContent = s.text;
                        }
                        if (id === 'ov-scale' && s.text) {
                            const span = el.querySelector('.ov-drag-area span');
                            if (span) span.textContent = s.text;
                        }
                    });
                    _ovSaveState();
                    _updateLayerPanel();
                    alert('Layout loaded successfully.');
                } catch (err) {
                    alert('Error loading layout: ' + err.message);
                }
            };
            reader.readAsText(file);
            event.target.value = '';
        }

        // ── Full View ──
        function viewFullMap() {
            const modal = document.getElementById('map-modal');
            const mimg = document.getElementById('modal-img');
            const mdl = document.getElementById('modal-dl-btn');
            const target = document.getElementById('canvas-wrap-static');

            if (mdl) {
                mdl.href = '#';
                mdl.download = '';
                mdl.style.display = 'inline-block';
            }

            mimg.src = '';
            modal.style.display = 'flex';

            const fallbackToRaw = () => {
                const src = document.getElementById('out-img')?.src;
                if (src && !src.endsWith('undefined') && !src.endsWith('null') && src.length > 10) {
                    mimg.src = src;
                    if (mdl) {
                        mdl.href = src;
                        mdl.download = 'elfak_map_raw.png';
                    }
                } else {
                    mimg.alt = 'Error: No map image available. Click "Open Full PNG" in the Composer tab.';
                    if (mdl) {
                        mdl.href = '#';
                        mdl.download = '';
                        mdl.style.display = 'none';
                    }
                    alert('Could not generate composite image. Please use the "Open Full PNG" button inside the Composer tab to view the raw map.');
                }
            };

            const layer = document.getElementById('overlay-layer');
            const wasEditing = layer.classList.contains('editing');
            const wasVisible = layer.classList.contains('visible');

            _getOvItems().forEach(el => {
                if (el.style.display === 'none') {
                    el.dataset.hiddenForCapture = 'true';
                    el.style.display = '';
                }
            });

            html2canvas(target, {
                scale: 2.0,
                useCORS: true,
                allowTaint: false,
                backgroundColor: null,
                logging: false,
                onclone: (clonedDoc) => {
                    const clonedOverlay = clonedDoc.querySelector('#overlay-layer');
                    if (clonedOverlay) {
                        clonedOverlay.classList.add('visible');
                        clonedOverlay.querySelectorAll('.ov-item').forEach(item => {
                            item.style.display = '';
                        });
                    }
                }
            }).then(canvas => {
                _getOvItems().forEach(el => {
                    if (el.dataset.hiddenForCapture === 'true') {
                        el.style.display = 'none';
                        delete el.dataset.hiddenForCapture;
                    }
                });
                try {
                    const dataUrl = canvas.toDataURL('image/png');
                    mimg.src = dataUrl;
                    if (mdl) {
                        mdl.href = dataUrl;
                        mdl.download = 'elfak_map_edited.png';
                        mdl.style.display = 'inline-block';
                    }
                } catch (e) {
                    console.error('Canvas toDataURL failed:', e);
                    fallbackToRaw();
                }
            }).catch(err => {
                _getOvItems().forEach(el => {
                    if (el.dataset.hiddenForCapture === 'true') {
                        el.style.display = 'none';
                        delete el.dataset.hiddenForCapture;
                    }
                });
                console.error('Composite capture failed:', err);
                fallbackToRaw();
            });
        }

        // ── Composer: load every current map text for editing ──
        async function refreshComposerTexts() {
            const box = document.getElementById('comp-legend-rows');
            const statusEl = document.getElementById('comp-status');
            const setStat = t => { if (statusEl) statusEl.textContent = t; };
            if (!currentRunId) { setStat('Run a pipeline first.'); return false; }
            const mod = (document.getElementById('comp-module') || {}).value || activeModule || 'A';
            setStat('Loading map texts…');
            try {
                const d = await fetchJSON(`${BASE}/map_texts/${currentRunId}?module=${encodeURIComponent(mod)}`);
                if (d.error) throw new Error(d.error);
                const fill = (id, v) => {
                    const el = document.getElementById(id);
                    if (el && !el.value && v) el.value = v;
                };
                fill('comp-title', d.title);
                fill('comp-subtitle', d.subtitle);
                fill('comp-area', d.area);
                fill('comp-legend', d.legend_title);
                const modSel = document.getElementById('comp-module');
                if (modSel && d.module) modSel.value = d.module;
                try { updateCompHint(); } catch (_) {}
                box.innerHTML = '';
                (d.rows || []).forEach((txt, i) => {
                    const row = document.createElement('div');
                    row.className = 'comp-row';
                    const n = document.createElement('span');
                    n.className = 'comp-row-n';
                    n.textContent = (i + 1);
                    const inp = document.createElement('input');
                    inp.className = 'comp-row-inp';
                    inp.type = 'text';
                    inp.placeholder = txt || ('Row ' + (i + 1));
                    inp.title = 'Blank = keep: ' + (txt || '');
                    row.appendChild(n);
                    row.appendChild(inp);
                    box.appendChild(row);
                });
                if (!(d.rows || []).length) {
                    box.innerHTML = '<div class="comp-rows-hint">No legend rows found for this run.</div>';
                }
                setStat('Map texts loaded.' + (d.note ? ' ' + d.note : ''));
                return true;
            } catch (e) {
                setStat('Error: ' + e.message);
                return false;
            }
        }

        // ── Composer: per-module guidance ──
        const COMP_HINTS = {
            A: 'Boundary map: rename the SN survey points, boundary and forest rows.',
            B: 'Segmented map: one legend row per forest with its area.',
            C: 'Sample-plot map: rename the plot-point row; hide numbers if dense.',
            D: 'Multi-forest map: one legend row per forest with its area.',
            E: 'Subdivision map: rename Compartment-N rows (areas included), SN points listed last.',
            F: 'Slope map: rename the three slope-class rows; hectares stay measured.',
            G: 'Survey-point map: Vertex / Boundary / Divider rows with counts; toggle point IDs.',
            H: 'Sample-point slope map: same rows as slope plus survey points.'
        };
        function updateCompHint() {
            const el = document.getElementById('comp-mod-hint');
            const mod = ((document.getElementById('comp-module') || {}).value || 'A').toUpperCase();
            if (el) el.textContent = COMP_HINTS[mod] || '';
        }

        // ── Do Compose ──
        async function doCompose(extraPayload) {
            if (!currentRunId) { alert('Run a pipeline first.'); return false; }
            const statusEl = document.getElementById('comp-status');
            if (statusEl) statusEl.textContent = 'Re-rendering…';

            _ovSaveState();

            const legendEl = document.getElementById('ov-legend');
            const northEl = document.getElementById('ov-north');
            const legendPos = legendEl ? getRelativePosition(legendEl) : null;
            const northPos = northEl ? getRelativePosition(northEl) : null;

            const payload = Object.assign({
                title: document.getElementById('comp-title').value || document.getElementById('g-title').value || '',
                subtitle: document.getElementById('comp-subtitle').value || '',
                area_text: document.getElementById('comp-area').value || '',
                legend_labels: [...document.querySelectorAll('#comp-legend-rows .comp-row-inp')].map(e => e.value),
                point_labels: document.getElementById('comp-ptlabels').value || 'auto',
                legend_title: document.getElementById('comp-legend').value || 'Legend',
                label_col: document.getElementById('comp-label').value || document.getElementById('g-label').value || '',
                module: document.getElementById('comp-module').value || activeModule,
                legend_pos: legendPos,
                north_pos: northPos,
                layout_state: _getLayoutStateForServer(),
            }, extraPayload || {});

            try {
                const d = await fetchJSON(`${BASE}/compose/${currentRunId}`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                if (d.error) throw new Error(d.error);
                if (statusEl) statusEl.textContent = '✅ Map re-rendered successfully!';
                const img = document.getElementById('out-img');
                img.style.opacity = '0';
                img.onload = () => {
                    img.style.display = 'block';
                    img.style.transition = 'opacity .35s var(--ease)';
                    requestAnimationFrame(() => { img.style.opacity = '1'; });
                    setTimeout(() => img.style.transition = '', 400);
                    setTimeout(() => showOverlayForRun(), 100);
                };
                img.src = `${BASE}${d.png}`;
                document.getElementById('empty-msg').style.display = 'none';
                switchPView('static');
                return true;
            } catch (e) {
                if (statusEl) statusEl.textContent = '❌ Error: ' + e.message;
                else alert('Compose error: ' + e.message);
                return false;
            }
        }

        function _getLayoutStateForServer() {
            const state = {};
            _getOvItems().forEach(el => {
                const id = el.id;
                if (!id) return;
                const rect = el.getBoundingClientRect();
                const parentRect = el.parentElement.getBoundingClientRect();
                state[id] = {
                    left: parseFloat(el.style.left) || 0,
                    top: parseFloat(el.style.top) || 0,
                    width: rect.width,
                    height: rect.height,
                    zIndex: parseInt(el.style.zIndex) || 6,
                    rotation: parseFloat(el.dataset.rotation) || 0,
                    visible: el.style.display !== 'none',
                    type: el.dataset.ovType || 'item',
                };
                if (id === 'ov-title' || id === 'ov-area') {
                    const area = el.querySelector('.ov-drag-area');
                    if (area) state[id].text = area.textContent;
                }
                if (id === 'ov-legend') {
                    const body = document.getElementById('ov-legend-body');
                    const title = document.getElementById('ov-legend-title');
                    if (body) state[id].legendHtml = body.innerHTML;
                    if (title) state[id].legendTitle = title.textContent;
                }
                if (id === 'ov-scale') {
                    const span = el.querySelector('.ov-drag-area span');
                    if (span) state[id].text = span.textContent;
                }
            });
            return state;
        }

        // ── Export Layout ──
        async function exportLayout() {
            const runId = currentRunId;
            if (!runId) {
                alert('No active run. Please generate a map first.');
                return;
            }

            if (typeof _ovSaveState === 'function') {
                _ovSaveState();
            }

            let layoutState = {};
            if (typeof _getLayoutStateForServer === 'function') {
                layoutState = _getLayoutStateForServer();
            } else {
                layoutState = {
                    'ov-title': {
                        visible: true,
                        left: 50, top: 4,
                        text: document.getElementById('comp-title')?.value || 'Forest Map'
                    },
                    'ov-area': {
                        visible: true,
                        left: 50, top: 10,
                        text: document.getElementById('ov-area')?.querySelector('.ov-drag-area')?.textContent || 'Area: 0.00 ha'
                    },
                    'ov-north': {
                        visible: true,
                        left: 88, top: 6,
                        width: 5, height: 7,
                        rotation: 0
                    },
                    'ov-legend': {
                        visible: true,
                        left: 4, top: 80,
                        width: 22, height: 14,
                        legendTitle: document.getElementById('comp-legend')?.value || 'Legend',
                        legendHtml: document.getElementById('ov-legend-body')?.innerHTML || ''
                    },
                    'ov-scale': {
                        visible: true,
                        left: 35, top: 86,
                        width: 30, height: 4,
                        text: document.getElementById('ov-scale')?.querySelector('.ov-drag-area span')?.textContent || '1:10,000'
                    }
                };
            }

            const fmt = document.getElementById('export-format').value;
            const dpi = parseInt(document.getElementById('export-dpi').value) || 200;
            const pageW = parseFloat(document.getElementById('export-page-w').value) || 8.27;
            const pageH = parseFloat(document.getElementById('export-page-h').value) || 11.69;

            const status = document.getElementById('export-status');
            status.textContent = '⏳ Generating official map layout…';
            status.style.color = 'var(--muted)';

            try {
                const response = await fetch(`${BASE}/export_layout`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        run_id: runId,
                        layout_state: layoutState,
                        title: document.getElementById('comp-title')?.value || document.getElementById('g-title')?.value || '',
                        subtitle: document.getElementById('comp-subtitle')?.value || '',
                        area_text: document.getElementById('comp-area')?.value || '',
                        legend_labels: [...document.querySelectorAll('#comp-legend-rows .comp-row-inp')].map(e => e.value),
                        point_labels: document.getElementById('comp-ptlabels')?.value || 'auto',
                        legend_title: document.getElementById('comp-legend')?.value || 'Legend',
                        label_col: document.getElementById('comp-label')?.value || document.getElementById('g-label')?.value || '',
                        module: document.getElementById('comp-module')?.value || activeModule,
                        format: fmt,
                        dpi: dpi,
                        page_width: pageW,
                        page_height: pageH
                    })
                });

                if (!response.ok) {
                    const err = await response.json();
                    throw new Error(err.error || 'Export failed');
                }

                const blob = await response.blob();
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `layout.${fmt}`;
                document.body.appendChild(a);
                a.click();
                a.remove();
                URL.revokeObjectURL(url);

                status.textContent = '✅ Map exported successfully!';
                status.style.color = 'var(--mint)';
            } catch (e) {
                status.textContent = '❌ Error: ' + e.message;
                status.style.color = 'var(--red)';
                console.error(e);
            }
        }

        // ── Download ZIP override ──
        const dlBtn = document.getElementById('dl-btn');
        if (dlBtn) {
            const newBtn = dlBtn.cloneNode(true);
            dlBtn.parentNode.replaceChild(newBtn, dlBtn);
            newBtn.addEventListener('click', async function(e) {
                e.preventDefault();
                if (!currentRunId) { alert('No run available.'); return; }
                const ok = await doCompose({});
                if (ok) {
                    window.location.href = `${BASE}/download/${currentRunId}`;
                } else {
                    alert('Failed to re‑render map before download. Please try again.');
                }
            });
        }

        // ── Mobile nav ──
        function toggleMobileNav() {
            const panel = document.getElementById('left-nav-panel');
            const bg = document.getElementById('mobile-nav-bg');
            const btn = document.getElementById('mobile-nav-btn');
            const isOpen = panel.classList.toggle('mobile-open');
            bg.classList.toggle('show', isOpen);
            btn.classList.toggle('open', isOpen);
        }
        document.addEventListener('click', (e) => {
            if (e.target.closest('.nav-item') && window.innerWidth <= 820) {
                const panel = document.getElementById('left-nav-panel');
                if (panel.classList.contains('mobile-open')) {
                    setTimeout(() => toggleMobileNav(), 150);
                }
            }
        });

        // ── Login ──
        let _hintTimer = null;

        function _checkUsernameHint(val) {
            const hint = document.getElementById('login-hint');
            if (!hint) return;
            clearTimeout(_hintTimer);
            if (!val || val.length < 2) { hint.textContent = '';
                return; }
            if (val.length < 2) { hint.textContent = 'Minimum 2 characters';
                hint.style.color = 'var(--red)';
                return; }
            if (val.length > 40) { hint.textContent = 'Maximum 40 characters';
                hint.style.color = 'var(--red)';
                return; }
            if (!/^[A-Za-z0-9][A-Za-z0-9 _-]*$/.test(val)) {
                hint.textContent = 'Letters, numbers, spaces, - or _ only. Must start with letter/digit.';
                hint.style.color = 'var(--red)';
                return;
            }
            hint.textContent = '✓ Valid username format';
            hint.style.color = 'var(--mint)';
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

        async function doLogin() {
            const inp = document.getElementById('login-inp');
            const name = inp.value.trim();
            const errEl = document.getElementById('lerr');
            const sugg = document.getElementById('sugg');
            const btn = document.getElementById('login-go');
            const hint = document.getElementById('login-hint');

            inp.classList.remove('taken');
            errEl.className = 'login-err';
            errEl.style.display = 'none';
            sugg.style.display = 'none';

            if (!name) {
                errEl.textContent = 'Please enter a username.';
                errEl.style.display = 'block';
                return;
            }
            if (name.length < 2) {
                errEl.textContent = 'Username must be at least 2 characters.';
                errEl.style.display = 'block';
                return;
            }
            if (!/^[A-Za-z0-9][A-Za-z0-9 _-]*$/.test(name)) {
                errEl.textContent = 'Invalid characters. Use letters, numbers, spaces, - or _.';
                errEl.style.display = 'block';
                return;
            }

            btn.disabled = true;
            btn.textContent = 'Checking…';
            if (hint) { hint.textContent = 'Connecting to server…';
                hint.style.color = 'var(--muted)'; }

            try {
                const data = await fetchJSON(`${BASE}/login`, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ username: name })
                });

                if (data.taken) {
                    inp.classList.add('taken');
                    errEl.className = 'login-err taken-msg';
                    errEl.textContent = data.error || `"${name}" is already taken by another user.`;
                    errEl.style.display = 'block';
                    _suggs = _genSuggs(name);
                    const spans = sugg.querySelectorAll('span');
                    _suggs.forEach((s, i) => { if (spans[i]) spans[i].textContent = s; });
                    sugg.style.display = 'block';
                    if (hint) { hint.textContent = 'Choose a different name below:';
                        hint.style.color = 'var(--amber)'; }
                    btn.disabled = false;
                    btn.textContent = 'Enter Studio →';
                    return;
                }

                if (data.error) throw new Error(data.error);

                if (hint) {
                    hint.textContent = data.is_new ? '✓ Account created!' : `✓ Welcome back, ${data.username}!`;
                    hint.style.color = 'var(--mint)';
                }
                setTimeout(() => onLoginSuccess(data.username, data.runs || [], data.is_new), 300);

            } catch (e) {
                errEl.textContent = e.message || 'Connection error. Please try again.';
                errEl.style.display = 'block';
                if (hint) { hint.textContent = ''; }
                btn.disabled = false;
                btn.textContent = 'Enter Studio →';
            }
        }

        function _genSuggs(base) {
            const clean = base.replace(/[_-]?\d+$/, '').trim() || base;
            const yr = new Date().getFullYear() % 100;
            const rn = Math.floor(Math.random() * 89 + 10);
            return [
                clean + '_' + rn,
                clean + yr,
                clean + '_GIS'
            ];
        }

        function onLoginSuccess(username, runs, isNew) {
            const ov = document.getElementById('login-overlay');
            ov.style.transition = 'opacity .45s';
            ov.style.opacity = '0';
            setTimeout(() => { ov.style.display = 'none'; }, 460);

            document.getElementById('user-bar').style.display = 'flex';
            const uname = document.getElementById('uname');
            uname.textContent = username;
            uname.style.opacity = '0';
            setTimeout(() => {
                uname.style.transition = 'opacity .4s';
                uname.style.opacity = '1';
            }, 100);
            document.getElementById('uavatar').textContent = username.charAt(0).toUpperCase();
            renderHistory(runs);

            const toast = document.createElement('div');
            toast.textContent = isNew ? `Welcome, ${username}! Account created.` : `Welcome back, ${username}!`;
            toast.style.cssText = `position:fixed;bottom:20px;left:50%;transform:translateX(-50%) translateY(10px);
                background:var(--mint-g);color:white;padding:10px 22px;border-radius:30px;
                font-size:12px;font-weight:600;font-family:var(--sans);z-index:99000;
                box-shadow:0 4px 20px rgba(16,185,129,.45);opacity:0;
                transition:all .35s var(--spring)`;
            document.body.appendChild(toast);
            requestAnimationFrame(() => {
                toast.style.opacity = '1';
                toast.style.transform = 'translateX(-50%) translateY(0)';
            });
            setTimeout(() => {
                toast.style.opacity = '0';
                toast.style.transform = 'translateX(-50%) translateY(8px)';
                setTimeout(() => toast.remove(), 400);
            }, 3000);
        }

        async function doLogout() {
            if (!confirm('Sign out?')) return;
            try { await fetchJSON(`${BASE}/logout`, { method: 'POST' }); } catch {}
            document.getElementById('user-bar').style.display = 'none';
            const ov = document.getElementById('login-overlay');
            ov.style.opacity = '1';
            ov.style.display = 'flex';
            document.getElementById('login-inp').value = '';
            document.getElementById('lerr').style.display = 'none';
            document.getElementById('login-go').disabled = false;
            document.getElementById('login-go').textContent = 'Enter Studio →';
            renderHistory([]);
            setTimeout(() => document.getElementById('login-inp').focus(), 200);
        }

        // ── Check session ──
        (async function checkSession() {
            _applyTheme(_dark, false);
            _loadDemCatalog();
            try {
                const d = await fetchJSON(`${BASE}/me`);
                if (d.username) {
                    onLoginSuccess(d.username, d.runs || [], false);
                    return;
                }
            } catch {}
            document.getElementById('login-overlay').style.display = 'flex';
            setTimeout(() => document.getElementById('login-inp').focus(), 400);
        })();

        // ── History ──
        function toggleHist() {
            const d = document.getElementById('hist-drawer');
            const bg = document.getElementById('hist-bg');
            const open = d.classList.contains('open');
            d.classList.toggle('open', !open);
            bg.style.display = open ? 'none' : 'block';
            if (!open) {
                fetchJSON(`${BASE}/history`)
                    .then(d => { if (d && d.runs) renderHistory(d.runs); })
                    .catch(() => {});
            }
        }
        const MICONS = { A: '🌲', B: '🌲', C: '📍', D: '🗂', E: '✂️', F: '🏔', G: '📌', H: '🌲' };

        function renderHistory(runs) {
            const body = document.getElementById('hist-body');
            const hcnt = document.getElementById('hcnt');
            if (hcnt) hcnt.textContent = runs && runs.length ? ` (${runs.length})` : '';
            if (!runs || !runs.length) { body.innerHTML =
                '<div style="text-align:center;color:var(--muted);font-size:11px;padding:40px 20px;font-family:var(--mono)">No runs yet.</div>'; return; }
            body.innerHTML = [...runs].reverse().map((r, i) => {
                const ic = MICONS[r.module] || '📄';
                let ts = '—';
                if (r.timestamp) { const d = new Date(r.timestamp);
                    ts = d.toLocaleString(undefined, { month: 'short', day: 'numeric', hour: '2-digit',
                        minute: '2-digit' }); }
                const short = (r.run_id || '').slice(0, 8) + '…';
                const desc = r.description ?
                    `<div style="font-size:9px;color:var(--mint);font-family:var(--mono);margin-bottom:2px">${r.description}</div>` :
                    '';
                return `<div class="run-card" style="${i===0?'border-color:rgba(16,185,129,.35);':''}">
              <div class="run-card-top"><span class="run-mod">${ic} ${r.module}</span><span class="run-time">${ts}</span></div>
              <div style="font-size:9px;color:var(--muted);font-family:var(--mono);margin-bottom:2px">ID: ${short}</div>
              ${desc}
              <div class="run-actions">
                <button class="run-act" onclick="loadRunPrev('${r.run_id}')">🗺 Preview</button>
                <button class="run-act" onclick="triggerDL('${r.run_id}')">⬇ Download</button>
              </div></div>`;
            }).join('');
        }

        function triggerDL(rid) { const a = document.createElement('a');
            a.href = `${BASE}/download/${rid}`;
            a.download = '';
            a.click(); }

        function loadRunPrev(rid) {
            currentRunId = rid;
            const img = document.getElementById('out-img');
            img.style.opacity = '0';
            img.style.transform = 'scale(.97)';
            img.onload = () => {
                img.style.display = 'block';
                document.getElementById('empty-msg').style.display = 'none';
                img.style.transition = 'opacity .4s var(--ease),transform .4s var(--ease)';
                requestAnimationFrame(() => { img.style.opacity = '1';
                    img.style.transform = 'scale(1)'; });
                setTimeout(() => img.style.transition = '', 450);
                const vfb = document.getElementById('view-full-btn');
                if (vfb) vfb.style.display = 'block';
                showOverlayForRun();
            };
            img.src = `${BASE}/outputs/${rid}/output.png?t=${Date.now()}`;
            document.getElementById('dl-btn').href = `${BASE}/download/${rid}`;
            document.getElementById('dl-btn').style.display = 'inline-block';
            document.getElementById('dl-btn2').href = `${BASE}/download/${rid}`;
            switchPView('static');
            toggleHist();
            loadOSM(rid, null);
            fetchJSON(`${BASE}/geojson/${rid}`).then(gj => {
                if (gj) { currentGeoJSON = gj;
                    _rebuildOverlayLegend(activeModule, gj); }
            }).catch(() => {});
        }

        // ── Initial setup ──
        document.addEventListener('DOMContentLoaded', function() {
            const layer = document.getElementById('overlay-layer');
            layer.classList.add('visible');

            const img = document.getElementById('out-img');
            img.addEventListener('load', function() {
                if (currentRunId && this.style.display !== 'none') {
                    showOverlayForRun();
                }
            });

            const editBtn = document.getElementById('layout-edit-btn');
            if (editBtn) {
                document.getElementById('layout-hint').style.display = 'none';
            }

            _wireOvEvents();

            document.getElementById('layer-panel')?.classList.remove('visible');
            document.getElementById('align-toolbar')?.classList.remove('visible');

            const scaleSpan = document.getElementById('ov-scale')?.querySelector('.ov-drag-area span');
            if (scaleSpan && !scaleSpan.textContent.trim()) {
                scaleSpan.textContent = '0    500    1000 m';
            }

            const titleArea = document.getElementById('ov-title')?.querySelector('.ov-drag-area');
            if (titleArea && !titleArea.textContent.trim()) {
                titleArea.textContent = 'Map Title';
            }

            console.log('🌲 Elfak GIS Pro Studio — Layout Editor ready');
        });

        // ── North arrow colour picker + legend swatch wiring ────────
        (function(){
            const northEl=document.getElementById('ov-north');
            const colorInp=document.getElementById('ov-north-color');
            if(northEl&&colorInp){
                northEl.addEventListener('mouseenter',()=>{
                    if(document.getElementById('overlay-layer')?.classList.contains('editing'))
                        colorInp.style.display='block';
                });
                northEl.addEventListener('mouseleave',()=>colorInp.style.display='none');
                colorInp.addEventListener('pointerdown',e=>e.stopPropagation());
                colorInp.addEventListener('input',function(){
                    const c=this.value;
                    document.getElementById('ov-north-n')?.setAttribute('fill',c);
                    document.getElementById('ov-north-up')?.setAttribute('fill',c);
                    document.getElementById('ov-north-up')?.setAttribute('stroke',c);
                    document.getElementById('ov-north-dn')?.setAttribute('stroke',c);
                });
            }
            const legBody=document.getElementById('ov-legend-body');
            if(legBody){
                legBody.addEventListener('pointerdown',e=>{
                    if(!document.getElementById('overlay-layer')?.classList.contains('editing')) return;
                    const sw=e.target.closest('.ov-legend-swatch');
                    if(!sw) return;
                    e.stopPropagation();
                    if(!sw._picker){
                        const p=document.createElement('input');
                        p.type='color';
                        p.style.cssText='position:absolute;width:0;height:0;opacity:0;pointer-events:none;';
                        sw.appendChild(p); sw._picker=p;
                        p.addEventListener('input',()=>{
                            sw.style.background=p.value;
                            if(!sw.classList.contains('line')) sw.style.borderColor=p.value;
                        });
                    }
                    try{const ctx=document.createElement('canvas').getContext('2d');
                        ctx.fillStyle=sw.style.background||'#10b981';
                        sw._picker.value=ctx.fillStyle;}catch(_){}
                    sw._picker.click();
                });
            }
        })();
        console.log('🌲 Elfak GIS Pro Studio — Professional Layout Editor');
