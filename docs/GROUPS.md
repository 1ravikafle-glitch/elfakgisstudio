# Elfak GIS Studio — Processing Groups Reference

> One-group-at-a-time execution model: every HTTP request dispatches to **exactly
> one** group module. Only that group's code + the shared GIS base load into the
> worker. Groups never call each other — orchestration lives in
> `elfakgis/routes/pipeline.py`. This file explains each group's use, inputs,
> outputs, working, and links. For the package layout and lazy-loading design,
> see [ARCHITECTURE.md](../ARCHITECTURE.md). For request flows, see
> [FLOW.md](../FLOW.md).

## Group index

| Group | Module | Route | Use it when… |
|---|---|---|---|
| A — Boundary Whole | `elfakgis/groups/group_a.py` | `POST /upload` (`module=A`) | Map one forest boundary + its survey points |
| B — Segmented Forest | `elfakgis/groups/group_b.py` | `POST /upload` (`module=B`) | Boundary arrives as multiple segment files |
| C — Sample Plot Generator | `elfakgis/groups/group_c.py` | `POST /upload` (`module=C`) | Generate a grid of sample plots inside a boundary |
| D — Multi-Forest Complex | `elfakgis/groups/group_d.py` | `POST /upload` (`module=D`) | Map several forest packages in one run |
| E — Polygon Subdivider | `elfakgis/groups/group_e.py` | `POST /upload` (`module=E`) | Split one polygon into N equal-area compartments (2–15) |
| F — Slope Analysis | `elfakgis/groups/group_f.py` | `POST /upload` (`module=F`) | Slope-class map from boundary + DEM |
| G — Survey Point Generator | `elfakgis/groups/group_g.py` | `POST /run_g` | Generate vertex / boundary / divider survey points |
| H — Sample-Point GIS Maps | `elfakgis/groups/group_h.py` | `POST /run_h` | Full map set from boundary + compartments + DEM + satellite + sample points |
| I — Thesis Locator Map | `elfakgis/groups/group_i.py` | `POST /run_thesis` (+ `GET /thesis_options`) | 4-panel A4-landscape locator: Nepal · province · district · study area |

Every group run produces a run directory `outputs/<run_id>/` containing:

```
outputs/<run_id>/
├── output.png   # A4 survey map, 300 DPI (rendered by geo/render.py)
├── meta.json    # {forest_name, area_ha, module, ...} (core/store.py::_save_run_meta)
├── output.kmz   # Google Earth export (geo/kmz.py::generate_kmz)
└── *.shp/.dbf/.shx/.prj/.cpg  # boundary / line / point shapefiles
```

Progress for every group is streamed over `GET /progress/<run_id>` (SSE) via
`core/store.py::_prog`, and the run is appended to the user's history
(`_append_run`) for the History drawer + `/map_editor/<run_id>` deep link.

---

## A — Boundary Whole (`group_a`)

**Entry:** `group_a(df, forest, crs, out, mapping=None)` → `(polygons, lines, points)`

- **Use:** the standard case — one named forest, boundary coordinates in
  CSV/Excel, optional survey-point table.
- **Input:** `df` — boundary dataframe (auto-detected X/Y/order/forest columns
  via `geo/geom.py`: `_XA/_YA/_OA/_FA` alias sets + `read_input`); `forest` —
  forest name; `crs` — e.g. `EPSG:32644/32645`; `out` — run directory.
- **How it works:** normalize column order → build + repair polygons
  (`safe_polygon`, `_repair`) → enforce polygon-only GeoDataFrame
  (`_enforce_poly_gdf`) → compute `Area_ha` → return three GeoDataFrames.
- **Output:** boundary polygon(s), boundary line(s), survey point(s) shapefiles
  + A4 map + KMZ.
- **Links:** helpers from `elfakgis/geo/geom.py`; rendering by
  `elfakgis/geo/render.py::render_map`.

## B — Segmented Forest (`group_b`)

**Entry:** `group_b(df, crs, out, mapping=None)` → `(polygons, lines, points)`

- **Use:** the boundary is supplied as multiple segment rows/files that must be
  merged into one mapped forest (ZIP uploads are rejected for A/B/D — CSV/Excel
  only; the route returns 400 otherwise).
- **Input / Output:** same shapes as A.
- **How it works:** same normalize → repair → enforce pipeline as A, tuned for
  multi-segment inputs.
- **Links:** `geo/geom.py` helpers; `render_map` for the preview.

## C — Sample Plot Generator (`group_c`)

**Entry:** `group_c(file, crs, w, h, rows, cols, out, mode, mapping=None, base_name="boundary", run_id=None)` → `(polygons, lines, sample_points)`

- **Use:** lay out a regular grid of sample plots (w × h m, rows × cols) inside
  a boundary for field inventory.
- **Input:** boundary file (CSV/Excel **or** ZIP shapefile) + plot dimensions +
  grid size + mode.
- **How it works:** load boundary (`read_input` or ZIP loader) → generate grid
  points clipped to the polygon → label `SN` order → return layers with
  `lc_out = "SN"`.
- **Output:** boundary layers + `*_point.shp` sample-plot points, A4 map, KMZ.
- **Links:** `geo/geom.py`; note the file carries its own stdlib imports
  (`tempfile`, …) for its temp workspace.

## D — Multi-Forest Complex (`group_d`)

**Entry:** `group_d(df, crs, out, mapping=None, mode="A")` → `(polygons, lines, points)`

- **Use:** one run covering several forest packages/blocks.
- **Input:** dataframe with a forest/block column (`d_mode` selects handling).
- **How it works:** per-forest split (`_save_fl` helper) → shared normalize →
  repair → enforce pipeline → merged coloured output.
- **Output:** multi-polygon layers + A4 map + KMZ.
- **Links:** `geo/geom.py`; `render_map`.

## E — Polygon Subdivider (`group_e`)

**Entry:** `group_e(file_or_df, crs, out, mapping, e_mode, n_compartments, is_zip, fcol, area_tol_ha, method, run_id)` → `(polygons, lines, points, survey_points)`

- **Use:** divide one forest polygon into N (2–15) equal-area compartments with
  a configurable area tolerance (default 0.3 ha).
- **Input:** boundary (dataframe or ZIP) + `n_compartments` + `method`
  (`bisect` | `voronoi` | `grid` | `bisect-area`) + tolerance.
- **How it works (13 stages inside the module):**
  1. unit-aware snap tolerance (`_snap_tol`, `_GEOM_TOL_FRAC`)
  2. low-level repair (`_force_valid`, `_close_poly`, `_as_poly`, `_repair` —
     these are the **canonical** versions also exported by `geo/geom.py`)
  3. guaranteed-containment hard clip (`_hard_clip`)
  4. topology-safe overlay (`safe_overlay`)
  5. shape descriptors (`_elong`, `_asp`, `_pa_angle`)
  6. area-balance refinement (`_enforce_area_tolerance`)
  7. clip-to-original + gap fill (`_clip_to_original`)
  8. exact-count enforcement (`_ensure_count`)
  9. final hard-clip pass (`_final_clip_pass`)
  10. validation (`_validate_subdivision`)
  11. four subdivision methods (`_bisect`, `_subdivide_ba`,
      `_subdivide_voronoi`, `_subdivide_grid`) via `_subdivide`
  12. output helpers (`_extract_div_pts`, `_save_compartments`)
  13. input loaders (`_df_to_poly`, `_load_polys_from_zip`)
- **Output:** compartment polygons (`Comp_ID`, `Area_ha`), divider points,
  survey points, A4 map (module="E" legend), KMZ.
- **Links:** needs `UPLOAD` (config) + `_prog` (store) + geom core. The route
  re-renders with `render_map(..., module="E")` and returns early with its own
  JSON (does not use the shared tail).
- **Further division:** each `_subdivide_*` method is self-contained — a new
  method only needs to satisfy `_validate_subdivision`.

## F — Slope Analysis (`group_f`)

**Entry:** `group_f(boundary_file, dem_file, crs, out, mapping=None, boundary_is_zip, forest_name, f_mode, comp_col_name, field_area_ha, run_id)` → `(summary_table, slope_polygons, boundary_lines, f_mode, per_group)`

- **Use:** slope-class map for a forest from its boundary + a DEM raster.
- **Input:** boundary (CSV/Excel or ZIP) + DEM (uploaded `.tif`, catalog path
  via `dem_catalog_path`, or cached tile via `dem_cache_key`) + optional field
  area (ha) for recalibration.
- **How it works:** load boundary (`_bnd_from_df` / `_bnd_from_zip`) → 20 %
  buffered rectangular DEM clip → slope compute → reclassify (gentle 0–19°,
  moderate 19–31°, steep >31°) → raster-to-polygon (`rio_shapes`) → dissolve by
  class → clip to compartments (no double-counting buffer) → optional
  field-area recalibration → Excel-ready summary.
- **Output:** slope-class polygons with `Slope_Range`/`Area_ha`, clipped slope
  rasters, A4 map with baked north arrow + scale bar + slope table
  (`slope_mode=True`), KMZ.
- **Links:** rasterio/scipy (guarded `_HAS_RASTERIO` block at module top —
  loads only when Group F runs); `geo/geom.py`; `UPLOAD` + `_prog`.
- **Linked with:** DEM catalog routes (`/dem_catalog`, `/dem_fetch`) supply the
  `dem_catalog_path`; `DEM_CACHE_DIR` (`uploads/dem_cache`) holds downloads.

## G — Survey Point Generator (`group_g`)

**Entry:** `group_g(file_storage, dem_zone, comp_col_name, spacing, out_dir, run_id, target_shp=None, base_name="ForestPoints")` → `(points_df, valid_shape_gdf, polygon_gdf, summary)`

- **Use:** generate field survey points from compartment polygons.
- **Input:** compartment shapefile (or ZIP) + UTM zone + compartment column +
  boundary spacing (m).
- **How it works:** vertex points (`_g_vertex_points`) + evenly spaced boundary
  points (`_g_boundary_points`) + divider/compartment-intersection points
  (`_g_divider_points`) → dedup merge (`_g_merge_dedup`) → SN assignment
  (`_g_assign_ids`) → shapefile/Excel export (`_g_export`) → A4 preview
  (`_g_preview`, which uses `geo/render.py::render_map`).
- **Output:** `ForestPoints` point shapefile + Excel, preview map, KMZ, summary
  (`area_ha`, counts) in the JSON response.
- **Links:** `render_map` (render); `_prog` (store). Also used by `/upload`
  (`_extract_shapefile_basename_from_zip` for ZIP run naming) — the only
  cross-use of a group helper outside its own route.
- **Route:** dedicated `POST /run_g` (own module, own file input) — Group G
  loads **only** on this route.

## H — Sample-Point GIS Maps (`process_group_h`)

**Entry:** `process_group_h(boundary_zip, compartments_zip, dem_file, satellite_file, sample_points_file, survey_points_file, crs, out_dir, run_id)` → `(zip_path, out_dir)`

- **Use:** the full bundle — boundary + compartments + DEM + satellite imagery +
  sample points (+ optional survey points) → complete map set.
- **Input:** five required files + one optional (`survey_points`).
- **How it works:** validate ZIP components (`_validate_zip_components`) →
  extract shapefiles (`_extract_shp`) → read points (`_read_points`) → clip to
  boundary (`_clip_to_boundary`) → slope raster from DEM
  (`_compute_slope_raster`) → raster-to-polygons (`_raster_to_polygons`) →
  multi-panel figure (`create_figure`, compact slope table
  `_draw_slope_table_compact`).
- **Output:** bundled ZIP of all maps/shapefiles + `out_dir` served under
  `/outputs/<run_id>/`.
- **Links:** `DPI`/`OUTPUT` (config), render slope helpers, `_prog`. Heaviest
  group (rasterio + imagery) — loads **only** on `POST /run_h`.
- **Route:** dedicated `POST /run_h` with all-five-files validation before any
  GIS import runs.

---

## I — Thesis Locator Map (`group_i`)

**Entry:** `group_thesis(file_storage, province_sel, district_sel, crs, out_dir, mapping=None, cf_name="Study Area", run_id=None)` → summary dict

- **Use:** thesis location figure — Nepal → province → district → study area
  (A4 landscape, one N-arrow + alternating scale bar per panel, legend in BR).
- **Input:** study boundary (SHP-ZIP polygon **or** CSV/Excel X/Y/Order, same
  auto-detection as A–E) + manual `province` (code 1–7 or official name:
  Koshi, Madhesh, Bagmati, Gandaki, Lumbini, Karnali, Sudurpashchim) +
  `district` (dropdown spelling from the base file, e.g. MAKAWANPUR).
- **Base:** `data/nepal/local_unit.shp` (bundled; 777 local units with
  `DISTRICT` + `STATE_CODE`; geographic Everest datum). All layers are
  reprojected to the run's UTM zone (the app's 44N/45N/… setting), so every
  panel is metric and scale bars are exact. A yellow star marks the study
  centroid in the Nepal / province / district panels. District-vs-province
  mismatch returns 400.
- **Composer:** thesis runs support the Composer tab — `/map_texts` returns
  the 4 legend rows (study, district, province, Nepal) plus the 4 panel
  headings (`Map of Nepal / … Province / … District / Study Area`);
  `/compose` and `/export_layout` re-render the 4-panel figure with edited
  title, legend title, legend rows and panel headings (`panel_titles`;
  `comp-module` option `I`). The true study boundary (lavender fill +
  navy outline, no markers) is overlaid in the Nepal, province and
  district panels; solid connector arrows start on the nearest point of
  the true boundary and land with heads on the study panel's frame
  corners.
- **How it works:** match province/district → reproject study to base CRS
  (Everest MUTM metres) → dissolve province/district polygons → render with
  `map_layout.render_thesis_map` → save `thesis_study.shp` + KMZ.
- **Output:** `output.png` (A4 landscape, 300 DPI), `thesis_study.shp`,
  `output.kmz`, `meta.json` (`module="I"`, province/district).
- **Routes:** `GET /thesis_options` (dropdown data; light, no GIS upload
  needed) + `POST /run_thesis` (dedicated route like G/H — only Group I loads).
- **Links:** `core/config.py` (`NEPAL_WARDS_SHP`, `PROVINCE_NAMES`);
  `map_layout.render_thesis_map`; `geo/geom.py` (`read_input`, `safe_polygon`).

---

## Shared services (used by groups, live in `geo/` + `core/`)

| Service | Module | What it does | Used by |
|---|---|---|---|
| `render_map` | `geo/render.py` | A4 300 DPI map: UTM grid, blue boundary, red dots, SN labels, slope mode (baked arrow/bar/table) | A–G routes, compose/export |
| Layout engine | `geo/layout.py` | `FreeSpaceManager`, safe-rect, collision-free labels, default overlay slots | render, compose |
| Geometry core | `geo/geom.py` | column detection, `read_input`, CRS, repair/close/as-poly/enforce | all groups |
| KMZ export | `geo/kmz.py` | `generate_kmz`, `_generate_run_id`, `_safe_runid` | upload, run_g |
| Progress/history | `core/store.py` | `_prog` (SSE), `_save_run_meta`, `_append_run`, users | all routes |
| Guards | `core/security.py`, `core/pipeline.py` | rate-limit, cooldown, semaphore (`MAX_PIPELINES=4`), validation | all routes |

## Group linkage map (orchestration only — no group→group calls)

```
POST /upload ──┬── module=A → group_a ──┐
               ├── module=B → group_b   │
               ├── module=C → group_c   ├─► render_map + generate_kmz (shared tail)
               ├── module=D → group_d   │
               ├── module=E → group_e ──┘  (own early return + re-render)
               └── module=F → group_f ──► render_map(slope_mode) + KMZ (own early return)
POST /run_g ──► group_g (+ render_map via _g_preview) + KMZ
POST /run_h ──► process_group_h (validates 5 files first)
POST /run_thesis ──► group_thesis (+ render_thesis_map) + KMZ
GET /thesis_options ──► dropdown data (provinces + districts per province)
GET /compose, POST /export_layout, POST /save_edit ──► render_map re-render
```

## Splitting further (when / how)

- A group file is the unit of split: e.g. `group_e.py` stages 1–13 can move to
  `elfakgis/groups/e/` sub-modules as long as `group_e()` keeps its signature
  (routes import the entry name, not internals).
- A new group = new `elfakgis/groups/group_<x>.py` + one branch in
  `upload()` (or a dedicated route like G/H) + one row in the table above.
- Never import `routes` from `groups`/`geo` (dependency rule:
  routes → groups → geo → core, downward only).
