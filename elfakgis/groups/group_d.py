"""Elfak GIS Studio — Group D multi-forest complex (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from elfakgis.geo.geom import _enforce_poly_gdf, _safe_dn, normalize_order, safe_col, safe_polygon

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
# GROUP D – MULTI-FOREST COMPLEX
# ----------------------------------------------------------------------

def _save_fl(pr,lr,pts,d,crs):
    os.makedirs(d,exist_ok=True); pfx=os.path.basename(d)
    _pdf=_enforce_poly_gdf(gpd.GeoDataFrame([pr],crs=crs))
    if not _pdf.empty: _pdf.to_file(os.path.join(d,f"{pfx}_polygon.shp"))
    gpd.GeoDataFrame([lr],crs=crs).to_file(os.path.join(d,f"{pfx}_line.shp"))
    gpd.GeoDataFrame(pts,crs=crs).to_file(os.path.join(d,f"{pfx}_point.shp"))

def group_d(df, crs, out, mapping=None, mode="A"):
    df=normalize_order(df)
    xc=safe_col(df,mapping,"X","X"); yc=safe_col(df,mapping,"Y","Y")
    oc=safe_col(df,mapping,"Order","Order"); fc=safe_col(df,mapping,"Forest","Forest")
    cc=safe_col(df,mapping,"Compartment","Compartment")
    if not xc: raise ValueError("X column not found.")
    if not yc: raise ValueError("Y column not found.")
    if not fc: raise ValueError("Forest column not found.")
    if mode=="B" and not cc: raise ValueError("Compartment column required.")
    ap,al,apt=[],[],[]
    for f,fg in df.groupby(fc):
        fd=os.path.join(out,_safe_dn(f))
        if mode=="B":
            for c,cg in fg.groupby(cc):
                if oc: cg=cg.sort_values(oc)
                coords=list(zip(cg[xc],cg[yc]))
                if len(coords)<3: continue
                coords.append(coords[0]); poly=safe_polygon(coords)
                pr={"Forest":f,"Compartment":c,"Area_ha":round(poly.area/10000,4),"Perim_m":round(poly.length,4),"geometry":poly}
                lr={"Forest":f,"Compartment":c,"geometry":LineString(coords)}
                ptl=[{"Forest":f,"Compartment":c,"Order":r[oc] if oc else None,"geometry":Point(r[xc],r[yc])} for _,r in cg.iterrows()]
                _save_fl(pr,lr,ptl,os.path.join(fd,_safe_dn(c)),crs)
                ap.append(pr); al.append(lr); apt.extend(ptl)
        else:
            if oc: fg=fg.sort_values(oc)
            coords=list(zip(fg[xc],fg[yc]))
            if len(coords)<3: continue
            coords.append(coords[0]); poly=safe_polygon(coords)
            pr={"Forest":f,"Area_ha":round(poly.area/10000,4),"Perim_m":round(poly.length,4),"geometry":poly}
            lr={"Forest":f,"geometry":LineString(coords)}
            ptl=[{"Forest":f,"Order":r[oc] if oc else None,"geometry":Point(r[xc],r[yc])} for _,r in fg.iterrows()]
            _save_fl(pr,lr,ptl,fd,crs); ap.append(pr); al.append(lr); apt.extend(ptl)
    if not ap: raise ValueError("No valid polygons built.")
    return gpd.GeoDataFrame(ap,crs=crs),gpd.GeoDataFrame(al,crs=crs),gpd.GeoDataFrame(apt,crs=crs)

# ----------------------------------------------------------------------

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
