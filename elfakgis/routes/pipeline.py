"""Elfak GIS Studio — heavy GIS pipeline routes (groups lazy-load here) (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
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
pipeline_bp = Blueprint('pipeline_bp', __name__)
# ----------------------------------------------------------------------
# UPLOAD ROUTE (handles Groups A–G) with Human-Readable Run ID
# ----------------------------------------------------------------------

def _upload_impl():
    from elfakgis.core.config import DEM_CACHE_DIR, DEM_CATALOG_DIR, OUTPUT
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        _safe_filename(file.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    # Heavy GIS binds only after validation — and only the dispatched group's
    # module loads, so one group works at a time (fast, low RAM).
    from elfakgis.geo.geom import get_crs, read_input
    from elfakgis.geo.render import render_map
    from elfakgis.geo.kmz import generate_kmz

    # Determine base name for run ID
    module = request.form.get("module", "A")
    if module == "F":
        base_name = request.form.get("f_forest") or os.path.splitext(file.filename)[0]
    else:
        base_name = request.form.get("forest") or os.path.splitext(file.filename)[0]

    run_id = request.form.get("_run_id") or _generate_run_id(base_name)
    # Ensure uniqueness (very unlikely, but safe)
    while os.path.exists(os.path.join(OUTPUT, run_id)):
        run_id = _generate_run_id(base_name + "_" + secrets.token_hex(2))

    with _PROG_LOCK: _PROG[run_id] = []
    try:
        if module in ("A", "B", "D") and file.filename.lower().endswith(".zip"):
            return jsonify({"error": "ZIP files are not allowed for this module. Please upload a CSV or Excel file.", "run_id": run_id}), 400

        mode = request.form.get("mode", "A")
        zone = request.form.get("zone", "44")
        title = request.form.get("title", "").strip()
        legend_title = request.form.get("legend_title", "Legend").strip() or "Legend"
        label_col = request.form.get("label_col", "").strip()
        try:
            mapping = json.loads(request.form.get("mapping", "{}"))
        except:
            mapping = {}
        w = float(request.form.get("w", 50))
        h = float(request.form.get("h", 50))
        rows = int(request.form.get("rows", 10))
        cols = int(request.form.get("cols", 10))
        forest = request.form.get("forest") or (mapping or {}).get("forest") or "FOREST"
        username = _require_login() or "guest"
        out = os.path.join(OUTPUT, run_id)
        os.makedirs(out, exist_ok=True)
        crs = get_crs(zone)
        _prog(run_id, f"Starting module {module}…", 2)
        lc_out = None
        lp_gdf = None
        area_ha_disp = None
        base_name_file = os.path.splitext(os.path.basename(file.filename))[0]
        if file.filename.lower().endswith(".zip"):
            try:
                from elfakgis.groups.group_g import _extract_shapefile_basename_from_zip
                base_name_file = _extract_shapefile_basename_from_zip(file, mapping.get("target_shp"))
            except Exception as e:
                log.warning(f"Could not extract shapefile basename from ZIP: {e}, using ZIP filename")
                base_name_file = os.path.splitext(os.path.basename(file.filename))[0]

        if module == "B":
            from elfakgis.groups.group_b import group_b
            _prog(run_id, "Reading…", 8)
            df = read_input(file)
            poly, line, pts = group_b(df, crs, out, mapping)
            lc_out = label_col or "Forest"
        elif module == "C":
     	     from elfakgis.groups.group_c import group_c
     	     _prog(run_id, "Processing ZIP or CSV…", 10)
     	     poly, line, pts = group_c(file, crs, w, h, rows, cols, out, mode, mapping,
                               base_name=base_name, run_id=run_id)
     	     lc_out = "SN"
     	     lp_gdf = pts
        elif module == "D":
            from elfakgis.groups.group_d import group_d
            _prog(run_id, "Reading…", 8)
            df = read_input(file)
            d_mode = request.form.get("d_mode", "A")
            poly, line, pts = group_d(df, crs, out, mapping, mode=d_mode)
            lc_out = label_col or "Forest"
        elif module == "E":
            from elfakgis.groups.group_e import group_e
            e_mode = request.form.get("e_mode", "A")
            nc = max(2, min(15, int(request.form.get("n_compartments", 4))))
            at = float(request.form.get("area_tol_ha", "0.3") or "0.3")
            method = request.form.get("e_method", "bisect")
            is_zip = file.filename.lower().endswith(".zip")
            fcn = request.form.get("forest_col_name") or None
            if mapping and not mapping.get("forest"):
                mapping["forest"] = base_name_file
            _prog(run_id, "Loading input…", 8)
            src_data = file if is_zip else read_input(file)
            poly, line, pts, survey = group_e(src_data, crs, out, mapping,
                                      e_mode=e_mode, n_compartments=nc,
                                      is_zip=is_zip, fcol=fcn,
                                      area_tol_ha=at, method=method, run_id=run_id)
            _prog(run_id, "Rendering preview…", 88)
            render_map(os.path.join(out, "output.png"), poly_gdf=poly,
                       pts_gdf=survey if survey is not None and not survey.empty else None,
                       point_label_col="SN",
                       label_col=label_col or "Comp_ID",
                       title=forest or base_name_file, module="E",
                       legend_title=legend_title)
            _save_run_meta(out, forest or base_name_file,
                           float(poly["Area_ha"].sum()) if "Area_ha" in poly.columns else None,
                           module="E", title=forest or base_name_file,
                           legend_title=legend_title)
            kmz_url = generate_kmz(poly, line, pts, out, run_id)
            _append_run(username, run_id, "E")
            _prog(run_id, "Complete.", 100)
            return jsonify({"run_id": run_id, "download": f"/download/{run_id}",
                            "kmz_url": kmz_url, "map_editor_url": f"/map_editor/{run_id}"})
        elif module == "F":
            dem_cache_key = request.form.get("dem_cache_key", "").strip()
            dem_catalog_path = request.form.get("dem_catalog_path", "").strip()

            class _FileDEM:
                def __init__(self, p):
                    self.filename = os.path.basename(p)
                    self._p = p
                def save(self, dest):
                    shutil.copy2(self._p, dest)
                def read(self, size=-1):
                    with open(self._p, "rb") as f:
                        return f.read(size)

            if dem_cache_key:
                candidates = [f for f in os.listdir(DEM_CACHE_DIR) if f.startswith(dem_cache_key)]
                if not candidates:
                    return jsonify({"error": "Cached DEM not found. Please select again.", "run_id": run_id}), 400
                dem_f = _FileDEM(os.path.join(DEM_CACHE_DIR, candidates[0]))
            elif dem_catalog_path:
                cat_full = _safe_path(DEM_CATALOG_DIR, dem_catalog_path)
                if not os.path.exists(cat_full):
                    return jsonify({"error": f"DEM catalog file not found: {dem_catalog_path}", "run_id": run_id}), 400
                dem_f = _FileDEM(cat_full)
            else:
                dem_f = request.files.get("dem_file")
                if not dem_f:
                    return jsonify({"error": "No DEM selected. Choose from the catalog dropdown or upload a .tif file.", "run_id": run_id}), 400
            f_forest = request.form.get("f_forest") or base_name_file
            f_mode = request.form.get("f_mode", "A")
            cc = request.form.get("comp_col") or None
            bzip = file.filename.lower().endswith(".zip")
            fa = None
            fas = request.form.get("field_area_ha", "").strip()
            if fas:
                try:
                    fa = float(fas)
                except:
                    pass
            import geopandas as gpd
            from elfakgis.groups.group_f import group_f
            sr, vgdf, bgdf, fmo, pgs = group_f(file, dem_f, crs, out, mapping,
                                                boundary_is_zip=bzip,
                                                forest_name=f_forest,
                                                f_mode=f_mode,
                                                comp_col_name=cc,
                                                field_area_ha=fa,
                                                run_id=run_id)
            _prog(run_id, "Rendering preview…", 92)
            render_map(os.path.join(out, "output.png"), poly_gdf=vgdf, line_gdf=bgdf,
                       label_col="Description", title=f_forest, module="F",
                       legend_title=legend_title,
                       slope_mode=True, slope_areas={r["Slope_Range"]: r["Area_ha"] for r in sr})
            poly = vgdf if (vgdf is not None and not vgdf.empty) else gpd.GeoDataFrame()
            line = gpd.GeoDataFrame()
            pts = gpd.GeoDataFrame()
            _save_run_meta(out, f_forest,
                           float(poly["Area_ha"].sum()) if "Area_ha" in poly.columns else None,
                           module="F", title=f_forest,
                           legend_title=legend_title)
            kmz_url = generate_kmz(poly, line, pts, out, run_id)
            _append_run(username, run_id, "F")
            _prog(run_id, "Complete.", 100)
            return jsonify({"run_id": run_id, "download": f"/download/{run_id}",
                            "kmz_url": kmz_url, "map_editor_url": f"/map_editor/{run_id}"})
        else:
            from elfakgis.groups.group_a import group_a
            _prog(run_id, "Reading…", 8)
            df = read_input(file)
            poly, line, pts = group_a(df, forest, crs, out, mapping)
            if not poly.empty and "Area_ha" in poly.columns:
                area_ha_disp = float(poly["Area_ha"].sum())
            lc_out = label_col or "Forest"

        point_label_col = None
        if pts is not None and not pts.empty:
            for cand in ["SN", "Order", "Point_ID", "ID", "point_id"]:
                if cand in pts.columns:
                    point_label_col = cand
                    break
            if point_label_col is None:
                point_label_col = label_col or None

        _prog(run_id, "Rendering preview…", 88)
        render_map(os.path.join(out, "output.png"), poly_gdf=poly, line_gdf=line, pts_gdf=pts,
                   label_col=lc_out, point_label_col=point_label_col, title=title,
                   module=module, legend_title=legend_title)
        _area_ha = None
        if poly is not None and not poly.empty:
            if "Area_ha" in poly.columns:
                _area_ha = float(poly["Area_ha"].sum())
            else:
                try: _area_ha = round(poly.geometry.area.sum() / 10000, 4)
                except Exception: pass
        _save_run_meta(out, forest or base_name_file, _area_ha,
                           module=module, title=title,
                           legend_title=legend_title)
        kmz_url = generate_kmz(poly, line, pts, out, run_id)
        _append_run(username, run_id, module)
        _prog(run_id, "Complete.", 100)
        return jsonify({"run_id": run_id, "download": f"/download/{run_id}",
                        "kmz_url": kmz_url, "map_editor_url": f"/map_editor/{run_id}"})
    except ValueError as e:
        _prog(run_id, f"ERROR: {e}", 0)
        return jsonify({"error": str(e), "run_id": run_id}), 400
    except Exception as e:
        _prog(run_id, f"ERROR: {e}", 0)
        return jsonify({"error": f"Unexpected error: {e}", "run_id": run_id}), 500

# ----------------------------------------------------------------------
# GROUP H ROUTE with Human-Readable Run ID
# ----------------------------------------------------------------------

def _run_h_impl():
    # Generate human-readable run ID from boundary file name
    from elfakgis.core.config import OUTPUT, UPLOAD
    boundary_file = request.files.get('boundary')
    if not boundary_file:
        return jsonify({"error": "Missing boundary file"}), 400
    base_name = os.path.splitext(boundary_file.filename)[0]
    run_id = request.form.get("_run_id") or _generate_run_id(base_name)
    while os.path.exists(os.path.join(OUTPUT, run_id)):
        run_id = _generate_run_id(base_name + "_" + secrets.token_hex(2))

    _prog(run_id, "Starting Group H...", 0)
    try:
        required = ['boundary', 'compartments', 'dem', 'satellite', 'sample_points']
        files = {}
        for key in required:
            if key not in request.files or request.files[key].filename == '':
                return jsonify({"error": f"Missing required file: {key}", "run_id": run_id}), 400
            files[key] = request.files[key]
        # Group H loads here — only group H works in this request.
        from elfakgis.groups.group_h import process_group_h
        survey_file = request.files.get('survey_points')
        crs = request.form.get('crs', 'EPSG:32644')

        tmp_dir = os.path.join(UPLOAD, f"h_temp_{run_id}")
        os.makedirs(tmp_dir, exist_ok=True)
        saved_files = {}
        for key, f in files.items():
            path = os.path.join(tmp_dir, f"{key}_{f.filename}")
            f.save(path)
            saved_files[key] = path
        if survey_file and survey_file.filename != '':
            path = os.path.join(tmp_dir, f"survey_{survey_file.filename}")
            survey_file.save(path)
            saved_files['survey'] = path

        out_dir = os.path.join(OUTPUT, run_id)
        os.makedirs(out_dir, exist_ok=True)

        zip_path, out_dir = process_group_h(
            saved_files['boundary'],
            saved_files['compartments'],
            saved_files['dem'],
            saved_files['satellite'],
            saved_files['sample_points'],
            saved_files.get('survey'),
            crs=crs,
            out_dir=out_dir,
            run_id=run_id
        )

        shutil.rmtree(tmp_dir, ignore_errors=True)

        preview_path = os.path.join(out_dir, "Slope_Map.png")
        if os.path.exists(preview_path):
            shutil.copy(preview_path, os.path.join(out_dir, "output.png"))

        _append_run(_require_login() or "guest", run_id, "H", "Group H maps generated")
        _prog(run_id, "Complete.", 100)

        return jsonify({
            "run_id": run_id,
            "download": f"/download/{run_id}",
            "message": "Group H processing complete. Six maps generated."
        })
    except Exception as e:
        _prog(run_id, f"ERROR: {e}", 0)
        return jsonify({"error": str(e), "run_id": run_id}), 500

def _run_g_impl():
    from elfakgis.core.config import OUTPUT
    from elfakgis.geo.kmz import generate_kmz
    run_id = request.form.get("_run_id") or str(uuid.uuid4())
    _prog(run_id, "Starting Group G...", 0)
    try:
        if "file" not in request.files:
            return jsonify({"error": "No shapefile uploaded.", "run_id": run_id}), 400
        file = request.files["file"]

        # Get parameters
        zone = request.form.get("zone", "44")
        comp_col = request.form.get("comp_col", "").strip() or None
        title = request.form.get("title", "Forest Survey Points").strip()
        try:
            spacing = float(request.form.get("spacing", "20"))
            if spacing <= 0:
                raise ValueError
        except:
            return jsonify({"error": "Invalid spacing value.", "run_id": run_id}), 400

        # Determine base_name from uploaded file
        base_name = os.path.splitext(os.path.basename(file.filename))[0]
        target_shp = request.form.get("target_shp", "").strip() or None

        username = _require_login() or "guest"
        out = os.path.join(OUTPUT, run_id)
        os.makedirs(out, exist_ok=True)

        # Group G loads here — only group G works in this request.
        import geopandas as gpd
        from elfakgis.groups.group_g import _g_preview, group_g

        # Process
        df, shp_gdf, poly_gdf, summary = group_g(
            file, zone, comp_col, spacing, out, run_id,
            target_shp=target_shp, base_name=base_name
        )

        _prog(run_id, "Rendering A4 map…", 90)
        _g_preview(shp_gdf, poly_gdf, os.path.join(out, "output.png"), title=title)
        _save_run_meta(out, title or base_name, summary.get("area_ha"),
                           module="G", title=title)

        kmz_url = None
        try:
            kmz_url = generate_kmz(poly_gdf, gpd.GeoDataFrame(), shp_gdf, out, run_id)
        except:
            pass

        _append_run(username, run_id, "G", f"{summary['total']} pts | {summary['compartments']} compartments")
        _prog(run_id, "Complete.", 100)
        return jsonify({
            "run_id": run_id,
            "download": f"/download/{run_id}",
            "kmz_url": kmz_url,
            "summary": summary,
            "map_editor_url": f"/map_editor/{run_id}"
        })
    except Exception as e:
        _prog(run_id, f"ERROR: {e}", 0)
        log.error(f"Group G error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "run_id": run_id}), 500


@pipeline_bp.route("/thesis_options", methods=["GET"])
def thesis_options():
    """Dropdown data for Thesis Map (Group I): provinces + districts."""
    try:
        from elfakgis.groups.group_i import thesis_options as _opts
        return jsonify(_opts())
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _run_thesis_impl():
    """Group I — Thesis Locator Map (4-panel A4 landscape)."""
    from elfakgis.core.config import OUTPUT
    from elfakgis.core.store import _save_run_meta, _append_run
    from elfakgis.geo.geom import get_crs
    run_id = request.form.get("_run_id") or str(uuid.uuid4())
    _prog(run_id, "Starting Thesis Map...", 0)
    try:
        file = request.files.get("file") or request.files.get("boundary")
        if not file or not file.filename:
            return jsonify({"error": "No study-area file uploaded.", "run_id": run_id}), 400
        try:
            _safe_filename(file.filename)
        except ValueError as e:
            return jsonify({"error": str(e), "run_id": run_id}), 400
        province = request.form.get("province", "").strip()
        district = request.form.get("district", "").strip()
        if not province or not district:
            return jsonify({"error": "Select a province and a district.", "run_id": run_id}), 400
        cf_name = (request.form.get("cf_name", "").strip()
                   or os.path.splitext(os.path.basename(file.filename))[0])
        title = request.form.get("title", "").strip()
        zone = request.form.get("zone", "44")
        try:
            mapping = json.loads(request.form.get("mapping", "{}"))
        except Exception:
            mapping = {}

        username = _require_login() or "guest"
        out = os.path.join(OUTPUT, run_id)
        os.makedirs(out, exist_ok=True)

        from elfakgis.groups.group_i import group_thesis
        summary = group_thesis(
            file, province, district, get_crs(zone), out,
            mapping=mapping, cf_name=cf_name, run_id=run_id)
        if title:
            pass  # title is baked via cf_name map heading; kept for history

        _save_run_meta(out, cf_name, summary.get("area_ha"),
                       module="I", title=title or f"Location Map of {cf_name}",
                       legend_title="Legend",
                       province=summary.get("province"),
                       district=summary.get("district"),
                       zone=zone)
        _append_run(username, run_id, "I",
                    f"{summary.get('district')} | {summary.get('province')}")
        _prog(run_id, "Complete.", 100)
        return jsonify({
            "run_id": run_id,
            "download": f"/download/{run_id}",
            "kmz_url": summary.get("kmz_url"),
            "summary": summary,
            "map_editor_url": f"/map_editor/{run_id}",
        })
    except ValueError as e:
        _prog(run_id, f"ERROR: {e}", 0)
        return jsonify({"error": str(e), "run_id": run_id}), 400
    except Exception as e:
        _prog(run_id, f"ERROR: {e}", 0)
        log.error(f"Thesis map error: {traceback.format_exc()}")
        return jsonify({"error": str(e), "run_id": run_id}), 500


# ----------------------------------------------------------------------
# Background execution (502 fix): heavy GIS work runs in daemon threads so
# the POST returns 202 instantly — Render's proxy never waits on a pipeline.
# The client polls GET /result/<run_id> (maps.py) for the final payload.
# Route bodies above (_*_impl) run UNCHANGED inside a replayed request
# context (same form fields + files, same session user).
# ----------------------------------------------------------------------

_BG_SEM = threading.Semaphore(int(os.environ.get("MAX_BG_JOBS", "2")))


def _bg_save(storage, bg_dir, field):
    """Spool one uploaded file to the bg dir. Returns {field: (name, path)}."""
    ext = os.path.splitext(storage.filename or "")[1].lower()
    dest = os.path.join(bg_dir, f"{field}{ext}")
    storage.save(dest)
    return {field: (storage.filename, dest)}


def _launch_bg(impl, run_id, username, saved, form):
    from elfakgis.core.store import _bg_pending
    _bg_pending(run_id)
    threading.Thread(target=_bg_replay,
                     args=(impl, run_id, username, saved, form),
                     daemon=True).start()


def _bg_replay(impl, run_id, username, saved, form):
    """Re-run an impl inside a fresh request context carrying the snapshotted
    uploads + form + user, then store its JSON payload for /result polling."""
    from elfakgis import app as _app
    from elfakgis.core.store import _bg_store
    handles = []
    try:
        _BG_SEM.acquire()
        try:
            data = dict(form)
            for field, (fname, path) in saved.items():
                h = open(path, "rb")
                handles.append(h)
                data[field] = (h, fname)
            with _app.test_request_context("/_bg", method="POST", data=data):
                from flask import session as _sess
                if username and username != "guest":
                    _sess["username"] = username
                resp = impl()
            for h in handles:
                try:
                    h.close()
                except Exception:
                    pass
            handles = []
            if isinstance(resp, tuple):
                resp_obj, code = resp[0], resp[1]
            else:
                resp_obj, code = resp, getattr(resp, "status_code", 200)
            try:
                payload = resp_obj.get_json()
            except Exception:
                payload = {"run_id": run_id}
            if not isinstance(payload, dict):
                payload = {"result": payload}
            _bg_store(run_id, code if isinstance(code, int) else 200,
                       payload)
        finally:
            try:
                _BG_SEM.release()
            except Exception:
                pass
    except Exception as e:
        _prog(run_id, f"ERROR: {e}", 0)
        try:
            _bg_store(run_id, 500, {"error": f"Unexpected error: {e}",
                                    "run_id": run_id})
        except Exception:
            pass
    finally:
        for h in handles:
            try:
                h.close()
            except Exception:
                pass


def _bg_accept(file_fields):
    """Snapshot uploads for background launch. Returns (saved, bg_dir)."""
    from elfakgis.core.config import UPLOAD
    bg_dir = os.path.join(UPLOAD, f"bg_{uuid.uuid4().hex[:8]}")
    os.makedirs(bg_dir, exist_ok=True)
    saved = {}
    for field in file_fields:
        storage = request.files.get(field)
        if storage is not None and storage.filename:
            saved.update(_bg_save(storage, bg_dir, field))
    return saved


@pipeline_bp.route("/upload", methods=["POST"])
@_cool_down(seconds=2)
def upload():
    """Accept-only wrapper: snapshot uploads, launch job, return 202."""
    from elfakgis.core.config import OUTPUT
    file = request.files.get("file")
    if not file:
        return jsonify({"error": "No file uploaded."}), 400
    try:
        _safe_filename(file.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    module = request.form.get("module", "A")
    if module == "F":
        base_name = request.form.get("f_forest") or os.path.splitext(file.filename)[0]
    else:
        base_name = request.form.get("forest") or os.path.splitext(file.filename)[0]
    run_id = _generate_run_id(base_name)
    while os.path.exists(os.path.join(OUTPUT, run_id)):
        run_id = _generate_run_id(base_name + "_" + secrets.token_hex(2))
    with _PROG_LOCK:
        _PROG[run_id] = []
    try:
        saved = _bg_accept(["file", "dem_file"])
        if "file" not in saved:
            return jsonify({"error": "No file uploaded.", "run_id": run_id}), 400
        form = request.form.to_dict()
        form["_run_id"] = run_id
        username = _require_login() or "guest"
        _launch_bg(_upload_impl, run_id, username, saved, form)
    except Exception as e:
        return jsonify({"error": str(e), "run_id": run_id}), 400
    return jsonify({"accepted": True, "run_id": run_id}), 202


@pipeline_bp.route("/run_h", methods=["POST"])
@_cool_down(seconds=2)
def run_h():
    """Accept-only wrapper for Group H (5 required files + 1 optional)."""
    from elfakgis.core.config import OUTPUT
    boundary_file = request.files.get('boundary')
    if not boundary_file:
        return jsonify({"error": "Missing boundary file"}), 400
    required = ['boundary', 'compartments', 'dem', 'satellite', 'sample_points']
    for key in required:
        if key not in request.files or request.files[key].filename == '':
            return jsonify({"error": f"Missing required file: {key}"}), 400
    base_name = os.path.splitext(boundary_file.filename)[0]
    run_id = _generate_run_id(base_name)
    while os.path.exists(os.path.join(OUTPUT, run_id)):
        run_id = _generate_run_id(base_name + "_" + secrets.token_hex(2))
    with _PROG_LOCK:
        _PROG[run_id] = []
    try:
        saved = _bg_accept(required + ['survey_points'])
        form = request.form.to_dict()
        form["_run_id"] = run_id
        username = _require_login() or "guest"
        _launch_bg(_run_h_impl, run_id, username, saved, form)
    except Exception as e:
        return jsonify({"error": str(e), "run_id": run_id}), 400
    return jsonify({"accepted": True, "run_id": run_id}), 202


@pipeline_bp.route("/run_g", methods=["POST"])
@_cool_down(seconds=2)
def run_g():
    """Accept-only wrapper for Group G."""
    if "file" not in request.files:
        return jsonify({"error": "No shapefile uploaded."}), 400
    file = request.files["file"]
    run_id = str(uuid.uuid4())
    with _PROG_LOCK:
        _PROG[run_id] = []
    try:
        saved = _bg_accept(["file"])
        if "file" not in saved:
            return jsonify({"error": "No shapefile uploaded.", "run_id": run_id}), 400
        form = request.form.to_dict()
        form["_run_id"] = run_id
        username = _require_login() or "guest"
        _launch_bg(_run_g_impl, run_id, username, saved, form)
    except Exception as e:
        return jsonify({"error": str(e), "run_id": run_id}), 400
    return jsonify({"accepted": True, "run_id": run_id}), 202


@pipeline_bp.route("/run_thesis", methods=["POST"])
@_cool_down(seconds=2)
def run_thesis():
    """Accept-only wrapper for Group I (Thesis Locator Map)."""
    file = request.files.get("file") or request.files.get("boundary")
    if not file or not file.filename:
        return jsonify({"error": "No study-area file uploaded."}), 400
    try:
        _safe_filename(file.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    province = request.form.get("province", "").strip()
    district = request.form.get("district", "").strip()
    if not province or not district:
        return jsonify({"error": "Select a province and a district."}), 400
    run_id = str(uuid.uuid4())
    with _PROG_LOCK:
        _PROG[run_id] = []
    try:
        from elfakgis.core.config import UPLOAD
        bg_dir = os.path.join(UPLOAD, f"bg_{run_id}")
        os.makedirs(bg_dir, exist_ok=True)
        if "file" in request.files and request.files["file"].filename:
            saved = _bg_save(request.files["file"], bg_dir, "file")
        else:
            saved = _bg_save(request.files["boundary"], bg_dir, "boundary")
        form = request.form.to_dict()
        form["_run_id"] = run_id
        username = _require_login() or "guest"
        _launch_bg(_run_thesis_impl, run_id, username, saved, form)
    except Exception as e:
        return jsonify({"error": str(e), "run_id": run_id}), 400
    return jsonify({"accepted": True, "run_id": run_id}), 202


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
