"""ElfakGISProStudio — Group A boundary-whole (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.geo.geom import _safe_dn, normalize_order, safe_col, safe_polygon

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
# GROUP A – BOUNDARY WHOLE
# ----------------------------------------------------------------------

def group_a(df, forest, crs, out, mapping=None):
    df = normalize_order(df)
    xc = safe_col(df,mapping,"X","X"); yc = safe_col(df,mapping,"Y","Y")
    oc = safe_col(df,mapping,"Order","Order")
    if not xc: raise ValueError("X/Easting column not found.")
    if not yc: raise ValueError("Y/Northing column not found.")
    if oc: df = df.sort_values(oc)
    coords = list(zip(df[xc], df[yc]))
    if len(coords) < 3: raise ValueError("Need ≥3 points for a polygon.")
    coords.append(coords[0])
    poly = safe_polygon(coords); line = LineString(coords)
    ah = round(poly.area/10000, 4); pfx = _safe_dn(forest)
    pg = gpd.GeoDataFrame([{"Forest":forest,"Area_ha":ah,"Perim_m":round(poly.length,2),"geometry":poly}],crs=crs)
    lg = gpd.GeoDataFrame([{"Forest":forest,"geometry":line}],crs=crs)
    ptg = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df[xc],df[yc]), crs=crs)
    pg.to_file(os.path.join(out,f"{pfx}_polygon.shp"))
    lg.to_file(os.path.join(out,f"{pfx}_line.shp"))
    ptg.to_file(os.path.join(out,f"{pfx}_point.shp"))
    return pg, lg, ptg

# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
