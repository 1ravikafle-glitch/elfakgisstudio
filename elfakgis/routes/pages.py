"""Elfak GIS Studio — pages & SEO routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import (Flask, request, jsonify, send_file, send_from_directory,
                   render_template, session, Response, stream_with_context, abort, g,
                   redirect)
from elfakgis.core.config import *
from elfakgis.core.config import _PROJECT_ROOT
from elfakgis.core.store import (_prog, _PROG, _PROG_LOCK, _save_run_meta, _append_run,
    _require_login, _login_required, _lu, _su, _register_user, _login_existing, _logout_user,
    _owns_run, _owns_run_or_404)
from elfakgis.core.security import _rate_limit, _cool_down, _safe_filename, _safe_path, _validate_username, _get_client_ip
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.geo.kmz import _generate_run_id, _safe_runid
from elfakgis.routes.auth import REMEMBER_COOKIE, _establish_session

log = logging.getLogger("elfakgis")

from flask import Blueprint, current_app
pages_bp = Blueprint('pages_bp', __name__)
@pages_bp.route("/map_editor/<run_id>")
@_login_required
def map_editor(run_id):
    """Standalone map editor page (opened from history or directly)."""
    run_id = _safe_runid(run_id)
    # 404 (not 403) for someone else's run, so the page cannot be used to
    # discover which run ids exist.
    _owns_run_or_404(run_id)
    folder = _safe_path(OUTPUT, run_id)
    if not os.path.isdir(folder):
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

def _signed_in_username():
    """The username for this request, or None.

    A valid Flask session wins. If the session is gone (restart, deploy,
    expiry) the long-lived stay-signed-in cookie is honoured here, in the
    request, so a hard refresh lands straight on the app instead of bouncing
    the visitor through a login form.
    """
    username = _require_login()
    if username:
        return username
    raw = request.cookies.get(REMEMBER_COOKIE, "")
    if not raw:
        return None
    try:
        from elfakgis.core import db as _db
        username = _db.remember_who(raw.strip())
    except Exception as e:
        log.warning("remember-me lookup failed (%s)", e)
        return None
    if username:
        _establish_session(username, load_runs=False)
        log.info("Remember-me sign-in on page load: %r from %s", username, _get_client_ip())
    return username


@pages_bp.route("/")
def home():
    """The studio itself. Never renders a login form: signed-out visitors
    are sent to the dedicated /login page, so a hard refresh while signed in
    cannot flash the login screen."""
    username = _signed_in_username()
    if not username:
        return redirect("/login")
    # Mint the CSRF token here so the app shell can echo it into a meta tag.
    #
    # username is not optional here. The header badge renders
    # data-username="{{ username }}", and crosssite.js reads an empty value as
    # "signed out" and skips the hand-off. Jinja renders an undefined variable
    # as "", so leaving it out of the context disabled single sign-on back to
    # the sibling app without a single error - the link still worked, it just
    # quietly dropped the signed-in visitor on the Prep sign-in page.
    from elfakgis.core.csrf import generate_csrf_token
    return render_template(
        "index.html", csrf_token=generate_csrf_token(), username=username
    )


@pages_bp.route("/login")
def login_page():
    """Standalone sign-in page — its own URL, its own layout, no app shell."""
    if _signed_in_username():
        return redirect("/")
    return render_template("login.html")

# ABOUT, ROBOTS, SITEMAP
# ----------------------------------------------------------------------

@pages_bp.route("/about")
def about_page():
    return Response("""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Elfak GIS Studio — Professional Forest GIS Application</title>
<meta name="description" content="Elfak GIS Studio (elfakgis, elfakgis, elfakgisstudio, elfakgisstudio) is a professional web-based GIS application for forest boundary mapping, slope analysis, compartment subdivision, survey point generation and multi-forest analysis. Built for Nepal forestry professionals.">
<meta name="keywords" content="elfakgis, elfakgisstudio, elfak gis, forest gis nepal, forest boundary mapping, slope analysis nepal, compartment mapping, survey points gis, forestry nepal gis, GIS tool nepal">
<meta name="robots" content="index, follow">
<meta property="og:title" content="Elfak GIS Studio — Forest GIS Application">
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
 "name":"Elfak GIS Studio",
 "alternateName":["elfakgis","elfakgisstudio"],
 "url":"https://elfakgisstudio.onrender.com/",
 "description":"Professional web-based GIS application for forest boundary mapping, slope analysis, compartment subdivision and survey point generation for Nepal forestry professionals.",
 "applicationCategory":"GIS Software",
 "operatingSystem":"Web Browser",
 "offers":{"@type":"Offer","price":"0","priceCurrency":"USD"},
 "author":{"@type":"Organization","name":"Elfak GIS"}}
</script>
</head>
<body>
<h1>🌲 Elfak GIS Studio</h1>
<p><strong>Professional Forest GIS Application</strong> for boundary mapping, slope analysis, compartment subdivision, and survey point generation.</p>
<p>
  <span class="badge">elfakgis</span>
  <span class="badge">elfakgisstudio</span>
  <span class="badge">Forest GIS Nepal</span>
</p>
<a href="/" class="cta">🚀 Open Application</a>

<h2>What is Elfak GIS Studio?</h2>
<p>Elfak GIS Studio is a free, web-based Geographic Information System designed for forestry professionals in Nepal and the broader Himalayan region. It provides a complete workflow from raw survey data to professional-quality GIS outputs — all without requiring QGIS, ArcGIS, or any desktop installation.</p>

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

<h2>Who Uses Elfak GIS Studio?</h2>
<p>Forest rangers, community forestry groups, district forest offices, forest inventory teams, and GIS professionals in Nepal, Bhutan, and similar forested regions who need professional GIS output without expensive desktop software.</p>

<h2>Open the Application</h2>
<p><a href="/" class="cta">🌲 Launch Elfak GIS Studio</a></p>

<h2>Creation</h2>
<p>Elfak GIS Studio is designed and built by <a href="https://ravikafle.com.np" target="_blank" rel="noopener">ravikafle.com.np</a>.</p>

<footer>
  <p>© 2026 Elfak GIS Studio · <a href="https://elfakgisstudio.onrender.com/">elfakgisstudio.onrender.com</a> · Created by <a href="https://ravikafle.com.np" target="_blank" rel="noopener">ravikafle.com.np</a></p>
  <p><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · <a href="/cookies">Cookies</a> · <a href="/about">About</a></p>
  <p>Keywords: elfakgis · elfakgisstudio · forest gis nepal · slope analysis · compartment mapping · survey points · forestry gis</p>
</footer>
</body>
</html>""", mimetype="text/html")
@pages_bp.route("/favicon.ico")
def favicon():
    return send_from_directory(current_app.static_folder, "favicon.ico", mimetype="image/x-icon")

_LEGAL_STYLE = (
    "body{font-family:system-ui,sans-serif;max-width:900px;margin:0 auto;padding:20px 24px;"
    "color:#1a2e22;background:#f0f8f3;line-height:1.7}"
    "h1{color:#059669;font-size:2em;margin-bottom:8px}"
    "h2{color:#065f46;border-bottom:2px solid #34d399;padding-bottom:6px;margin-top:32px}"
    "footer{margin-top:48px;padding-top:16px;border-top:1px solid #b7eacf;"
    "color:#6b9880;font-size:13px}"
)

_LEGAL_FOOTER = (
    "<footer>"
    "<p>© 2026 Elfak GIS Studio · "
    '<a href="https://elfakgisstudio.onrender.com/">elfakgisstudio.onrender.com</a> · Created by '
    '<a href="https://ravikafle.com.np" target="_blank" rel="noopener">ravikafle.com.np</a></p>'
    '<p><a href="/privacy">Privacy</a> · <a href="/terms">Terms</a> · '
    '<a href="/cookies">Cookies</a> · <a href="/about">About</a></p>'
    "</footer>"
)


def _legal_page(title, body_html):
    return Response(
        "<!DOCTYPE html><html lang=\"en\"><head>"
        "<meta charset=\"UTF-8\">"
        '<meta name="viewport" content="width=device-width,initial-scale=1.0">'
        f"<title>{html.escape(title)} — Elfak GIS Studio</title>"
        '<meta name="robots" content="index, follow">'
        f"<style>{_LEGAL_STYLE}</style>"
        "</head><body>"
        f"<h1>{html.escape(title)}</h1>"
        f"{body_html}<p><a href=\"/\">← Back to Elfak GIS Studio</a></p>{_LEGAL_FOOTER}"
        "</body></html>",
        mimetype="text/html",
    )


@pages_bp.route("/privacy")
def privacy_page():
    return _legal_page("Privacy Policy", """
<p><strong>Last updated: 2026.</strong> Elfak GIS Studio processes the files you upload
(Excel, CSV, shapefiles, GeoTIFF) to run the requested GIS analysis and render your maps.</p>
<h2>What we store</h2>
<ul>
<li>Your account username and login session (including an optional stay-signed-in cookie).</li>
<li>Your uploaded inputs and generated outputs (shapefiles, maps, run history) tied to your account.</li>
<li>Basic operational logs needed for security, rate limiting, and reliability.</li>
</ul>
<h2>What we do not do</h2>
<ul>
<li>We do not sell your data or share your uploads with advertisers.</li>
<li>We do not use your forest survey data for any purpose other than running your analysis.</li>
</ul>
<h2>Your control</h2>
<p>You can delete individual runs or clear your history from the Run History drawer.
Sign out any time to end your session. For questions, contact the creator via
<a href="https://ravikafle.com.np" target="_blank" rel="noopener">ravikafle.com.np</a>.</p>
""")


@pages_bp.route("/terms")
def terms_page():
    return _legal_page("Terms of Use", """
<p><strong>Last updated: 2026.</strong> By using Elfak GIS Studio you agree to these terms.</p>
<h2>Service</h2>
<ul>
<li>Elfak GIS Studio is a free web-based GIS for forestry mapping and analysis.</li>
<li>Outputs (areas, slopes, maps) are computed from your inputs — always verify
critical boundaries and areas against field records before official use.</li>
</ul>
<h2>Acceptable use</h2>
<ul>
<li>Upload only data you have the right to process.</li>
<li>Do not abuse the service (scraping, credential sharing, attacks, unlawful content).</li>
</ul>
<h2>Availability</h2>
<p>The service is provided "as is" without warranties. We may rate-limit, suspend
abusive accounts, or change features to keep the service reliable.</p>
""")


@pages_bp.route("/cookies")
def cookies_page():
    return _legal_page("Cookie Policy", """
<p><strong>Last updated: 2026.</strong> Elfak GIS Studio uses a small number of cookies
and browser storage entries to keep you signed in and remember your preferences.</p>
<h2>Cookies we use</h2>
<ul>
<li><strong>Session cookie</strong> — keeps you signed in while you use the studio.</li>
<li><strong>Stay-signed-in cookie</strong> — optional long-lived login so a hard refresh
lands straight on the app.</li>
<li><strong>Theme preference</strong> — remembers your light/dark appearance choice.</li>
</ul>
<h2>Managing cookies</h2>
<p>You can clear cookies in your browser settings. Note that signing out or clearing
cookies will sign you out of the studio.</p>
""")

@pages_bp.route("/robots.txt")
def robots_txt():
    return Response("""User-agent: *
Allow: /
Allow: /about
Allow: /privacy
Allow: /terms
Allow: /cookies
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
  <url><loc>https://elfakgisstudio.onrender.com/privacy</loc>
       <priority>0.3</priority><changefreq>yearly</changefreq></url>
  <url><loc>https://elfakgisstudio.onrender.com/terms</loc>
       <priority>0.3</priority><changefreq>yearly</changefreq></url>
  <url><loc>https://elfakgisstudio.onrender.com/cookies</loc>
       <priority>0.3</priority><changefreq>yearly</changefreq></url>
</urlset>""", mimetype="application/xml")


@pages_bp.route("/sw.js")
def service_worker():
    """Serve the service worker from the origin root.

    A worker served from /static/ would only get scope "/static/" and
    could never intercept the page or the API calls, which is the whole
    point of it. Service-Worker-Allowed is set so the root path is
    accepted even if the file ever moves.
    """
    resp = send_from_directory(
        os.path.join(_PROJECT_ROOT, "static"), "sw.js", mimetype="application/javascript"
    )
    resp.headers["Service-Worker-Allowed"] = "/"
    # Revalidated every time so a deploy ships a new worker promptly.
    resp.headers["Cache-Control"] = "no-cache"
    return resp


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
