"""ElfakGISProStudio — map data & compose routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import (Flask, request, jsonify, send_file, send_from_directory,
                   render_template, session, Response, stream_with_context, abort, g)
from elfakgis.core.config import *
from elfakgis.core.store import (_prog, _PROG, _PROG_LOCK, _save_run_meta, _append_run,
    _require_login, _login_required, _lu, _su, _register_user, _login_existing, _logout_user)
from elfakgis.core.security import _rate_limit, _cool_down, _safe_filename, _safe_path, _validate_username, _get_client_ip
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.geo.kmz import _generate_run_id, _safe_runid

log = logging.getLogger("elfakgis")

from flask import Blueprint
maps_bp = Blueprint('maps_bp', __name__)
@maps_bp.route("/progress/<run_id>")
def progress_stream(run_id):
    from elfakgis.core.store import _prog_replay
    try:
        run_id = _safe_runid(run_id)
    except:
        pass
    def gen():
        # Replay persisted events first (survives worker restarts), then
        # live-tail memory. Dedup by value: replay already merges both.
        # A replayed 100% ends the stream immediately (nothing live to wait).
        sent = set()
        finished = False
        try:
            for m in _prog_replay(run_id):
                sent.add(m)
                try:
                    if (json.loads(m) or {}).get("pct", 0) >= 100:
                        finished = True
                except Exception:
                    pass
                yield f"data: {m}\n\n"
        except Exception:
            pass
        if finished:
            return
        last_heartbeat = time.time()
        while True:
            msgs = _PROG.get(run_id, [])
            new = [m for m in msgs if m not in sent]
            if new:
                for m in new:
                    sent.add(m)
                    yield f"data: {m}\n\n"
                try:
                    if json.loads(msgs[-1]).get("pct", 0) >= 100:
                        return
                except:
                    pass
            else:
                now = time.time()
                if now - last_heartbeat > 3:
                    yield f": heartbeat\n\n"
                    last_heartbeat = now
            time.sleep(0.1)
    return Response(
        stream_with_context(gen()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-store",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "Transfer-Encoding": "chunked",
        }
    )

@maps_bp.route("/result/<run_id>")
def job_result(run_id):
    """Poll the final payload of a background pipeline job.

    200 {"done": false, "boot": <worker>} while running; {"done": true,
    "status": <code>, "payload": {...}} when finished (memory first, then
    the on-disk result mirror, so restarted workers still answer)."""
    from elfakgis.core.store import _bg_get, BOOT_ID
    try:
        run_id = _safe_runid(run_id)
    except Exception:
        return jsonify({"done": False, "boot": BOOT_ID}), 200
    r = _bg_get(run_id)
    if r:
        return jsonify({"done": bool(r.get("done")),
                        "status": r.get("status"),
                        "payload": r.get("payload"),
                        "boot": BOOT_ID}), 200
    # Disk mirror (job finished before a restart).
    try:
        from elfakgis.core.config import OUTPUT
        p = os.path.join(_safe_path(OUTPUT, run_id), "result.json")
        if os.path.exists(p):
            with open(p, encoding="utf-8") as f:
                disk = json.load(f)
            return jsonify({"done": True, "status": disk.get("status"),
                            "payload": disk.get("payload"),
                            "boot": BOOT_ID}), 200
    except Exception:
        pass
    return jsonify({"done": False, "boot": BOOT_ID}), 200

@maps_bp.route("/geojson/<run_id>")
def get_geojson(run_id):
    from elfakgis.core.config import OUTPUT
    run_id = _safe_runid(run_id)
    folder = _safe_path(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"type": "FeatureCollection", "features": []}), 200
    shps = []
    for r, _, fs in os.walk(folder):
        for f in fs:
            if not f.endswith(".shp"): continue
            fl = f.lower()
            if "polygon" in fl or "forestpoints" in fl or "rtp" in fl or "point" in fl or "line" in fl:
                shps.append(os.path.join(r, f))
    if not shps:
        shps = [os.path.join(r, f) for r, _, fs in os.walk(folder)
                for f in fs if f.endswith(".shp")]
    gdfs = []
    import geopandas as gpd
    import pandas as pd
    for shp in shps:
        try:
            g = gpd.read_file(shp)
            from elfakgis.geo.geom import restore_shp_cols
            g = restore_shp_cols(g)
            if g.crs is not None: g = g.to_crs("EPSG:4326")
            keep = [c for c in g.columns if c in
                    ("Comp_ID","Forest","Class","Slope_Range","Area_ha",
                     "Point_ID","Point_Type","Source","Compartments",
                     "Easting","Northing","geometry")]
            g = g[[c for c in keep if c in g.columns]]
            gdfs.append(g)
        except: pass
    if not gdfs:
        return jsonify({"type": "FeatureCollection", "features": []}), 200
    combined = gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs="EPSG:4326")
    return Response(combined.to_json(), mimetype="application/json")

@maps_bp.route("/compose/<run_id>", methods=["POST"])
def compose_map(run_id):
    from elfakgis.core.config import OUTPUT
    run_id = _safe_runid(run_id)
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"error": "Run not found"}), 404
    # Heavy GIS binds only for real runs (keeps 404s light).
    from elfakgis.geo.layout import get_default_layout_state
    from elfakgis.geo.render import render_map
    try:
        data = request.get_json(silent=True) or {}
        if _is_thesis_run(folder, data):
            return _compose_thesis(folder, data, run_id)
        layout_state = data.get("layout_state")
        if not layout_state:
            layout_state = get_default_layout_state()

        pg, line_gdf, pts_gdf = _load_run_layers(folder)
        if pg is None:
            return jsonify({"error": "No shapefiles found."}), 400

        pp = os.path.join(folder, "output.png")
        title = (data.get("title") or "").strip() or None
        subtitle = (data.get("subtitle") or "").strip() or None
        legend_title = (data.get("legend_title") or "").strip() or "Legend"
        area_text = (data.get("area_text") or "").strip() or None
        legend_labels = _parse_legend_labels(data.get("legend_labels"))
        show_point_labels = (data.get("point_labels", "auto") != "hide")
        label_col = (data.get("label_col") or "").strip() or None
        module = (data.get("module") or "").strip() or None
        if label_col is None:
            label_col = "Comp_ID" if "Comp_ID" in pg.columns else None
        point_label_col = None
        if pts_gdf is not None and not pts_gdf.empty:
            for cand in ("SN", "sn", "Order", "Point_ID", "ID", "point_id"):
                if cand in pts_gdf.columns:
                    point_label_col = cand
                    break
            if point_label_col is None and label_col in pts_gdf.columns:
                point_label_col = label_col
        render_map(pp, poly_gdf=pg, line_gdf=line_gdf, pts_gdf=pts_gdf,
                   label_col=label_col, point_label_col=point_label_col,
                   title=title, subtitle=subtitle, module=module,
                   legend_title=legend_title, area_text=area_text,
                   legend_labels=legend_labels,
                   show_point_labels=show_point_labels)
        return jsonify({"ok": True, "png": f"/outputs/{run_id}/output.png?t={uuid.uuid4().hex[:8]}"})
    except Exception as e:
        return jsonify({"error": f"Compose error: {e}"}), 500


def _load_run_layers(folder):
    """Reload a run's saved layers: (polygons, lines, points).

    Points = first non-empty *_point.shp (survey/SN dots); lines =
    first *_line.shp. Returns (pg or None, line or None, pts or None).
    """
    import geopandas as gpd
    import pandas as pd

    def _read(paths):
        from elfakgis.geo.geom import restore_shp_cols
        gdfs, crs0 = [], None
        for p in paths:
            try:
                g = restore_shp_cols(gpd.read_file(p))
                crs0 = crs0 or g.crs
                gdfs.append(g)
            except Exception:
                pass
        if not gdfs:
            return None
        return gpd.GeoDataFrame(pd.concat(gdfs, ignore_index=True), crs=crs0)

    walked = [os.path.join(r, f) for r, _, fs in os.walk(folder) for f in fs]
    poly = _read([p for p in walked if p.endswith("_polygon.shp")])
    if poly is None or poly.empty:
        return None, None, None
    line = _read([p for p in walked if p.endswith("_line.shp")])
    pts = _read([p for p in walked
                 if p.endswith(".shp") and "point" in os.path.basename(p).lower()])
    if pts is not None and pts.empty:
        pts = None
    return poly, line, pts

def _read_meta(folder):
    try:
        with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _is_thesis_run(folder, data):
    mod = ((data or {}).get("module") or "").strip().upper()
    if mod == "I":
        return True
    if not mod:
        return (_read_meta(folder).get("module") or "").upper() == "I"
    return False


def _parse_panel_titles(raw):
    """Normalize Composer panel-title overrides to a 4-list (or None)."""
    items = _parse_legend_labels(raw)
    if not items:
        return None
    items = [str(s).strip() for s in items][:4]
    while len(items) < 4:
        items.append("")
    return items if any(items) else None


def _compose_thesis(folder, data, run_id):
    """Re-render a Group I thesis map with Composer-edited texts."""
    import geopandas as gpd
    from elfakgis.groups.group_i import render_thesis_run
    meta = _read_meta(folder)
    shp = os.path.join(folder, "thesis_study.shp")
    if not os.path.exists(shp):
        return jsonify({"error": "Thesis study shapefile missing."}), 400
    study = gpd.read_file(shp)
    if study.empty:
        return jsonify({"error": "Thesis study shapefile is empty."}), 400
    title = (data.get("title") or "").strip() or None
    legend_title = (data.get("legend_title") or "").strip() or "Legend"
    legend_labels = _parse_legend_labels(data.get("legend_labels"))
    panel_titles = _parse_panel_titles(data.get("panel_titles"))
    cf_name = meta.get("forest_name") or "Study Area"
    render_thesis_run(
        os.path.join(folder, "output.png"),
        meta.get("province") or "", meta.get("district") or "",
        study, cf_name, title=title,
        legend_labels=legend_labels, legend_title=legend_title,
        panel_titles=panel_titles, run_id=run_id)
    return jsonify({"ok": True,
                    "png": f"/outputs/{run_id}/output.png?t={uuid.uuid4().hex[:8]}"})


def _parse_legend_labels(raw):
    """Accept a JSON list or newline-separated string of legend label
    overrides. Returns a list (may be empty = all auto)."""
    if not raw:
        return None
    if isinstance(raw, str):
        items = [s.strip() for s in raw.replace("\r", "\n").split("\n")]
    else:
        try:
            items = [str(s).strip() for s in raw]
        except TypeError:
            return None
    return items if any(items) else None


@maps_bp.route("/map_texts/<run_id>")
def map_texts(run_id):
    """Return every editable text currently on the run's map: title,
    subtitle, area line, legend title and one entry per legend row.
    Powers the Composer's per-row editing (blank = keep auto)."""
    from elfakgis.core.config import OUTPUT
    run_id = _safe_runid(run_id)
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"error": "Run not found"}), 404
    # map_layout pulls matplotlib — bind only for real runs.
    from map_layout import MODULE_SUBTITLES, legend_spec
    try:
        module = (request.args.get("module") or "").strip().upper() or None
        meta = {}
        try:
            with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
        if module is None:
            module = (meta.get("module") or "A").upper()
        if module == "I":
            from elfakgis.groups.group_i import thesis_panel_titles
            cf = meta.get("forest_name") or "Study Area"
            dist = meta.get("district") or "District"
            prov = meta.get("province") or "Province"
            area_ha = meta.get("area_ha")
            return jsonify({
                "ok": True,
                "module": "I",
                "title": meta.get("title") or f"Location Map of {cf}",
                "subtitle": MODULE_SUBTITLES.get("I", ""),
                "area": f"Area: {area_ha:.2f} ha" if area_ha is not None else "",
                "legend_title": meta.get("legend_title") or "Legend",
                "rows": [cf, f"{dist} District", f"{prov} Province", "Nepal"],
                "panel_titles": thesis_panel_titles(prov, dist),
                "note": "Thesis map: 4 legend rows (study, district, "
                        "province, Nepal) + 4 panel headings. "
                        "Blank = keep auto.",
            })
        pg, _line, pts = _load_run_layers(folder)
        label_col = None
        if pg is not None and not pg.empty:
            for cand in ("Comp_ID", "Forest", "Slope_Range", "Description"):
                if cand in pg.columns:
                    label_col = cand
                    break
        slope_areas = None
        if pg is not None and not pg.empty:
            for cand in ("Slope_Range", "Slope", "Description"):
                if cand in pg.columns:
                    try:
                        if "Area_ha" in pg.columns:
                            slope_areas = {r: float(pg[pg[cand] == r]["Area_ha"].sum())
                                           for r in pg[cand].unique()}
                        else:
                            slope_areas = {r: float(pg[pg[cand] == r].geometry.area.sum() / 10000)
                                           for r in pg[cand].unique()}
                    except Exception:
                        slope_areas = None
                    break
        spec = legend_spec(pg, pts, label_col, module, slope_areas)
        rows = [label for _, _, label in spec["rows"]]
        area_ha = meta.get("area_ha")
        if area_ha is None and pg is not None and not pg.empty:
            try:
                area_ha = float(pg.geometry.area.sum() / 10000)
            except Exception:
                area_ha = None
        return jsonify({
            "ok": True,
            "module": module,
            "title": meta.get("title") or meta.get("forest_name") or "",
            "subtitle": MODULE_SUBTITLES.get(module, ""),
            "area": f"Area: {area_ha:.2f} ha" if area_ha is not None else "",
            "legend_title": meta.get("legend_title") or "Legend",
            "rows": rows,
            "note": "Scale, coordinates and projection are measured from "
                    "your data and always stay automatic.",
        })
    except Exception as e:
        return jsonify({"error": f"texts error: {e}"}), 500


@maps_bp.route("/export_layout", methods=["POST"])
def export_layout():
    """Re-render the A4 map with user-edited text and return it as download."""
    from elfakgis.core.config import OUTPUT
    data = request.get_json(silent=True) or {}
    run_id = _safe_runid(data.get("run_id", ""))
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"error": "Run not found"}), 404
    # Heavy renderer binds only for real runs (keeps 404s light).
    from elfakgis.geo.render import render_map
    try:
        if _is_thesis_run(folder, data):
            resp = _compose_thesis(folder, data, run_id)
            if isinstance(resp, tuple):
                return resp
            pp = os.path.join(folder, "output.png")
            return send_file(pp, as_attachment=True,
                             download_name=f"elfak_thesis_{run_id}.png",
                             mimetype="image/png")
        pg, line_gdf, pts_gdf = _load_run_layers(folder)
        if pg is None:
            return jsonify({"error": "No shapefiles found."}), 400
        title = (data.get("title") or "").strip() or None
        subtitle = (data.get("subtitle") or "").strip() or None
        legend_title = (data.get("legend_title") or "").strip() or "Legend"
        area_text = (data.get("area_text") or "").strip() or None
        legend_labels = _parse_legend_labels(data.get("legend_labels"))
        show_point_labels = (data.get("point_labels", "auto") != "hide")
        label_col = (data.get("label_col") or "").strip() or None
        module = (data.get("module") or "").strip() or None
        if label_col is None:
            label_col = "Comp_ID" if "Comp_ID" in pg.columns else None
        point_label_col = None
        if pts_gdf is not None and not pts_gdf.empty:
            for cand in ("SN", "sn", "Order", "Point_ID", "ID", "point_id"):
                if cand in pts_gdf.columns:
                    point_label_col = cand
                    break
            if point_label_col is None and label_col in pts_gdf.columns:
                point_label_col = label_col
        pp = os.path.join(folder, "output.png")
        render_map(pp, poly_gdf=pg, line_gdf=line_gdf, pts_gdf=pts_gdf,
                   label_col=label_col, point_label_col=point_label_col,
                   title=title, subtitle=subtitle, module=module,
                   legend_title=legend_title, area_text=area_text,
                   legend_labels=legend_labels,
                   show_point_labels=show_point_labels)
        return send_file(pp, as_attachment=True,
                         download_name=f"elfak_map_{run_id}.png",
                         mimetype="image/png")
    except Exception as e:
        return jsonify({"error": f"Export error: {e}"}), 500
@maps_bp.route("/save_edit/<run_id>", methods=["POST"])
def save_edit(run_id):
    from elfakgis.core.config import OUTPUT
    run_id = _safe_runid(run_id)
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"error": "Run not found"}), 404
    try:
        data = request.get_json(silent=True) or {}
        geojson = data.get("geojson")
        if not geojson:
            return jsonify({"error": "No GeoJSON provided."}), 400
        # Heavy GIS binds only for validated edits (keeps 404/400s light).
        import geopandas as gpd
        from elfakgis.geo.geom import _close_poly, _repair
        from elfakgis.geo.layout import get_default_layout_state
        from elfakgis.geo.render import render_map

        # Convert to GeoDataFrame
        gdf_new = gpd.GeoDataFrame.from_features(geojson.get("features", []), crs="EPSG:4326")

        # Find original CRS from existing polygon shapefile
        poly_shps = [os.path.join(r, f) for r, _, fs in os.walk(folder)
                     for f in fs if f.endswith("_polygon.shp")]
        point_shps = [os.path.join(r, f) for r, _, fs in os.walk(folder)
                      for f in fs if f.endswith("_point.shp")]

        orig_crs = "EPSG:32644"
        if poly_shps:
            try:
                g0 = gpd.read_file(poly_shps[0])
                if g0.crs:
                    orig_crs = str(g0.crs)
            except:
                pass

        # Reproject to original CRS
        gdf_new = gdf_new.to_crs(orig_crs)

        # Repair geometries
        gdf_new["geometry"] = [_close_poly(_repair(g)) if g else None for g in gdf_new.geometry]
        gdf_new = gdf_new[gdf_new.geometry.notna()]

        # Separate polygons and points
        poly_gdf = gdf_new[gdf_new.geometry.geom_type.isin(['Polygon', 'MultiPolygon'])].copy()
        point_gdf = gdf_new[gdf_new.geometry.geom_type.isin(['Point', 'MultiPoint'])].copy()

        # --- Handle Polygons ---
        if not poly_gdf.empty:
            # Ensure area column
            if "Area_ha" not in poly_gdf.columns:
                poly_gdf["Area_ha"] = [round(g.area/10000, 4) if g else 0 for g in poly_gdf.geometry]
            # Save polygon shapefile
            shp_path = os.path.join(folder, "edited_polygon.shp")
            poly_gdf.to_file(shp_path)
            # Overwrite existing polygon shapefile
            for shp in poly_shps:
                if os.path.basename(shp).endswith("_polygon.shp"):
                    shutil.copyfile(shp_path, shp)
                    break
        else:
            # If no polygons, keep existing (should not happen for Group C)
            pass

        # --- Handle Points ---
        if not point_gdf.empty:
            # Ensure point attributes: SN (or Point_ID) and coordinates
            # If Point_ID column exists, use it; otherwise create SN
            if "SN" not in point_gdf.columns:
                point_gdf["SN"] = range(1, len(point_gdf) + 1)
            # Ensure X and Y columns (from geometry)
            point_gdf["X"] = point_gdf.geometry.x
            point_gdf["Y"] = point_gdf.geometry.y

            # Save point shapefile
            point_shp_path = os.path.join(folder, "edited_point.shp")
            point_gdf.to_file(point_shp_path)
            # Overwrite existing point shapefile
            for shp in point_shps:
                if os.path.basename(shp).endswith("_point.shp"):
                    shutil.copyfile(point_shp_path, shp)
                    break

            # Regenerate Excel file
            excel_path = None
            for root, _, files in os.walk(folder):
                for f in files:
                    if f.endswith("_sampleplot.xlsx"):
                        excel_path = os.path.join(root, f)
                        break
                if excel_path:
                    break
            if excel_path:
                # Create DataFrame from point_gdf
                df = point_gdf[["SN", "X", "Y"]].copy()
                df.to_excel(excel_path, index=False)
        else:
            # If no points (all deleted), we might remove the point files or keep empty.
            # For safety, we can write an empty point shapefile and Excel.
            # But we'll skip for now – user likely wants to keep at least some points.
            pass

        # --- Re‑render the map ---
        # Load the updated polygon and point layers
        from elfakgis.geo.geom import restore_shp_cols as _restore
        updated_poly = _restore(gpd.read_file(poly_shps[0])) if poly_shps else None
        updated_point = _restore(gpd.read_file(point_shps[0])) if point_shps else None
        # Line layer is not editable, we can load it from existing file
        line_shps = [os.path.join(r, f) for r, _, fs in os.walk(folder)
                     for f in fs if f.endswith("_line.shp")]
        line_gdf = _restore(gpd.read_file(line_shps[0])) if line_shps else None

        layout_state = data.get("layout_state", get_default_layout_state())
        if updated_poly is not None and not updated_poly.empty:
            bounds = updated_poly.total_bounds
        else:
            bounds = (0, 0, 1, 1)
        if bounds is not None and len(bounds) == 4:
            w = bounds[2] - bounds[0]
            h = bounds[3] - bounds[1]
            poly_aspect = w / h if h > 0 else 1.0
        else:
            poly_aspect = 1.0
        pp = os.path.join(folder, "output.png")
        label_col = "SN"
        try:
            with open(os.path.join(folder, "meta.json"), encoding="utf-8") as f:
                _meta = json.load(f)
            _title = _meta.get("forest_name") or None
            _area = _meta.get("area_ha")
        except Exception:
            _title, _area = None, None
        render_map(pp, poly_gdf=updated_poly, line_gdf=line_gdf, pts_gdf=updated_point,
                   label_col=label_col, point_label_col=label_col,
                   title=_title, area_ha=_area)

        return jsonify({"ok": True, "png": f"/outputs/{run_id}/output.png?t={uuid.uuid4().hex[:8]}"})
    except Exception as e:
        log.error(f"Save edit error: {traceback.format_exc()}")
        return jsonify({"error": f"Edit error: {e}\n{traceback.format_exc()}"}), 500

@maps_bp.route("/download/<run_id>")
def download(run_id):
    from elfakgis.core.config import OUTPUT
    run_id = _safe_runid(run_id)
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        return jsonify({"error": "Run not found"}), 404
    zip_path = os.path.join(folder, "..", f"{run_id}.zip")
    shutil.make_archive(folder, "zip", folder)
    return send_file(zip_path, as_attachment=True)

@maps_bp.route("/outputs/<run_id>/<path:filename>")
def serve_output(run_id, filename):
    from elfakgis.core.config import OUTPUT
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(os.path.join(folder, filename)):
        return jsonify({"error": "File not found"}), 404
    return send_from_directory(folder, filename)


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
