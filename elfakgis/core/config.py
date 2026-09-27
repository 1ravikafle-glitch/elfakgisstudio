"""ElfakGISProStudio — shared constants & paths (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
UPLOAD, OUTPUT, USERS_FILE = "uploads", "outputs", "users.json"
DEM_CATALOG_DIR = os.environ.get("DEM_CATALOG_DIR", "dem_catalog")
GITHUB_DEM_BASE = os.environ.get(
    "GITHUB_DEM_BASE",
    "https://raw.githubusercontent.com/1ravikafle-glitch/ElfakGISProStudio/main/dem_catalog"
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
DEM_CACHE_DIR = os.path.join(UPLOAD, "dem_cache")
for _d in (UPLOAD, OUTPUT, DEM_CATALOG_DIR, DEM_CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

FIG_W, FIG_H, DPI = 8.27, 11.69, 300  # A4 portrait, 300 DPI
EPS = 1e-6
DEFAULT_PADDING = 0.02

# ── Export map style constants ──────────────────────────────
MAP_BG     = "white"
POLY_COLOR = "#0000DD"
POLY_LW    = 2.0
POINT_COL  = "#FF0000"
POINT_SZ   = 18
LABEL_FS   = 6.5
LABEL_COL  = "#000000"
GRID_COL   = "#aaaaaa"
GRID_LW    = 0.4
TICK_FS    = 7


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
