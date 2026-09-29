"""Elfak GIS Studio — Group C sample-plot generator (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.geo.geom import _enforce_poly_gdf, normalize_order, read_input, safe_col, safe_polygon

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
# GROUP C – SAMPLE PLOT GENERATOR
# ----------------------------------------------------------------------
import tempfile
import zipfile
import shutil
import traceback

def group_c(file, crs, w, h, rows, cols, out, mode, mapping=None, base_name="boundary", run_id=None):
    """
    Process boundary file (CSV/Excel or ZIP shapefile) and generate grid points.
    """
    polygons = []
    is_zip = file.filename.lower().endswith(".zip")

    if is_zip:
        tmp_dir = tempfile.mkdtemp(prefix="group_c_")
        zip_path = os.path.join(tmp_dir, "upload.zip")
        try:
            # Save uploaded ZIP to temp
            file.save(zip_path)

            # Extract
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(tmp_dir)

            # Find all .shp files
            shps = []
            for root, _, files in os.walk(tmp_dir):
                for fname in files:
                    if fname.lower().endswith(".shp"):
                        shps.append(os.path.join(root, fname))

            if not shps:
                raise ValueError("No .shp file found in the uploaded ZIP.")

            # Choose target shapefile (if specified)
            sp = shps[0]
            ts = (mapping or {}).get("target_shp")
            if ts:
                for s in shps:
                    if os.path.basename(s) == os.path.basename(ts):
                        sp = s
                        break

            # Read shapefile
            try:
                gdf = gpd.read_file(sp)
            except Exception as e:
                raise ValueError(f"Failed to read shapefile: {e}")

            if gdf.empty:
                raise ValueError("Shapefile is empty.")

            # Ensure CRS
            if gdf.crs is None:
                gdf = gdf.set_crs(crs)
            else:
                try:
                    gdf = gdf.to_crs(crs)
                except Exception as e:
                    # Fallback: use the user-provided CRS
                    gdf = gdf.set_crs(crs)
                    log.warning(f"CRS conversion failed, set to {crs}: {e}")

            # Collect valid polygons
            for geom in gdf.geometry:
                if geom is None or geom.is_empty:
                    continue
                if geom.geom_type == "Polygon":
                    polygons.append(geom if geom.is_valid else geom.buffer(0))
                elif geom.geom_type == "MultiPolygon":
                    for p in geom.geoms:
                        polygons.append(p if p.is_valid else p.buffer(0))

            if not polygons:
                raise ValueError("No valid polygon geometries found in shapefile.")

        except Exception as e:
            # Log the full traceback
            log.error(f"Group C ZIP processing error: {traceback.format_exc()}")
            raise ValueError(f"ZIP processing failed: {e}")
        finally:
            # Clean up temp directory
            shutil.rmtree(tmp_dir, ignore_errors=True)

    else:
        # CSV/Excel handling (unchanged)
        df = read_input(file)
        df = normalize_order(df)
        xc = safe_col(df, mapping, "X", "X")
        yc = safe_col(df, mapping, "Y", "Y")
        oc = safe_col(df, mapping, "Order", "Order")

        if not xc or not yc:
            raise ValueError("X/Y columns not found.")

        if mode == "A":
            if oc:
                df = df.sort_values(oc)
            coords = list(zip(df[xc], df[yc]))
            coords.append(coords[0])
            polygons = [safe_polygon(coords)]
        else:  # mode B – segmented
            fc = safe_col(df, mapping, "Forest", "Forest")
            cc = safe_col(df, mapping, "Compartment", "Compartment")
            if not fc:
                raise ValueError("Forest column required for segmented mode.")
            gkeys = [fc, cc] if cc else [fc]
            for _, g in df.groupby(gkeys):
                if oc:
                    g = g.sort_values(oc)
                coords = list(zip(g[xc], g[yc]))
                if len(coords) < 3:
                    continue
                coords.append(coords[0])
                polygons.append(safe_polygon(coords))
            if not polygons:
                raise ValueError("No valid polygons could be constructed from the data.")

    if not polygons:
        raise ValueError("No valid polygons from input.")

    # Build GeoDataFrames
    p_gdf = gpd.GeoDataFrame([{"geometry": p} for p in polygons], crs=crs)
    l_gdf = gpd.GeoDataFrame([{"geometry": LineString(p.exterior.coords)} for p in polygons], crs=crs)

    # Union for point‑in‑polygon tests
    union = p_gdf.unary_union
    minx, miny, _, _ = union.bounds

    pts = []
    sn = 1
    for ri in range(rows):
        for ci in range(cols):
            center = Point(minx + ci * w + w / 2, miny + ri * h + h / 2)
            if union.contains(center):
                pts.append({"SN": sn, "X": center.x, "Y": center.y, "geometry": center})
                sn += 1

    pt_gdf = gpd.GeoDataFrame(pts, crs=crs)

    # Save outputs
    p_gdf = _enforce_poly_gdf(p_gdf)
    if not p_gdf.empty:
        p_gdf.to_file(os.path.join(out, f"{base_name}_polygon.shp"))
    l_gdf.to_file(os.path.join(out, f"{base_name}_line.shp"))
    if not pt_gdf.empty:
        pt_gdf.to_file(os.path.join(out, f"{base_name}_point.shp"))
        pd.DataFrame(pts)[["SN", "X", "Y"]].to_excel(os.path.join(out, f"{base_name}_sampleplot.xlsx"), index=False)

    return p_gdf, l_gdf, pt_gdf
# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
