"""Elfak GIS Studio — Group G survey-point generator (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.core.store import _prog
from elfakgis.geo.render import render_map

import numpy as np
import pandas as pd
import geopandas as gpd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
import matplotlib.patheffects as pe
import matplotlib.gridspec as gridspec
import matplotlib.ticker as mticker
from matplotlib.transforms import Bbox
from shapely.geometry import Polygon, Point, LineString, MultiPolygon, box
from shapely.geometry import box as _sbox
from shapely.ops import unary_union
from shapely import affinity
# GROUP G – SURVEY POINT GENERATOR
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# GROUP G – SURVEY POINT GENERATOR HELPERS
# ----------------------------------------------------------------------

def _g_get_poly(geom):
    if geom is None or geom.is_empty:
        return
    if geom.geom_type == "Polygon":
        yield geom
    elif geom.geom_type == "MultiPolygon":
        for p in geom.geoms:
            yield p

def _g_vertex_points(gdf, comp_col):
    records = []
    for _, row in gdf.iterrows():
        cid = str(row[comp_col]) if comp_col and comp_col in row.index else ""
        for poly in _g_get_poly(row.geometry):
            for x, y in poly.exterior.coords:
                records.append({
                    "Point_Type": "Vertex",
                    "Source": "Vertex",
                    "Compartments": cid,
                    "Easting": round(x, 3),
                    "Northing": round(y, 3),
                    "geometry": Point(x, y)
                })
    return records

def _g_boundary_points(gdf, comp_col, spacing):
    records = []
    for _, row in gdf.iterrows():
        cid = str(row[comp_col]) if comp_col and comp_col in row.index else ""
        for poly in _g_get_poly(row.geometry):
            line = poly.exterior
            total = line.length
            d = spacing
            while d < total:
                pt = line.interpolate(d)
                records.append({
                    "Point_Type": "Boundary",
                    "Source": "Boundary",
                    "Compartments": cid,
                    "Easting": round(pt.x, 3),
                    "Northing": round(pt.y, 3),
                    "geometry": pt
                })
                d += spacing
    return records

def _g_divider_points(gdf, comp_col, spacing):
    records = []
    rows = list(gdf.iterrows())
    n = len(rows)
    for i in range(n):
        _, ri = rows[i]
        ci = str(ri[comp_col]) if comp_col and comp_col in ri.index else f"C{i+1}"
        for j in range(i + 1, n):
            _, rj = rows[j]
            cj = str(rj[comp_col]) if comp_col and comp_col in rj.index else f"C{j+1}"
            try:
                if not ri.geometry.intersects(rj.geometry):
                    continue
                common = ri.geometry.boundary.intersection(rj.geometry.boundary)
            except Exception:
                continue
            lines = []
            if common.geom_type == "LineString":
                lines = [common]
            elif common.geom_type == "MultiLineString":
                lines = list(common.geoms)
            elif common.geom_type == "GeometryCollection":
                lines = [g for g in common.geoms
                         if g.geom_type in ("LineString", "MultiLineString")]
            comp_pair = ",".join(sorted([ci, cj]))
            for seg in lines:
                if seg.is_empty:
                    continue
                total = seg.length
                d = spacing
                while d < total:
                    pt = seg.interpolate(d)
                    records.append({
                        "Point_Type": "Divider",
                        "Source": "Divider",
                        "Compartments": comp_pair,
                        "Easting": round(pt.x, 3),
                        "Northing": round(pt.y, 3),
                        "geometry": pt
                    })
                    d += spacing
    return records

def _g_merge_dedup(vertex_recs, boundary_recs, divider_recs):
    PRIO = {"Divider": 3, "Vertex": 2, "Boundary": 1}
    merged = {}
    for recs in (boundary_recs, vertex_recs, divider_recs):
        for r in recs:
            key = (round(r["Easting"], 3), round(r["Northing"], 3))
            if key not in merged:
                merged[key] = r.copy()
                merged[key]["_all_sources"] = {r["Source"]}
                merged[key]["_all_comps"] = {r["Compartments"]}
                merged[key]["_prio"] = PRIO.get(r["Source"], 0)
            else:
                existing = merged[key]
                existing["_all_sources"].add(r["Source"])
                existing["_all_comps"].add(r["Compartments"])
                new_prio = PRIO.get(r["Source"], 0)
                if new_prio > existing["_prio"]:
                    existing["Point_Type"] = r["Point_Type"]
                    existing["_prio"] = new_prio
    result = []
    for rec in merged.values():
        rec["Source"] = "+".join(sorted(rec["_all_sources"]))
        comp_set = set()
        for cs in rec["_all_comps"]:
            for c in cs.split(","):
                if c:
                    comp_set.add(c.strip())
        rec["Compartments"] = ",".join(sorted(comp_set))
        result.append(rec)
    return result

def _g_assign_ids(records):
    ORDER = {"Divider": 0, "Vertex": 1, "Boundary": 2}
    records.sort(key=lambda r: (
        ORDER.get(r["Point_Type"], 9),
        r["Compartments"],
        r["Easting"],
        r["Northing"]
    ))
    for i, r in enumerate(records, 1):
        r["Point_ID"] = f"P{i:06d}"
    return records

def _g_export(records, epsg, out_dir, prefix="ForestPoints"):
    if not records:
        raise ValueError("No points generated. Check shapefile and spacing.")
    df = pd.DataFrame([{
        "Point_ID": r["Point_ID"],
        "Point_Type": r["Point_Type"],
        "Source": r["Source"],
        "Compartments": r["Compartments"],
        "Easting": r["Easting"],
        "Northing": r["Northing"]
    } for r in records])
    csv_path = os.path.join(out_dir, f"{prefix}.csv")
    df.to_csv(csv_path, index=False)
    xlsx_path = os.path.join(out_dir, f"{prefix}.xlsx")
    df.to_excel(xlsx_path, index=False)
    shp_gdf = gpd.GeoDataFrame(df,
                               geometry=[r["geometry"] for r in records],
                               crs=f"EPSG:{epsg}")
    shp_path = os.path.join(out_dir, f"{prefix}.shp")
    shp_gdf_valid = shp_gdf[shp_gdf.geometry.notna() & ~shp_gdf.geometry.is_empty].copy()
    if not shp_gdf_valid.empty:
        shp_gdf_valid.to_file(shp_path)
    return df, shp_gdf_valid

def _g_preview(shp_gdf, poly_gdf, path, safe_rect=None, title="Forest Survey Points", layout_state=None):
    render_map(path, poly_gdf=poly_gdf, pts_gdf=shp_gdf,
               point_label_col="Point_ID", title=title, module="G")

def _extract_shapefile_basename_from_zip(file_storage, target_shp=None):
    """
    Read a ZIP in memory and return the basename (without extension) of the first .shp
    or the one matching target_shp. Raises ValueError if no .shp is found.
    """
    import io, zipfile
    zip_bytes = file_storage.read()
    if len(zip_bytes) < 100:
        raise ValueError("ZIP file is empty or too small.")
    file_storage.seek(0)

    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        namelist = zf.namelist()
        shps = [n for n in namelist if n.lower().endswith('.shp')]
        if not shps:
            raise ValueError("No .shp file found inside the ZIP.")
        if target_shp:
            target_base = os.path.basename(target_shp)
            chosen = next((n for n in shps if os.path.basename(n) == target_base), None)
            if chosen is None:
                raise ValueError(f"Target shapefile '{target_shp}' not found in ZIP.")
        else:
            chosen = shps[0]
        return os.path.splitext(os.path.basename(chosen))[0]
def group_g(file_storage, dem_zone, comp_col_name, spacing, out_dir, run_id, target_shp=None, base_name="ForestPoints"):
    """
    Process a shapefile (or ZIP) of compartments, generate vertex, boundary, and divider points.
    """
    _prog(run_id, "Reading shapefile…", 5)

    # Use tempfile for ZIP extraction
    import tempfile
    import zipfile

    tmp_dir = None
    try:
        if file_storage.filename.lower().endswith(".zip"):
            # Handle ZIP
            tmp_dir = tempfile.mkdtemp(prefix="group_g_")
            zip_path = os.path.join(tmp_dir, "upload.zip")
            file_storage.save(zip_path)

            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_dir)

            # Find .shp files
            shps = []
            for root, _, files in os.walk(tmp_dir):
                for fname in files:
                    if fname.lower().endswith(".shp"):
                        shps.append(os.path.join(root, fname))

            if not shps:
                raise ValueError("No .shp file found in the uploaded ZIP.")

            # Choose target shapefile if specified
            shp_path = shps[0]
            if target_shp:
                for s in shps:
                    if os.path.basename(s) == os.path.basename(target_shp):
                        shp_path = s
                        break

            gdf = gpd.read_file(shp_path)
        else:
            # Direct shapefile upload (single .shp file) – save to temp
            tmp_dir = tempfile.mkdtemp(prefix="group_g_")
            shp_path = os.path.join(tmp_dir, file_storage.filename)
            file_storage.save(shp_path)
            gdf = gpd.read_file(shp_path)

        if gdf.empty:
            raise ValueError("Shapefile is empty or could not be read.")

        # Ensure CRS
        epsg = 32644 if str(dem_zone).strip() in ("44", "44N", "EPSG:32644") else 32645
        if gdf.crs is None:
            gdf = gdf.set_crs(f"EPSG:{epsg}")
        else:
            gdf = gdf.to_crs(f"EPSG:{epsg}")

        # Validate geometry type
        if not all(t in ("Polygon", "MultiPolygon") for t in gdf.geom_type.unique()):
            raise ValueError("Shapefile must contain only Polygon / MultiPolygon features.")

        # Detect compartment column
        comp_col = None
        if comp_col_name and comp_col_name in gdf.columns:
            comp_col = comp_col_name
        else:
            for alias in ("Comp_ID","Comp_No","comp_id","comp_no","Compartment","COMP"):
                if alias in gdf.columns:
                    comp_col = alias
                    break

        area_ha = round(gdf.geometry.area.sum() / 10000, 3)

        _prog(run_id, "Generating vertex points…", 20)
        v_recs = _g_vertex_points(gdf, comp_col)

        _prog(run_id, f"Generating boundary points (spacing={spacing}m)…", 35)
        b_recs = _g_boundary_points(gdf, comp_col, spacing)

        _prog(run_id, "Finding shared divider lines…", 50)
        d_recs = _g_divider_points(gdf, comp_col, spacing)

        _prog(run_id, f"Merging {len(v_recs)+len(b_recs)+len(d_recs)} raw records…", 60)
        all_recs = _g_merge_dedup(v_recs, b_recs, d_recs)

        _prog(run_id, f"Deduplicated to {len(all_recs)} unique points — assigning IDs…", 70)
        all_recs = _g_assign_ids(all_recs)

        _prog(run_id, f"Exporting {len(all_recs)} points → SHP / CSV / XLSX…", 80)
        df, shp_gdf = _g_export(all_recs, epsg, out_dir, prefix=base_name)

        summary = {
            "total": len(all_recs),
            "vertex": sum(1 for r in all_recs if r["Point_Type"] == "Vertex"),
            "boundary": sum(1 for r in all_recs if r["Point_Type"] == "Boundary"),
            "divider": sum(1 for r in all_recs if r["Point_Type"] == "Divider"),
            "area_ha": area_ha,
            "epsg": epsg,
            "spacing": spacing,
            "comp_col": comp_col or "—",
            "compartments": int(len(gdf)),
        }
        return df, shp_gdf, gdf, summary

    finally:
        if tmp_dir and os.path.exists(tmp_dir):
            shutil.rmtree(tmp_dir, ignore_errors=True)
# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
