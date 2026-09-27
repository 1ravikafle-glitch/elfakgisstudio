"""ElfakGISProStudio — Group B segmented forest (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.geo.geom import _enforce_poly_gdf, normalize_order, safe_col, safe_polygon

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
# GROUP B – SEGMENTED FOREST
# ----------------------------------------------------------------------

def group_b(df, crs, out, mapping=None):
    df = normalize_order(df)
    xc=safe_col(df,mapping,"X","X"); yc=safe_col(df,mapping,"Y","Y")
    oc=safe_col(df,mapping,"Order","Order"); fc=safe_col(df,mapping,"Forest","Forest")
    cc=safe_col(df,mapping,"Compartment","Compartment")
    if not xc: raise ValueError("X column not found.")
    if not yc: raise ValueError("Y column not found.")
    if not fc: raise ValueError("Forest column not found.")
    polys,lines,pts=[],[],[]
    for f,g in df.groupby(fc):
        if oc: g=g.sort_values(oc)
        subs = g.groupby(cc) if cc else [(None,g)]
        for c,cg in subs:
            coords = list(zip(cg[xc],cg[yc]))
            if len(coords)<3: continue
            coords.append(coords[0]); poly=safe_polygon(coords)
            polys.append({"Forest":f,"Compartment":c,"Area_ha":round(poly.area/10000,4),"Perim_m":round(poly.length,2),"geometry":poly})
            lines.append({"Forest":f,"Compartment":c,"geometry":LineString(coords)})
            for _,r in cg.iterrows():
                pts.append({"Forest":f,"Compartment":c,"Order":r[oc] if oc else None,"geometry":Point(r[xc],r[yc])})
    p=gpd.GeoDataFrame(polys,crs=crs); l=gpd.GeoDataFrame(lines,crs=crs); pt=gpd.GeoDataFrame(pts,crs=crs)
    p=_enforce_poly_gdf(p)
    if not p.empty: p.to_file(os.path.join(out,"forest_polygon.shp"))
    if not l.empty: l.to_file(os.path.join(out,"forest_line.shp"))
    if not pt.empty: pt.to_file(os.path.join(out,"forest_point.shp"))
    return p,l,pt

# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
