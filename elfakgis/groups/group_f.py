"""Elfak GIS Studio — Group F slope analysis (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.core.config import UPLOAD
from elfakgis.core.store import _prog
from elfakgis.geo.geom import (_CA, _FA, _as_poly, _close_poly, _enforce_poly_gdf,
    _find_col, _repair, _safe_dn, normalize_order, read_input, safe_col, safe_polygon)

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

try:
    import rasterio
    from rasterio.mask import mask as rio_mask
    from rasterio.features import shapes as rio_shapes
    from rasterio.plot import show
    import scipy.ndimage as ndi
    _HAS_RASTERIO = True
except ImportError:
    _HAS_RASTERIO = False
# GROUP F – SLOPE ANALYSIS
# ----------------------------------------------------------------------
# ----------------------------------------------------------------------
# Helper functions for Group F (add these before group_f if not already present)
# ----------------------------------------------------------------------

def _bnd_from_df(df, mapping):
    """Build boundary polygon from DataFrame (CSV/Excel) using X/Y columns."""
    df = normalize_order(df)
    xc = safe_col(df, mapping, "X", "X")
    yc = safe_col(df, mapping, "Y", "Y")
    oc = safe_col(df, mapping, "Order", "Order")
    if not xc:
        raise ValueError("X column not found.")
    if not yc:
        raise ValueError("Y column not found.")
    if oc:
        df = df.sort_values(oc)
    coords = list(zip(df[xc], df[yc]))
    if len(coords) < 3:
        raise ValueError("Need ≥3 boundary points.")
    coords.append(coords[0])
    return safe_polygon(coords)

def _bnd_from_zip(zip_file, target_shp, src_crs, dem_crs):
    """
    Extract boundary polygon from a ZIP containing a shapefile.
    Returns (boundary_polygon, boundary_gdf, basename).
    """
    import io, zipfile, tempfile
    zip_bytes = zip_file.read()
    if len(zip_bytes) < 100:
        raise ValueError("Uploaded ZIP file is empty or too small.")
    tmp_dir = os.path.join(UPLOAD, "bnd_tmp_" + uuid.uuid4().hex[:8])
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp_dir)
        shps = []
        for root, _, files in os.walk(tmp_dir):
            for f in files:
                if f.lower().endswith(".shp"):
                    shps.append(os.path.join(root, f))
        if not shps:
            raise ValueError("No .shp file found inside the uploaded ZIP.")
        if target_shp:
            target_base = os.path.basename(target_shp)
            chosen = None
            for s in shps:
                if os.path.basename(s) == target_base:
                    chosen = s
                    break
            if chosen is None:
                raise ValueError(f"Specified shapefile '{target_shp}' not found in ZIP.")
        else:
            chosen = shps[0]
        shp_basename = os.path.splitext(os.path.basename(chosen))[0]
        gdf = gpd.read_file(chosen)
        if gdf.empty:
            raise ValueError("Boundary shapefile is empty.")
        if gdf.crs is None:
            gdf = gdf.set_crs(src_crs)
        else:
            gdf = gdf.to_crs(dem_crs)
        union = _repair(gdf.unary_union)
        if union is None or union.is_empty:
            raise ValueError("Boundary geometry is empty after union.")
        return union, gdf, shp_basename
    except Exception as e:
        raise ValueError(f"Failed to process boundary ZIP: {e}")
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


# ----------------------------------------------------------------------
# GROUP F – SLOPE ANALYSIS (complete, corrected version)
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Helper functions for Group F (add these before group_f if not already present)
# ----------------------------------------------------------------------

# ----------------------------------------------------------------------
# Helper functions for Group F (add these before group_f if not already present)
# ----------------------------------------------------------------------

def _bnd_from_df(df, mapping):
    """Build boundary polygon from DataFrame (CSV/Excel) using X/Y columns."""
    df = normalize_order(df)
    xc = safe_col(df, mapping, "X", "X")
    yc = safe_col(df, mapping, "Y", "Y")
    oc = safe_col(df, mapping, "Order", "Order")
    if not xc:
        raise ValueError("X column not found.")
    if not yc:
        raise ValueError("Y column not found.")
    if oc:
        df = df.sort_values(oc)
    coords = list(zip(df[xc], df[yc]))
    if len(coords) < 3:
        raise ValueError("Need ≥3 boundary points.")
    coords.append(coords[0])
    return safe_polygon(coords)

def _bnd_from_zip(zip_file, target_shp, src_crs, dem_crs):
    """
    Extract boundary polygon from a ZIP containing a shapefile.
    Returns (boundary_polygon, boundary_gdf, basename).
    """
    import io, zipfile, tempfile
    zip_bytes = zip_file.read()
    if len(zip_bytes) < 100:
        raise ValueError("Uploaded ZIP file is empty or too small.")
    tmp_dir = os.path.join(UPLOAD, "bnd_tmp_" + uuid.uuid4().hex[:8])
    os.makedirs(tmp_dir, exist_ok=True)
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            zf.extractall(tmp_dir)
        shps = []
        for root, _, files in os.walk(tmp_dir):
            for f in files:
                if f.lower().endswith(".shp"):
                    shps.append(os.path.join(root, f))
        if not shps:
            raise ValueError("No .shp file found inside the uploaded ZIP.")
        if target_shp:
            target_base = os.path.basename(target_shp)
            chosen = None
            for s in shps:
                if os.path.basename(s) == target_base:
                    chosen = s
                    break
            if chosen is None:
                raise ValueError(f"Specified shapefile '{target_shp}' not found in ZIP.")
        else:
            chosen = shps[0]
        shp_basename = os.path.splitext(os.path.basename(chosen))[0]
        gdf = gpd.read_file(chosen)
        if gdf.empty:
            raise ValueError("Boundary shapefile is empty.")
        if gdf.crs is None:
            gdf = gdf.set_crs(src_crs)
        else:
            gdf = gdf.to_crs(dem_crs)
        union = _repair(gdf.unary_union)
        if union is None or union.is_empty:
            raise ValueError("Boundary geometry is empty after union.")
        return union, gdf, shp_basename
    except Exception as e:
        raise ValueError(f"Failed to process boundary ZIP: {e}")
    finally:
        try:
            shutil.rmtree(tmp_dir)
        except Exception:
            pass


# ----------------------------------------------------------------------
# GROUP F – SLOPE ANALYSIS (FINAL CORRECTED VERSION)
# ----------------------------------------------------------------------

def group_f(boundary_file, dem_file, crs, out, mapping=None,
            boundary_is_zip=False, forest_name="FOREST",
            f_mode="A", comp_col_name=None, field_area_ha=None, run_id=None):
    """
    Slope analysis with raster-to-polygon pipeline.
    Area_ha is the raw polygon area from raster-to-polygon after clipping to the compartment.
    No buffer is applied during clipping, ensuring the three classes partition the compartment exactly.
    If field_area_ha is provided, Recal_ha and Cal_Factor columns are added (Area_ha remains raw).
    Returns: (summary_rows, vector_gdf, boundary_gdf, f_mode, per_group)
    """
    if not _HAS_RASTERIO:
        raise ValueError("rasterio and scipy are not installed.")

    os.makedirs(out, exist_ok=True)
    pfx = _safe_dn(forest_name)
    SN = -9999.0

    if run_id:
        _prog(run_id, "Step 0 — Loading DEM…", 5)

    # Save uploaded DEM to disk
    dem_path = os.path.join(UPLOAD, f"{uuid.uuid4()}_dem.tif")
    try:
        dem_file.save(dem_path)
    except Exception as e:
        raise ValueError(f"Could not save DEM file: {e}")
    if not os.path.exists(dem_path) or os.path.getsize(dem_path) < 100:
        raise ValueError("DEM file is empty or could not be written to disk.")

    with rasterio.open(dem_path) as _src:
        dem_crs = _src.crs
        dem_nodata = _src.nodata
        dem_profile = _src.profile.copy()

    if run_id:
        _prog(run_id, "Step 1 — Loading & reprojecting boundary…", 10)

    # --- Load boundary polygon (either from ZIP shapefile or from CSV/Excel) ---
    if boundary_is_zip:
        ts = (mapping or {}).get("target_shp")
        bpoly, bgdf, shp_basename = _bnd_from_zip(boundary_file, ts, crs, str(dem_crs))
        if forest_name in (None, "", "FOREST"):
            forest_name = shp_basename
            pfx = _safe_dn(forest_name)
    else:
        try:
            df = read_input(boundary_file)
            bpoly = _bnd_from_df(df, mapping)
            bgdf = gpd.GeoDataFrame(
                [{"Forest": forest_name, "geometry": bpoly}], crs=crs
            ).to_crs(str(dem_crs))
            bpoly = bgdf.unary_union
        except Exception as e:
            raise ValueError(f"Failed to read/project boundary: {e}")

    if bpoly is None or bpoly.is_empty:
        raise ValueError("Boundary polygon is empty after loading.")

    # --- Prepare rectangular DEM clip (20% buffer) ---
    b = bpoly.bounds
    bx = (b[2] - b[0]) * 0.20
    by = (b[3] - b[1]) * 0.20
    rect_poly = _sbox(b[0] - bx, b[1] - by, b[2] + bx, b[3] + by)

    nd = dem_nodata if dem_nodata is not None else SN

    if run_id:
        _prog(run_id, "Step 1 — Clipping rectangular DEM (20% buffer)…", 18)

    # Clip DEM to rectangle
    try:
        with rasterio.open(dem_path) as _src:
            ra, rt = rio_mask(
                _src, [rect_poly.__geo_interface__],
                crop=True, filled=True, nodata=nd, all_touched=False
            )
        rdm = ra[0].astype(np.float32)
        rdm[rdm == nd] = np.nan
    except Exception as e:
        log.warning(f"Rect clip failed ({e}), reading full DEM")
        with rasterio.open(dem_path) as _src:
            ra = _src.read(1).astype(np.float32)
            rt = _src.transform
        nd_val = nd if nd is not None else SN
        rdm = ra.copy()
        rdm[rdm == nd_val] = np.nan

    rx = abs(rt.a)
    ry = abs(rt.e)
    if rx < 0.01 or ry < 0.01:
        raise ValueError(f"DEM pixel size is too small ({rx}m × {ry}m). Check DEM is in a projected CRS (e.g. UTM).")

    if run_id:
        _prog(run_id, "Step 2 — Computing slope (Horn method)…", 28)

    valid = ~np.isnan(rdm)
    if not valid.any():
        raise ValueError("DEM has no valid elevation data inside the boundary+buffer rectangle.")

    # Fill NoData holes using nearest neighbour
    if (~valid).any():
        ind = ndi.distance_transform_edt(
            ~valid, return_distances=False, return_indices=True
        )
        filled = rdm[tuple(ind)]
    else:
        filled = rdm.copy()

    # Slope calculation
    dzdx = ndi.sobel(filled, axis=1) / (8.0 * rx)
    dzdy = ndi.sobel(filled, axis=0) / (8.0 * ry)
    slope = np.degrees(np.arctan(np.sqrt(dzdx**2 + dzdy**2))).astype(np.float32)

    # Mask outer pixels (edge effect)
    outer_valid = np.ones_like(valid, dtype=bool)
    outer_valid[0, :] = False
    outer_valid[-1, :] = False
    outer_valid[:, 0] = False
    outer_valid[:, -1] = False
    slope[~outer_valid] = SN

    # Save rectangular slope raster
    rp = dem_profile.copy()
    rp.update(dtype="float32", nodata=SN, count=1,
              height=slope.shape[0], width=slope.shape[1], transform=rt)
    sp_path = os.path.join(out, f"{pfx}_slope_rect.tif")
    with rasterio.open(sp_path, "w", **rp) as dst:
        dst.write(slope, 1)

    if run_id:
        _prog(run_id, f"Step 2 — Slope raster saved ({slope.shape[1]}×{slope.shape[0]}px)", 35)

    # --- Reclassify ---
    if run_id:
        _prog(run_id, "Step 3 — Reclassifying slope into 3 classes…", 40)

    vm = (slope != SN) & ~np.isnan(slope)
    cls = np.zeros_like(slope, dtype=np.uint8)
    cls[vm & (slope < 19)] = 1
    cls[vm & (slope >= 19) & (slope <= 31)] = 2
    cls[vm & (slope > 31)] = 3

    cp = rp.copy()
    cp.update(dtype="uint8", nodata=0)
    cls_path = os.path.join(out, f"{pfx}_class_rect.tif")
    with rasterio.open(cls_path, "w", **cp) as dst:
        dst.write(cls, 1)

    # --- Raster to polygon ---
    if run_id:
        _prog(run_id, "Step 4 — Vectorising classified raster…", 50)

    rtp = []
    with rasterio.open(cls_path) as _src:
        ca, ct = _src.read(1), _src.transform
        mask_valid = (ca > 0).astype(np.uint8)
        for shp, val in rio_shapes(ca, mask=mask_valid, transform=ct):
            cid = int(val)
            if cid == 0:
                continue
            try:
                coords = shp["coordinates"]
                ext = coords[0]
                holes = coords[1:] if len(coords) > 1 else None
                geom = _repair(Polygon(ext, holes=holes))
                if geom and not geom.is_empty and geom.area > 1e-10:
                    rtp.append({"gridcode": cid, "geometry": geom})
            except Exception:
                continue

    if not rtp:
        raise ValueError("Raster-to-polygon produced no features. Check DEM overlap and CRS.")

    rtp_gdf = gpd.GeoDataFrame(rtp, crs=str(dem_crs))
    _rtp_save = _enforce_poly_gdf(rtp_gdf)
    if not _rtp_save.empty:
        _rtp_save.to_file(os.path.join(out, f"{pfx}_rtp_raw.shp"))

    if run_id:
        _prog(run_id, f"Step 4 — {len(rtp)} slope polygons vectorised", 56)

    # --- Dissolve by gridcode ---
    if run_id:
        _prog(run_id, "Step 5 — Dissolving by gridcode…", 62)

    try:
        dissolved = rtp_gdf.dissolve(by="gridcode", as_index=False)
        dissolved["gridcode"] = dissolved["gridcode"].astype(int)
    except Exception as e:
        log.warning(f"dissolve failed ({e}), using raw rtp")
        dissolved = rtp_gdf.copy()

    _dis_save = _enforce_poly_gdf(dissolved)
    if not _dis_save.empty:
        _dis_save.to_file(os.path.join(out, f"{pfx}_rtp_dissolved.shp"))

    if run_id:
        _prog(run_id, f"Step 5 — Dissolved into {len(dissolved)} gridcode classes", 66)

    # --- Determine compartments to clip to ---
    if run_id:
        _prog(run_id, "Step 6 — Clipping dissolved polygons to boundary…", 70)

    class_defs = {
        1: ("0-19 degree",  "Gentle",   "#2e8b57"),
        2: ("19-31 degree", "Moderate", "#ffd700"),
        3: (">31 degree",   "Steep",    "#ef4444"),
    }

    comp_polygons = []
    if f_mode == "A":
        comp_polygons = [(forest_name, bpoly)]
    else:
        # Find compartment column in bgdf
        grp_col = None
        if comp_col_name:
            for c in bgdf.columns:
                if c.lower() == comp_col_name.lower():
                    grp_col = c
                    break
        if grp_col is None:
            grp_col = _find_col(bgdf, _FA if f_mode == "B" else _CA)
        if grp_col:
            for val, grp in bgdf.groupby(grp_col):
                up = _repair(grp.unary_union)
                if up and not up.is_empty:
                    comp_polygons.append((str(val), up))
        if not comp_polygons:
            comp_polygons = [(forest_name, bpoly)]

    # --- Clip dissolved polygons to each compartment ---
    all_sum = []   # flattened list of rows (for summary Excel)
    per_grp = {}   # dict: label -> list of rows for that compartment
    all_vec = []   # list of geometry records for shapefile

    for i, (label, clip_poly) in enumerate(comp_polygons):
        if run_id:
            pct = 70 + int(16 * i / max(len(comp_polygons), 1))
            _prog(run_id, f"Step 6 — Clipping {label} ({i+1}/{len(comp_polygons)})…", pct)

        vrecs = []
        for _, drow in dissolved.iterrows():
            cid = int(drow["gridcode"])
            dgeom = _repair(drow.geometry)
            if dgeom is None or dgeom.is_empty:
                continue
            if cid not in class_defs:
                continue
            try:
                # --- FIX: removed buffer to avoid double-counting ---
                clipped = _repair(dgeom.intersection(clip_poly))
            except Exception:
                continue
            if clipped is None or clipped.is_empty:
                continue

            # Ensure polygon/multipolygon
            if clipped.geom_type == "Polygon":
                pg = clipped if clipped.is_valid else _repair(clipped)
            elif clipped.geom_type == "MultiPolygon":
                valid_parts = [p for p in clipped.geoms if p and not p.is_empty and p.area > 1e-10]
                if not valid_parts:
                    continue
                pg = MultiPolygon(valid_parts) if len(valid_parts) > 1 else valid_parts[0]
            elif hasattr(clipped, "geoms"):
                parts = []
                for g in clipped.geoms:
                    if g.geom_type == "Polygon" and not g.is_empty and g.area > 1e-10:
                        parts.append(g)
                    elif g.geom_type == "MultiPolygon":
                        parts.extend([p for p in g.geoms if not p.is_empty and p.area > 1e-10])
                if not parts:
                    continue
                pg = MultiPolygon(parts) if len(parts) > 1 else parts[0]
            else:
                pg = _as_poly(clipped)

            if pg is None or pg.is_empty or pg.area < 1e-10:
                continue

            # Close polygons
            if pg.geom_type == "Polygon":
                pg = _close_poly(pg)
            elif pg.geom_type == "MultiPolygon":
                fixed_parts = []
                for part in pg.geoms:
                    cp = _close_poly(part)
                    if cp and cp.geom_type == "Polygon" and not cp.is_empty:
                        fixed_parts.append(cp)
                if fixed_parts:
                    pg = MultiPolygon(fixed_parts) if len(fixed_parts) > 1 else fixed_parts[0]

            ah = round(pg.area / 10000, 4)
            vrecs.append({
                "Label":       label,
                "Class":       cid,
                "Slope_Range": class_defs[cid][0],
                "Description": class_defs[cid][1],
                "Area_ha":     ah,
                "geometry":    pg,
            })

        # Store raw vrecs for this compartment
        per_grp[label] = vrecs
        all_sum.extend(vrecs)
        all_vec.extend(vrecs)

    # --- Check if we got any results ---
    if not all_sum:
        raise ValueError("No slope polygons were generated. Check that the boundary overlaps the DEM and that the CRS is correct.")

    # --- Now we have all raw areas in all_sum. Apply recalibration if requested ---
    cal_factor = 1.0
    total_raw_all = sum(r["Area_ha"] for r in all_sum)
    if field_area_ha is not None and field_area_ha > 0 and total_raw_all > 1e-9:
        cal_factor = field_area_ha / total_raw_all
        # Add Recal_ha and Cal_Factor to each row (Area_ha stays raw)
        for r in all_sum:
            r["Recal_ha"] = round(r["Area_ha"] * cal_factor, 4)
            r["Cal_Factor"] = round(cal_factor, 6)
    else:
        # No recalibration: ensure these columns are absent
        for r in all_sum:
            r.pop("Recal_ha", None)
            r.pop("Cal_Factor", None)

    # --- Now rebuild the vector GeoDataFrame from all_vec (raw areas) ---
    vcrs = str(dem_crs)
    if all_vec:
        vgdf = gpd.GeoDataFrame(all_vec, crs=vcrs)
        vgdf = _enforce_poly_gdf(vgdf)
        if not vgdf.empty:
            vgdf.to_file(os.path.join(out, f"{pfx}_slope_polygon.shp"))
    else:
        vgdf = gpd.GeoDataFrame(columns=["Label","Class","Slope_Range",
                                          "Description","Area_ha","geometry"], crs=vcrs)

    # --- Save boundary shapefile ---
    try:
        bgdf_save = _enforce_poly_gdf(bgdf)
        if not bgdf_save.empty:
            bgdf_save.to_file(os.path.join(out, f"{pfx}_boundary_polygon.shp"))
    except Exception as e:
        log.warning(f"bgdf save: {e}")

    # --- Save clipped slope and class rasters for the first/main polygon (optional) ---
    try:
        main_poly = comp_polygons[0][1]
        with rasterio.open(sp_path) as _src:
            fc2, ft2 = rio_mask(
                _src, [main_poly.__geo_interface__],
                crop=True, filled=True, nodata=SN, all_touched=False
            )
        fc2 = fc2[0].astype(np.float32)
        cp2_ = rp.copy()
        cp2_.update(height=fc2.shape[0], width=fc2.shape[1], transform=ft2)
        with rasterio.open(os.path.join(out, f"{pfx}_slope_clipped.tif"), "w", **cp2_) as dst:
            dst.write(fc2, 1)
        # Reclassify clipped
        vm2 = (fc2 != SN) & ~np.isnan(fc2)
        ca2 = np.zeros_like(fc2, dtype=np.uint8)
        ca2[vm2 & (fc2 < 19)] = 1
        ca2[vm2 & (fc2 >= 19) & (fc2 <= 31)] = 2
        ca2[vm2 & (fc2 > 31)] = 3
        cp3_ = cp2_.copy()
        cp3_.update(dtype="uint8", nodata=0)
        with rasterio.open(os.path.join(out, f"{pfx}_slope_classes.tif"), "w", **cp3_) as dst:
            dst.write(ca2, 1)
    except Exception as e:
        log.warning(f"Clipped raster save error: {e}")

    # --- Prepare final DataFrame for Excel ---
    # Compute per-compartment totals for raw Area_ha (so Total_ha = sum of raw areas)
    comp_totals = {}
    for r in all_sum:
        lab = r["Label"]
        comp_totals[lab] = comp_totals.get(lab, 0) + r["Area_ha"]

    # Add Total_ha and Pct_Area based on raw areas
    for r in all_sum:
        lab = r["Label"]
        total_raw_comp = comp_totals[lab]
        r["Total_ha"] = round(total_raw_comp, 4)
        r["Pct_Area"] = round(r["Area_ha"] / total_raw_comp * 100, 2) if total_raw_comp > 0 else 0

    # Determine columns based on whether recalibration was applied
    if field_area_ha is not None and field_area_ha > 0 and total_raw_all > 1e-9:
        cols = ["Label", "Class", "Slope_Range", "Description",
                "Area_ha", "Recal_ha", "Cal_Factor", "Pct_Area", "Total_ha"]
    else:
        cols = ["Label", "Class", "Slope_Range", "Description",
                "Area_ha", "Pct_Area", "Total_ha"]

    # Create DataFrame and ensure all columns exist
    df_excel = pd.DataFrame(all_sum)
    existing_cols = [c for c in cols if c in df_excel.columns]
    df_excel = df_excel[existing_cols]

    # Save Excel
    ep = os.path.join(out, f"{pfx}_slope_summary.xlsx")
    if f_mode == "A":
        # Single group: remove Label column if it's redundant
        df_excel.drop(columns=["Label"], errors="ignore").to_excel(ep, index=False)
    else:
        # Multi-compartment: save all data in one sheet, and per-compartment sheets
        try:
            with pd.ExcelWriter(ep, engine="openpyxl") as wr:
                df_excel.to_excel(wr, sheet_name="All_Groups", index=False)
                for lb, grs in per_grp.items():
                    if not grs:
                        continue
                    # Build per-compartment DataFrame with same columns
                    comp_raw = sum(r["Area_ha"] for r in grs)
                    rows = []
                    for r in grs:
                        row = r.copy()
                        row["Total_ha"] = round(comp_raw, 4)
                        row["Pct_Area"] = round(r["Area_ha"] / comp_raw * 100, 2) if comp_raw > 0 else 0
                        if field_area_ha is not None and field_area_ha > 0 and total_raw_all > 1e-9:
                            row["Recal_ha"] = round(r["Area_ha"] * cal_factor, 4)
                            row["Cal_Factor"] = round(cal_factor, 6)
                            cols_per = ["Class", "Slope_Range", "Description",
                                        "Area_ha", "Recal_ha", "Cal_Factor",
                                        "Pct_Area", "Total_ha"]
                        else:
                            cols_per = ["Class", "Slope_Range", "Description",
                                        "Area_ha", "Pct_Area", "Total_ha"]
                        # Ensure all columns exist
                        rows.append({k: row.get(k, None) for k in cols_per})
                    df_comp = pd.DataFrame(rows)
                    df_comp = df_comp.dropna(axis=1, how='all')
                    df_comp.to_excel(wr, sheet_name=str(lb)[:31], index=False)
        except Exception as e:
            log.warning(f"Excel multi-sheet error ({e}), saving flat")
            df_excel.to_excel(ep, index=False)

    if run_id:
        _prog(run_id, "Group F complete.", 95)

    # Return summary, vector GDF, boundary GDF, mode, per-group dict
    return all_sum, vgdf, bgdf, f_mode, per_grp

# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
