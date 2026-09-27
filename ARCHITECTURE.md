# ElfakGISProStudio — Architecture

> Current state (2026-09): backend is the `elfakgis` package (Flask blueprints
> per concern + one module per processing group; heavy GIS imports load per
> request, boot stays light) plus `map_layout.py` (reference-style A4 renderer:
> neatline, lat/long graticule, title/area block, compass star, per-module
> legends, true-scale bar, projection block). Frontend is an Apple minimal
> single-page UI: slim `templates/index.html` shell + cacheable
> `static/css/app.css` and `static/js/*.js` (Composer tab with per-row legend
> editing via `/compose`, `/export_layout`, `/map_texts`).

## Stack

| Layer | Technology |
|---|---|
| Backend | Python 3, Flask |
| GIS Processing | GeoPandas, Shapely, Fiona |
| Map Rendering | Matplotlib (A4, 300 DPI) |
| DEM / Slope | rasterio, scipy.ndimage (optional) |
| Frontend | Vanilla JS, HTML5, CSS3 |
| Map Preview | Leaflet.js + leaflet-geoman |
| Auth | Session cookies, SHA-256 token, users.json |
| Storage | Local filesystem (outputs/, uploads/, dem_catalog/) |
| Deployment | Gunicorn (Procfile), Heroku/Render compatible |

---

## Directory Structure

```
ElfakGISProStudio/
│
├── app.py                  # Thin shim — `from elfakgis import app` (keeps
│                           # `gunicorn app:app` + `from app import app` working)
├── elfakgis/               # Backend package (split from the old monolith;
│   │                       # bodies verbatim, heavy GIS lazy-loads per-request)
│   ├── __init__.py         # create_app() factory + error handlers + bg threads
│   ├── lazy.py             # PEP 562 resolver: geopandas/matplotlib/rasterio
│   │                       # load on first pipeline request, never at boot
│   ├── core/               # config, store (users/progress/meta),
│   │                       # security (rate-limit/validation), pipeline (semaphore)
│   ├── geo/                # kmz, geom (canonical Group-E geometry core),
│   │                       # layout (A4 engine), render (render_map)
│   ├── groups/             # group_a … group_h (one module per pipeline group)
│   └── routes/             # pages, auth, maps, dem, inspect, pipeline (blueprints)
│
├── templates/
│   └── index.html          # Slim shell (~790 lines) — CSS/JS in static/
│
├── static/
│   ├── css/app.css         # Extracted stylesheet (cacheable, ?v= pinned)
│   └── js/                 # app.js (modules A–H) + dock.js + dock-resize.js,
│                           # all deferred, original execution order preserved
│
├── requirements.txt        # Python deps (unpinned)
├── Procfile                # gunicorn app:app
│
├── uploads/                # User-uploaded files (temp, per-request)
│   └── dem_cache/          # Downloaded DEM tiles cached here
│
├── outputs/                # Generated run output directories
│   └── <run_id>/
│       ├── output.png      # Rendered map (A4, 300 DPI)
│       ├── meta.json       # {forest_name, area_ha}
│       ├── output.zip      # Shapefiles + map
│       ├── output.kmz      # Google Earth file
│       └── *.shp / *.geojson
│
├── dem_catalog/
│   └── 44N/                # Nepal UTM Zone 44N DEM tiles
│
└── users.json              # Auth store {username: {token_hash, runs[]}}
```

---

## Backend (elfakgis package) Sections

> Why split: the old single-file backend imported geopandas + matplotlib +
> rasterio at startup, so every worker paid full GIS load before serving even
> `/` or `/robots.txt`. Now `create_app()` imports only Flask + light core;
> `geo/*` and `groups/*` import on first pipeline/compose request via
> `elfakgis/lazy.py`. Route paths and handler behavior are unchanged.

## Backend package (`elfakgis/`) — module map

```
elfakgis/
├── __init__.py      # create_app() factory: Flask app, 6 blueprints,
│                    # error handlers, security headers, 3 bg janitor threads.
│                    # Exposes `app` so `gunicorn app:app` keeps working.
├── lazy.py          # Symbol registry + resolver (attribute-style lazy access).
├── core/
│   ├── config.py    # Paths (uploads/outputs/dem_catalog), A4 + style constants.
│   ├── store.py     # _PROG/_PROG_LOCK (SSE progress), users.json store
│   │                # (_lu/_su/_register_user/…), _append_run, _login_required,
│   │                # _save_run_meta. Timestamps use stdlib datetime (no pandas).
│   ├── security.py  # _rate_limit, _cool_down, _safe_filename/_safe_path,
│   │                # _validate_username, _get_client_ip, output janitor.
│   └── pipeline.py  # _PIPELINE_SEM (MAX_PIPELINES=4) + _with_pipeline_sem.
├── geo/
│   ├── kmz.py       # _generate_run_id, _safe_runid, generate_kmz.
│   ├── geom.py      # Column detection, read_input, get_crs, repair/close/
│   │                # as-poly/enforce — canonical Group-E versions.
│   ├── layout.py    # FreeSpaceManager, safe-rect, label engine, defaults.
│   └── render.py    # _plot_*, UTM grid, north arrow/scale bar/slope table,
│                    # render_map() (standard + slope_mode). Imports map_layout.
├── groups/
│   ├── group_a.py   # A — Boundary Whole            → group_a()
│   ├── group_b.py   # B — Segmented Forest          → group_b()
│   ├── group_c.py   # C — Sample Plot Generator     → group_c()
│   ├── group_d.py   # D — Multi-Forest Complex      → group_d()
│   ├── group_e.py   # E — Polygon Subdivider (13 stages) → group_e()
│   ├── group_f.py   # F — Slope Analysis (raster)   → group_f()
│   ├── group_g.py   # G — Survey Point Generator    → group_g()
│   └── group_h.py   # H — Sample-Point GIS Maps     → process_group_h()
└── routes/                        # one Blueprint per file, exact same paths
    ├── pages.py     # /, /about, /map_editor/<run_id>, favicon/robots/sitemap
    ├── auth.py      # /login, /logout, /me, /history
    ├── maps.py      # /progress, /geojson, /compose, /map_texts,
    │                # /export_layout, /save_edit, /download, /outputs/…
    ├── dem.py       # /dem_catalog, /dem_fetch
    ├── inspect.py   # /zip_inspect
    └── pipeline.py  # /upload (dispatch A–F), /run_g, /run_h
```

Detail per group (uses, inputs, outputs, workings, links): [docs/GROUPS.md](docs/GROUPS.md).

### Dependency rule (downward only — keeps boot fast, splits clean)

```
routes → groups → geo → core          (never upward, never group→group)
```

- `core/*`, `config`, `geo/kmz.py`: import only stdlib + Flask → **always
  loaded at boot, ~0.1 s, no GIS libs.**
- `geo/*`, `groups/*`: heavy third-party imports (numpy/pandas/geopandas/
  shapely/matplotlib/rasterio) at module top, but these modules are imported
  **inside view functions, after validation** → first real GIS request pays the
  load once (cached in `sys.modules`); 404/400/validation paths never pay it.
- Inside `/upload`, only the **dispatched group's** module imports
  (A/B/C/D/E/F branch); `/run_g` loads only Group G, `/run_h` only Group H.
  One group works at a time.

### Request lifecycle (example: module-E upload)

```
Flask route (pipeline.py::upload, light)
 ├── file/format checks (400s without any GIS import)
 ├── from elfakgis.geo.geom import get_crs, read_input   ← shared GIS base loads
 ├── from elfakgis.geo.render import render_map
 ├── branch E: from elfakgis.groups.group_e import group_e  ← only Group E loads
 ├── group_e() streams _prog() → SSE /progress/<run_id>
 ├── render_map() → output.png, generate_kmz() → output.kmz
 └── _save_run_meta() + _append_run() → history
```

### 1. Config (`core/config.py`)
- `FIG_W=8.27, FIG_H=11.69, DPI=300` — A4 portrait
- `POLY_COLOR, POINT_COL, GRID_COL`, label/tick sizes — map style constants
- `UPLOAD/OUTPUT/USERS_FILE/DEM_CATALOG_DIR/DEM_CACHE_DIR` — **absolute paths
  anchored at the project root** (nearest ancestor with `app.py`+`templates/`).
  Required: Flask's `send_from_directory` resolves relative dirs against
  `app.root_path` (the `elfakgis/` package dir), while all writes use the
  process CWD — relative paths serve 404s for files that exist. Never make
  these relative again.

### 2. Auth & Security (`core/store.py`, `core/security.py`)
- `_rate_limit(limit, window)` — per-IP sliding window decorator
- `_safe_path(base, *parts)` — blocks directory traversal
- `_safe_runid(run_id)` — alphanumeric-only run ID validation (in `geo/kmz.py`)
- `_lu() / _su()` — read/write users.json with file lock
- `_USERS_LOCK` — threading.RLock for concurrent writes
- `_PIPELINE_SEM` — semaphore limits concurrent heavy GIS ops (default: 4)

### 3. Geometry Helpers (`geo/geom.py`)
- `_repair(geom)` — Shapely topology repair chain (Group-E canonical version)
- `_as_poly(geom)` — GeometryCollection → largest Polygon extraction
- `_enforce_poly_gdf(gdf)` — ensure GeoDataFrame has valid polygons only
- `_close_poly(coords)` — close open rings
- `_norm/_find_col/safe_col/normalize_order/read_input/get_crs` — column
  auto-detection (X/Y/order/forest/compartment alias sets) + file reading

### 4. Map Layout (`geo/layout.py`)
- `FreeSpaceManager` — tracks free canvas area, scores regions for overlay placement
- `_place_labels(ax, gdf, col, ...)` — 8-direction label placement with collision avoidance
- `get_default_layout_state()` — default overlay positions (%)
- `compute_safe_rect(layout_state, aspect)` — compute safe matplotlib axes rect
- `_rotated_bbox(rect, angle, center)` — rotation-aware bounding box

### 5. Map Renderer (`geo/render.py`, plus `map_layout.py`)
- `_add_north_arrow(fig, pos, size)` — baked N arrow (slope mode only)
- `_add_scale_bar(fig, ax, ...)` — baked scale bar (slope mode only)
- `_setup_utm_grid(ax, ...)` — UTM tick labels all 4 sides, dashed grid
- `_label_points_export(ax, pts_gdf, sn_col)` — SN labels with white stroke
- `render_map(path, ...)` — main entry point:
  - **Standard mode**: A4, 70% fill, UTM grid, blue boundary, red dots, no baked overlays
  - **Slope mode** (`slope_mode=True`): adds baked north arrow + scale bar + slope table

### 6. Processing Groups (`groups/group_*.py`)

| Group | Entry | Input | Output |
|---|---|---|---|
| A | `group_a()` | Boundary CSV/Excel + Survey table | Boundary polygon + survey points map |
| B | `group_b()` | Segmented boundary rows | Merged boundary map |
| C | `group_c()` | Boundary + plot size + grid | Sample-plot point map |
| D | `group_d()` | Multiple forest packages | Multi-forest coloured map |
| E | `group_e()` | Boundary + N + method + tolerance | Compartment subdivision map |
| F | `group_f()` | Boundary + DEM raster | Slope-class map (3 classes) |
| G | `group_g()` | Compartment SHP | Vertex/boundary/divider survey points |
| H | `process_group_h()` | Boundary + compartments + DEM + satellite + samples | Full map bundle ZIP |

Full per-group reference: [docs/GROUPS.md](docs/GROUPS.md).

### 7. KMZ Generator (`geo/kmz.py`)
- `generate_kmz(poly, line, pts, out_dir, run_id)` — builds KML from GeoDataFrames, zips to .kmz
- Handles Polygon, MultiPolygon, LineString, Point geometries

### 8. Routes (`routes/*.py`, 24 total — paths unchanged)

| Route | Method | File | Description |
|---|---|---|---|
| `/` | GET | pages | Serve index.html shell |
| `/about` | GET | pages | About page |
| `/map_editor/<run_id>` | GET | pages | Standalone map editor page (deep-link) |
| `/favicon.ico`, `/robots.txt`, `/sitemap.xml` | GET | pages | SEO/static |
| `/login` | POST | auth | Username auth, create/validate session |
| `/logout` | POST | auth | Clear session |
| `/me` | GET | auth | Return current session user |
| `/history` | GET | auth | Return user's run history |
| `/upload` | POST | pipeline | Dispatch modules A–F (one group per request) |
| `/run_g` | POST | pipeline | Group G only |
| `/run_h` | POST | pipeline | Group H only (validates 5 files first) |
| `/progress/<run_id>` | GET | maps | SSE stream of pipeline progress (0–100%) |
| `/geojson/<run_id>` | GET | maps | GeoJSON of run output for Leaflet |
| `/compose/<run_id>` | POST | maps | Re-render map with updated layout/title/labels |
| `/map_texts/<run_id>` | GET | maps | Editable texts for the Composer tab |
| `/export_layout` | POST | maps | Re-render A4 map with edited text (download) |
| `/save_edit/<run_id>` | POST | maps | Save vertex edits back to shapefiles |
| `/download/<run_id>` | GET | maps | Stream ZIP of all run outputs |
| `/outputs/<run_id>/<file>` | GET | maps | Serve output files (PNG, SHP, …) |
| `/dem_catalog` | GET | dem | List DEM tiles (local + GitHub) |
| `/dem_fetch` | POST | dem | Fetch a DEM tile into cache |
| `/zip_inspect` | POST | inspect | List archive contents |

### 9. Meta Helper
- `_save_run_meta(out_dir, forest_name, area_ha)` (`core/store.py`) — writes `meta.json` alongside every run

### 10. How to add / change things (future updates)

- **New group:** create `elfakgis/groups/group_<x>.py` with `group_<x>(…)` →
  add one branch in `pipeline.py::upload` (or a dedicated route like G/H) →
  document it in `docs/GROUPS.md`. Never import routes from groups.
- **New route:** add to the matching `routes/<concern>.py` blueprint; keep
  heavy imports **inside** the view after validation returns.
- **New shared helper:** put it in `geo/` (GIS) or `core/` (light); groups and
  routes both import downward.
- **Frontend change:** edit `static/js/*.js` / `static/css/app.css`, bump the
  `?v=` pin in `templates/index.html` so browsers fetch the new assets.

---

## Frontend (shell + static assets) Sections

`templates/index.html` is a slim shell (~790 lines, ~64 KB): head (SEO/meta,
`preconnect`, deferred vendor CDN scripts) + body markup + three deferred
app scripts. All CSS/JS was extracted byte-identical into cacheable files:

| Asset | Contains | Load |
|---|---|---|
| `static/css/app.css` | full app stylesheet | render-blocking stylesheet, `?v=` pinned |
| `static/js/app.js` | modules A–H forms, auth, history, progress, Leaflet preview, vertex edit, Composer, export | `defer` |
| `static/js/dock.js` | draggable dashboard sections | `defer` |
| `static/js/dock-resize.js` | resizable dashboard sections | `defer` |

All 9 scripts (6 vendor + 3 app) are `defer` in original execution order, so
first paint is unblocked while behavior is unchanged. After editing any asset,
bump the `?v=` pin in `templates/index.html`.

### Progress model (time-based, 75% cap)

The pipeline POST blocks until the run finishes, so the SSE stream can only
replay buffered `_prog` events *after* completion. To avoid a stuck-at-5% bar,
`static/js/app.js::ProgAnim` eases 5% → **75% cap** while awaiting the
response, using per-module learned durations (`localStorage elfak-est-<M>`,
seeded defaults A:25s…H:240s) with an ETA readout, then holds until the server
confirms — only then 100%. Hooks: run-button handler (A–E), `runF`, `runG`,
`runGroupH`; `finish()` records the wall time for the next estimate.

### Preview robustness

Preview images (`out-img`, H gallery) have `onerror` handlers
(`previewImgError`) — a failed PNG shows a message + retry button and a
ZIP-download link instead of failing silently.

### Layout
```
┌─────────────┬─────────────────────────────────────────────┐
│             │  Tab bar: 🖼 Map | 🌍 OSM | 🎨 Composer      │
│  Left Nav   ├─────────────────────────────────────────────┤
│  A B C D   │                                              │
│  E F G H   │     Map Canvas (canvas-wrap-static)          │
│             │                                              │
│  Module     │     ┌── overlay-layer ──────────────────┐   │
│  Forms      │     │  ov-north  ov-scale  ov-legend    │   │
│             │     │  ov-title  ov-area                 │   │
│  History    │     └───────────────────────────────────┘   │
│  Drawer     ├─────────────────────────────────────────────┤
│             │  [✥ Edit Layout]  [metrics]  [🔍 Full] [ZIP]│
└─────────────┴─────────────────────────────────────────────┘
```

### Key Global State Variables
```js
let currentRunId     = null;   // active run UUID
let currentGeoJSON   = null;   // GeoJSON of active run
let activeModule     = 'A';    // current group tab
let _layoutEditActive= false;  // overlay edit mode on/off
let leafMap          = null;   // Leaflet map instance
let leafLayers       = [];     // all Leaflet layers for current run
let activeTool       = null;   // 'vertex' | null
```

### Overlay System
Five overlay items float above the map image:

| ID | Type | Draggable | Resizable | Text Editable |
|---|---|---|---|---|
| `ov-north` | North arrow (SVG) | ✅ | ✅ 8 handles | ✅ N label, colour picker on hover |
| `ov-scale` | Scale bar + text | ✅ | ✅ | ✅ span contenteditable |
| `ov-legend` | Legend box | ✅ | ✅ | ✅ title + all labels |
| `ov-title` | Map title | ✅ | ✅ | ✅ |
| `ov-area` | Area display | ✅ | ✅ | ✅ |

State saved to `localStorage` key `ov-<run_id>` on every change.

### SSE Progress
`startSSE(runId)` opens an `EventSource` to `/progress/<run_id>`.
Server sends `data: {"pct": 45, "msg": "Processing boundary…"}` events.
Frontend updates progress bar and status text in real time.

---

## Security Model

| Concern | Mechanism |
|---|---|
| Path traversal | `_safe_path()` with `os.realpath()` comparison |
| Rate limiting | `_rate_limit` decorator, per-IP sliding window |
| Concurrent pipelines | `_PIPELINE_SEM` semaphore |
| Session fixation | `secrets.token_hex(32)`, stored as SHA-256 hash |
| Username abuse | Allowlist regex + blocklist check |
| File upload size | `MAX_CONTENT_LENGTH = 2 GB` |
| CORS | Response headers on all routes |

---

## Known Gaps

| Issue | Notes |
|---|---|
| `users.json` race condition | Read-modify-write not atomic under multi-worker Gunicorn |
| Unpinned requirements.txt | Shapely 2.x has breaking API changes vs 1.x |
| `_PROG` dict unbounded | Progress dict grows until cleanup (every 3600s, threshold 10000) |
| KMZ drops GeometryCollection | Silently skips features that aren't Polygon/Line/Point |
| Group C branch uses tabs | `pipeline.py` upload `module == "C"` block mixes tabs/spaces (verbatim from original — do not reindent blindly) |
