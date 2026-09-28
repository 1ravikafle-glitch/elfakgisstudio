"""ElfakGISProStudio — pages & SEO routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
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

from flask import Blueprint, current_app
pages_bp = Blueprint('pages_bp', __name__)
@pages_bp.route("/map_editor/<run_id>")
def map_editor(run_id):
    """Standalone map editor page (opened from history or directly)."""
    run_id = _safe_runid(run_id)
    folder = os.path.join(OUTPUT, run_id)
    if not os.path.exists(folder):
        abort(404, "Run not found.")
    map_file = os.path.join(folder, "output.png")
    if not os.path.exists(map_file):
        abort(404, "Map image not found for this run.")
    meta_path = os.path.join(folder, "meta.json")
    meta = {}
    if os.path.exists(meta_path):
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except Exception:
            pass
    return render_template(
        "map_result.html",
        map_url=f"/outputs/{run_id}/output.png",
        run_id=run_id,
        forest_name=meta.get("forest_name", "Forest Boundary"),
        area_ha=meta.get("area_ha"),
    )


# ----------------------------------------------------------------------
# ROUTES
# ----------------------------------------------------------------------

@pages_bp.route("/")
def home():
    return render_template("index.html")

# ABOUT, ROBOTS, SITEMAP
# ----------------------------------------------------------------------

@pages_bp.route("/about")
def about_page():
    return Response("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Elfak GIS Pro Studio — Professional Forest GIS Application</title>
<meta name="description" content="Elfak GIS Pro Studio (elfakgis, elfakgispro, elfakgisstudio, elfakgisprostudio) is a professional web-based GIS application for forest boundary mapping, slope analysis, compartment subdivision, survey point generation and multi-forest analysis. Built for Nepal forestry professionals.">
<meta name="keywords" content="elfakgis, elfakgispro, elfakgisstudio, elfakgisprostudio, elfak gis, elfak gis pro, forest gis nepal, forest boundary mapping, slope analysis nepal, compartment mapping, survey points gis, forestry nepal gis, GIS tool nepal">
<meta name="robots" content="index, follow">
<meta property="og:title" content="Elfak GIS Pro Studio — Forest GIS Application">
<meta property="og:description" content="Professional web GIS for Nepal forestry: boundary mapping, slope analysis, compartment subdivision, survey points. Free to use at elfakgisstudio.onrender.com">
<meta property="og:url" content="https://elfakgisstudio.onrender.com/">
<meta property="og:type" content="website">
<link rel="canonical" href="https://elfakgisstudio.onrender.com/">
<link rel="alternate" href="https://elfakgisstudio.onrender.com/" hreflang="en">
<style>
  body{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:20px 24px;
       color:#1a2e22;background:#f0f8f3;line-height:1.7}
  h1{color:#059669;font-size:2em;margin-bottom:8px}
  h2{color:#065f46;border-bottom:2px solid #34d399;padding-bottom:6px;margin-top:32px}
  .badge{display:inline-block;background:#d1fae5;color:#065f46;padding:3px 10px;
         border-radius:20px;font-size:13px;font-weight:600;margin:3px}
  .cta{display:inline-block;background:linear-gradient(135deg,#34d399,#059669);
       color:white;padding:12px 28px;border-radius:8px;text-decoration:none;
       font-weight:700;font-size:16px;margin-top:20px;box-shadow:0 4px 14px rgba(16,185,129,.35)}
  .feature{background:white;border-radius:10px;padding:16px 20px;margin:12px 0;
            border-left:4px solid #10b981;box-shadow:0 2px 8px rgba(0,0,0,.06)}
  footer{margin-top:48px;padding-top:16px;border-top:1px solid #b7eacf;
         color:#6b9880;font-size:13px}
</style>
<script type="application/ld+json">
{"@context":"https://schema.org","@type":"WebApplication",
 "name":"Elfak GIS Pro Studio",
 "alternateName":["elfakgis","elfakgispro","elfakgisstudio","elfakgisprostudio"],
 "url":"https://elfakgisstudio.onrender.com/",
 "description":"Professional web-based GIS application for forest boundary mapping, slope analysis, compartment subdivision and survey point generation for Nepal forestry professionals.",
 "applicationCategory":"GIS Software",
 "operatingSystem":"Web Browser",
 "offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},
 "author":{"@type":"Organization","name":"Elfak GIS"}}
</script>
</head>
<body>
<h1>🌲 Elfak GIS Pro Studio</h1>
<p><strong>Professional Forest GIS Application</strong> for boundary mapping, slope analysis, compartment subdivision, and survey point generation.</p>
<p>
  <span class="badge">elfakgis</span>
  <span class="badge">elfakgispro</span>
  <span class="badge">elfakgisstudio</span>
  <span class="badge">elfakgisprostudio</span>
  <span class="badge">Forest GIS Nepal</span>
</p>
<a href="/" class="cta">🚀 Open Application</a>

<h2>What is Elfak GIS Pro Studio?</h2>
<p>Elfak GIS Pro Studio is a free, web-based Geographic Information System designed for forestry professionals in Nepal and the broader Himalayan region. It provides a complete workflow from raw survey data to professional-quality GIS outputs — all without requiring QGIS, ArcGIS, or any desktop installation.</p>

<h2>Features</h2>
<div class="feature"><strong>A — Boundary Whole</strong>: Generate forest boundary polygon from GPS survey points (Excel/CSV). Produces shapefile, line, and point layers with area calculation.</div>
<div class="feature"><strong>B — Segmented Forest</strong>: Multi-forest boundary generation with separate shapefile per forest/compartment.</div>
<div class="feature"><strong>C — Sample Plot Generator</strong>: Systematic grid-based sample plot placement inside forest boundaries for forest inventory.</div>
<div class="feature"><strong>D — Multi-Forest Complex</strong>: Batch process multiple forests with nested compartment structure.</div>
<div class="feature"><strong>E — Polygon Subdivider</strong>: Automatically divide a forest polygon into N equal-area compartments (2–15) with configurable area tolerance.</div>
<div class="feature"><strong>F — Slope Analysis</strong>: DEM-based slope classification (0–19°, 19–31°, 31–45°, >45°) with raster-to-polygon conversion, per-compartment area tables, and professional A4 map output.</div>
<div class="feature"><strong>G — Survey Point Generator</strong>: Generate boundary, vertex, and divider survey points from compartment shapefiles. Exports SHP, CSV, and Excel.</div>
<div class="feature"><strong>H — Sample Point Based GIS Maps</strong>: Upload boundary, compartments, DEM, satellite image, sample points and survey points to automatically generate six publication‑quality maps: Slope, Satellite, Sub‑compartment, Sample Plot, Boundary Survey, and Survey Point maps. Exports PNG, PDF, SVG.</div>

<h2>Technical Specifications</h2>
<ul>
<li>Coordinate systems: UTM Zone 43N, 44N, 45N, 46N (EPSG:32643–32646)</li>
<li>Input formats: Excel (.xlsx), CSV, Shapefile (.shp), ZIP of shapefiles, GeoTIFF (.tif)</li>
<li>Output formats: Shapefile (.shp), GeoJSON, KMZ, PNG map, Excel, CSV, PDF, SVG</li>
<li>Map output: A4 size, 300 DPI, professional cartography</li>
<li>DEM analysis: Slope reclassification, raster-to-polygon, area statistics</li>
<li>Supports 100+ simultaneous users with per-user data isolation</li>
</ul>

<h2>Who Uses Elfak GIS Pro Studio?</h2>
<p>Forest rangers, community forestry groups, district forest offices, forest inventory teams, and GIS professionals in Nepal, Bhutan, and similar forested regions who need professional GIS output without expensive desktop software.</p>

<h2>Open the Application</h2>
<p><a href="/" class="cta">🌲 Launch Elfak GIS Pro Studio</a></p>

<footer>
  <p>Elfak GIS Pro Studio · <a href="https://elfakgisstudio.onrender.com/">elfakgisstudio.onrender.com</a></p>
  <p>Keywords: elfakgis · elfakgispro · elfakgisstudio · elfakgisprostudio · forest gis nepal · slope analysis · compartment mapping · survey points · forestry gis</p>
</footer>
</body>
</html>""", mimetype="text/html")
@pages_bp.route("/favicon.ico")
def favicon():
    return send_from_directory(current_app.static_folder, "favicon.ico", mimetype="image/x-icon")

@pages_bp.route("/robots.txt")
def robots_txt():
    return Response("""User-agent: *
Allow: /
Allow: /about
Allow: /sitemap.xml
Disallow: /upload
Disallow: /run_g
Disallow: /outputs/
Disallow: /download/
Disallow: /progress/
Disallow: /geojson/
Sitemap: https://elfakgisstudio.onrender.com/sitemap.xml
""", mimetype="text/plain")

@pages_bp.route("/sitemap.xml")
def sitemap_xml():
    return Response("""<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://elfakgisstudio.onrender.com/</loc>
       <priority>1.0</priority><changefreq>weekly</changefreq></url>
  <url><loc>https://elfakgisstudio.onrender.com/about</loc>
       <priority>0.9</priority><changefreq>monthly</changefreq></url>
</urlset>""", mimetype="application/xml")


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
