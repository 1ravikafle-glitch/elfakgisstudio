"""Elfak GIS Studio — geometry/column helpers (canonical Group-E versions) (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
import numpy as np
import pandas as pd
import geopandas as gpd
from shapely.geometry import Polygon, Point, LineString, MultiPolygon, box
from shapely.ops import unary_union
from shapely import affinity

log = logging.getLogger("elfakgis")
# ----------------------------------------------------------------------
# Helper: Column detection, reading, CRS, geometry
# ----------------------------------------------------------------------

def _norm(s): return "".join(c for c in str(s).lower() if c.isalnum())
_XA={"x","xcoord","xcoordinate","xcord","east","easting","eastings","lon","long","longitude","lng","pointx","coordx","utme","utmx"}
_YA={"y","ycoord","ycoordinate","ycord","north","northing","northings","lat","latitude","pointy","coordy","utmn","utmy"}
_OA={"order","id","sn","sno","serial","serialno","seq","sequence","index","rowid","fid","no","num","number","plotid","plotno","pointid","pointno","pid"}
_FA={"forest","forestname","forestid","forestno","fname","forestblock","block"}
_CA={"compartment","comp","compartmentno","compartmentid","compno","compid","section","sectionno"}
def _find_col(df, aliases):
    for c in df.columns:
        if _norm(c) in aliases: return c
    return None
def safe_col(df, mapping, key, fallback):
    if mapping and mapping.get(key) and mapping[key] in df.columns: return mapping[key]
    if fallback in df.columns: return fallback
    for c in df.columns:
        if c.lower() == fallback.lower(): return c
    am = {"X":_XA,"Y":_YA,"Order":_OA,"Forest":_FA,"Compartment":_CA}
    if key in am:
        h = _find_col(df, am[key])
        if h: return h
    return None
def normalize_order(df):
    for c in df.columns:
        if _norm(c) in _OA and c != "Order": df = df.rename(columns={c:"Order"}); break
    return df
def read_input(file):
    n = file.filename.lower()
    if n.endswith(".csv"): return pd.read_csv(file, encoding="utf-8-sig")
    elif n.endswith((".xlsx",".xls")): return pd.read_excel(file)
    raise ValueError("Only CSV/Excel supported.")
def get_crs(zone): return f"EPSG:326{zone}"
def _safe_dn(s): return str(s).strip().replace("/","_").replace("\\","_").replace(":","_")

# Geometry helpers
def safe_polygon(coords):
    p = Polygon(coords); return p if p.is_valid else p.buffer(0)
_GEOM_TOL_FRAC = 1e-6   # fraction of orig area that counts as "leak"


# ═══════════════════════════════════════════════════════════
# SECTION 2 – LOW-LEVEL GEOMETRY HELPERS
# ═══════════════════════════════════════════════════════════

def _force_valid(geom):
    if geom is None or geom.is_empty:
        return None
    if geom.is_valid:
        return geom
    for tol in (1e-8, 1e-7, 1e-6):
        try:
            g = geom.simplify(tol, preserve_topology=True)
            if g and not g.is_empty and g.is_valid:
                return g
        except Exception:
            pass
    try:
        g = geom.buffer(0)
        if g and not g.is_empty and g.is_valid:
            return g
    except Exception:
        pass
    try:
        # shapely.validation.make_valid is the strongest repair and can rescue
        # polygons that buffer(0) mangles, but it only exists in shapely 2+.
        # It used to be called here with no import at all, so the call raised
        # NameError into a bare `except` and this tier silently never ran.
        from shapely.validation import make_valid
        g = make_valid(geom)
        if g and not g.is_empty and g.is_valid:
            return g
    except Exception:
        pass
    for eps in (1e-7, 1e-6, 1e-5):
        try:
            g = geom.buffer(eps).buffer(-eps)
            if g and not g.is_empty and g.is_valid:
                return g
        except Exception:
            continue
    try:
        g = geom.convex_hull
        if g and not g.is_empty and g.is_valid:
            return g
    except Exception:
        pass
    return None


def _close_poly(p):
    """Return a Polygon with explicitly closed rings; picks largest part of Multi."""
    if p is None:
        return None
    try:
        if p.is_empty:
            return p
        if p.geom_type == "MultiPolygon":
            parts = [x for x in p.geoms if not x.is_empty]
            if not parts:
                return p
            p = max(parts, key=lambda x: x.area)
        if p.geom_type != "Polygon":
            return p
        ext = list(p.exterior.coords)
        if ext[0] != ext[-1]:
            ext.append(ext[0])
        holes = []
        for ring in p.interiors:
            h = list(ring.coords)
            if h[0] != h[-1]:
                h.append(h[0])
            holes.append(h)
        c = Polygon(ext, holes)
        if c.is_empty:
            return p
        return c if c.is_valid else c.buffer(0)
    except Exception:
        return p


def _as_poly(g):
    """Extract the largest Polygon from any geometry, or None."""
    if g is None:
        return None
    try:
        if g.is_empty:
            return None
        if g.geom_type == "Polygon":
            return g
        if g.geom_type == "MultiPolygon":
            parts = [x for x in g.geoms if x.geom_type == "Polygon" and not x.is_empty]
            return max(parts, key=lambda x: x.area) if parts else None
        if g.geom_type in ("GeometryCollection",):
            polys = []
            for x in g.geoms:
                if x.geom_type == "Polygon" and not x.is_empty and x.area > 1e-10:
                    polys.append(x)
                elif x.geom_type == "MultiPolygon":
                    polys.extend(pp for pp in x.geoms
                                 if not pp.is_empty and pp.area > 1e-10)
            return max(polys, key=lambda x: x.area) if polys else None
    except Exception:
        pass
    return None


def _repair(g):
    """Full repair: validate → collapse Multi/Collection → close rings."""
    if g is None:
        return None
    try:
        if g.is_empty:
            return None
        g = _force_valid(g)
        if g is None or g.is_empty:
            return None
        if g.geom_type == "MultiPolygon":
            parts = [p for p in g.geoms if p and not p.is_empty and p.is_valid]
            if not parts:
                return None
            g = parts[0] if len(parts) == 1 else unary_union(parts)
            if g is None or g.is_empty:
                return None
        elif g.geom_type == "GeometryCollection":
            polys = [p for p in g.geoms
                     if p.geom_type in ("Polygon", "MultiPolygon")
                     and not p.is_empty and p.is_valid]
            if not polys:
                return None
            g = unary_union(polys)
            if g is None or g.is_empty:
                return None
        if g.geom_type == "Polygon":
            g = _close_poly(g)
        return g if (g and not g.is_empty) else None
    except Exception:
        try:
            fixed = g.buffer(0) if g else None
            return fixed if (fixed and not fixed.is_empty) else None
        except Exception:
            return None


# ═══════════════════════════════════════════════════════════
def _enforce_poly_gdf(gdf):
    if gdf is None or gdf.empty:
        return gdf
    keep = []
    for _, row in gdf.iterrows():
        g = row.geometry
        if g is None:
            continue
        try:
            g = _repair(g)
            if g is None or g.is_empty:
                continue
            if g.geom_type in ("Polygon", "MultiPolygon"):
                pg = g
            elif g.geom_type == "GeometryCollection":
                polys = [p for p in g.geoms
                         if p.geom_type in ("Polygon","MultiPolygon")
                         and not p.is_empty and p.area > 1e-10]
                if not polys:
                    continue
                pg = _repair(unary_union(polys))
            else:
                pg = _repair(g.buffer(0))
                if pg is None or pg.geom_type not in ("Polygon","MultiPolygon"):
                    continue
            if pg is None or pg.is_empty or pg.area < 1e-12:
                continue
            if pg.geom_type == "Polygon":
                pg = _close_poly(pg)
            elif pg.geom_type == "MultiPolygon":
                fixed = []
                for part in pg.geoms:
                    if part is None or part.is_empty: continue
                    cp = _close_poly(part)
                    if cp is not None and cp.geom_type == "Polygon" and not cp.is_empty and cp.area > 1e-12:
                        fixed.append(cp)
                if not fixed:
                    continue
                pg = MultiPolygon(fixed) if len(fixed) > 1 else fixed[0]
            r2 = row.copy()
            r2["geometry"] = pg
            keep.append(r2)
        except Exception:
            continue
    if not keep:
        return gpd.GeoDataFrame(columns=gdf.columns, crs=gdf.crs)
    result = gpd.GeoDataFrame(keep, crs=gdf.crs)
    result = result.reset_index(drop=True)
    return result


# ESRI DBF caps field names at 10 chars — pyogrio silently launders longer
# ones on save (Slope_Range→Slope_Rang, Description→Descriptio,
# Compartments→Compartmen). Reads of OUR outputs must restore the canonical
# names or downstream filters (GeoJSON keep-list, legends, composer labels)
# silently drop the data.
_SHP_TRUNCATED = {
    "Slope_Rang": "Slope_Range",
    "Descriptio": "Description",
    "Compartmen": "Compartments",
}


def restore_shp_cols(gdf):
    """Rename laundered DBF columns back to canonical (no-op if absent)."""
    if gdf is None or getattr(gdf, "empty", True):
        return gdf
    try:
        ren = {c: _SHP_TRUNCATED[c] for c in gdf.columns
               if c in _SHP_TRUNCATED and _SHP_TRUNCATED[c] not in gdf.columns}
        if ren:
            return gdf.rename(columns=ren)
    except Exception:
        pass
    return gdf


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
