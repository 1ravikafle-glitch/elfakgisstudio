"""Elfak GIS Studio — shared constants & paths (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")


def _project_root():
    """Nearest ancestor containing app.py + templates/.

    Data dirs MUST be absolute: Flask's send_from_directory resolves relative
    paths against app.root_path (the elfakgis/ package dir since the split),
    while all file writes use the process CWD. Absolute anchored paths keep
    reads and writes in the same place on every server/gunicorn layout.
    """
    d = os.path.dirname(os.path.abspath(__file__))
    for _ in range(5):
        if (os.path.isfile(os.path.join(d, "app.py"))
                and os.path.isdir(os.path.join(d, "templates"))):
            return d
        d = os.path.dirname(d)
    return os.getcwd()


_PROJECT_ROOT = _project_root()

UPLOAD = os.path.join(_PROJECT_ROOT, "uploads")
OUTPUT = os.path.join(_PROJECT_ROOT, "outputs")
USERS_FILE = os.path.join(_PROJECT_ROOT, "users.json")
_dem = os.environ.get("DEM_CATALOG_DIR", "dem_catalog")
DEM_CATALOG_DIR = _dem if os.path.isabs(_dem) else os.path.join(_PROJECT_ROOT, _dem)
GITHUB_DEM_BASE = os.environ.get(
    "GITHUB_DEM_BASE",
    # The DEM catalog lives in this repo. It used to point at a sibling
    # "ElfakGISProStudio" that no longer resolves (404), which failed
    # /thesis_options with a 500. Both env vars still override this.
    "https://raw.githubusercontent.com/1ravikafle-glitch/elfakgisstudio/main/dem_catalog"
)
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()
DEM_CACHE_DIR = os.path.join(UPLOAD, "dem_cache")
NEPAL_DIR = os.environ.get("NEPAL_DIR", os.path.join(_PROJECT_ROOT, "data", "nepal"))
# Thesis Map base: pre-dissolved 7 provinces + 77 districts, geographic
# (Everest datum). Rendered layers are reprojected to the run's UTM zone, so
# maps always follow the app's 44N/45N/… setting. local_unit.shp (777 units)
# stays as the offline fallback source (regenerates the two files above).
NEPAL_BASE_SHP = os.path.join(NEPAL_DIR, "local_unit.shp")
NEPAL_PROVINCES_SHP = os.path.join(NEPAL_DIR, "provinces.shp")
NEPAL_DISTRICTS_SHP = os.path.join(NEPAL_DIR, "districts.shp")
NEPAL_WARDS_SHP = os.path.join(NEPAL_DIR, "NEPAL_WARDS.shp")  # legacy
for _d in (UPLOAD, OUTPUT, DEM_CATALOG_DIR, DEM_CACHE_DIR):
    os.makedirs(_d, exist_ok=True)

# Thesis Map (Group I) — official province names by STATE_CODE.
PROVINCE_NAMES = {
    1: "Koshi",
    2: "Madhesh",
    3: "Bagmati",
    4: "Gandaki",
    5: "Lumbini",
    6: "Karnali",
    7: "Sudurpashchim",
}

FIG_W, FIG_H, DPI = 8.27, 11.69, 300  # A4 portrait, 300 DPI
# Group H renders 6 figures x 3 formats on a 512MB instance: PNG previews at
# 150 DPI (1500px wide — crisp on screen, 4x fewer raster bytes) while the
# PDF/SVG twins stay fully vector for print.
H_PREVIEW_DPI = int(os.environ.get("H_PREVIEW_DPI", "150"))
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
