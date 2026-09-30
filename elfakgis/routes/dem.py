"""Elfak GIS Studio — DEM catalog routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
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
from elfakgis.core.csrf import csrf_protect
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.geo.kmz import _generate_run_id, _safe_runid

log = logging.getLogger("elfakgis")

from flask import Blueprint
dem_bp = Blueprint('dem_bp', __name__)
# ----------------------------------------------------------------------
# DEM Routes
# ----------------------------------------------------------------------

@dem_bp.route("/dem_catalog")
@_login_required
@_rate_limit(limit=60, window=60)
def dem_catalog():
    import urllib.request as _ur
    import json as _json
    # Cache per worker (10 min): the GitHub listing otherwise costs ~1.5s
    # on every F-tab visit / page load.
    global _DEM_CATALOG_CACHE
    try:
        cached = _DEM_CATALOG_CACHE
        if time.time() - cached[0] < 600:
            return jsonify(cached[1])
    except NameError:
        _DEM_CATALOG_CACHE = (0, None)
    github_api_urls = []
    for zone in ("44N", "45N"):
        api_base = os.environ.get("GITHUB_API_DEM",
            "https://api.github.com/repos/1ravikafle-glitch/elfakgisstudio/contents/dem_catalog")
        github_api_urls.append((zone, f"{api_base}/{zone}"))
    files = []
    for zone, api_url in github_api_urls:
        try:
            hdrs = {"User-Agent": "elfak-gis-app", "Accept": "application/vnd.github+json"}
            if GITHUB_TOKEN:
                hdrs["Authorization"] = f"Bearer {GITHUB_TOKEN}"
            req = _ur.Request(api_url, headers=hdrs)
            with _ur.urlopen(req, timeout=8) as resp:
                items = _json.loads(resp.read())
            for item in items:
                name = item.get("name", "")
                if not name.lower().endswith((".tif", ".tiff")):
                    continue
                size_mb = round(item.get("size", 0) / (1024*1024), 1)
                files.append({
                    "name": name,
                    "zone": zone,
                    "path": f"{zone}/{name}",
                    "size_mb": size_mb,
                    "url": item.get("url", api_url + "/" + name),
                })
        except Exception as e:
            log.warning(f"GitHub API {zone} failed: {e}")
            local = os.path.join(DEM_CATALOG_DIR, zone)
            if os.path.isdir(local):
                for f in os.listdir(local):
                    if f.lower().endswith((".tif", ".tiff")):
                        fp = os.path.join(local, f)
                        files.append({
                            "name": f,
                            "zone": zone,
                            "path": f"{zone}/{f}",
                            "size_mb": round(os.path.getsize(fp)/(1024*1024), 1),
                            "url": "",
                        })
    files.sort(key=lambda x: (x["zone"], x["name"]))
    payload = {"files": files, "source": "github"}
    _DEM_CATALOG_CACHE = (time.time(), payload)
    return jsonify(payload)

@dem_bp.route("/dem_fetch", methods=["POST"])
@_login_required
@csrf_protect
@_rate_limit(limit=30, window=60)
def dem_fetch():
    import urllib.request as _ur
    import urllib.error as _ue
    data = request.get_json(silent=True) or {}
    url = data.get("url", "").strip()
    path = data.get("path", "").strip()
    if not url and not path:
        return jsonify({"error": "No DEM URL or path provided."}), 400
    if url and not (url.startswith("https://raw.githubusercontent.com/") or
                    url.startswith("https://github.com/") or
                    url.startswith("https://api.github.com/")):
        return jsonify({"error": "DEM URL must be from GitHub."}), 400
    safe_name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(path or url))[:120]
    cache_key = hashlib.sha256((url or path).encode()).hexdigest()[:16]
    local_path = os.path.join(DEM_CACHE_DIR, f"{cache_key}_{safe_name}")
    if os.path.exists(local_path) and os.path.getsize(local_path) > 1000:
        size_mb = round(os.path.getsize(local_path) / (1024*1024), 1)
        log.info(f"DEM cache hit: {safe_name} ({size_mb}MB)")
        return jsonify({"ok": True, "cache_key": cache_key, "local": local_path,
                        "size_mb": size_mb, "cached": True})
    candidates = []
    if url:
        candidates.append(url)
    if path:
        owner_repo = "1ravikafle-glitch/elfakgisstudio"
        enc_path = "/".join(urllib.parse.quote(seg) for seg in path.split("/"))
        for branch in ("main", "master"):
            candidates.append(
                f"https://raw.githubusercontent.com/{owner_repo}/{branch}/dem_catalog/{enc_path}"
            )
    seen = set()
    candidates = [c for c in candidates if not (c in seen or seen.add(c))]
    if not candidates:
        return jsonify({"error": "Could not build a download URL from the given path."}), 400
    last_error = None
    for candidate_url in candidates:
        try:
            req = _ur.Request(candidate_url, headers={"User-Agent": "elfak-gis-app"})
            with _ur.urlopen(req, timeout=120) as resp, open(local_path, "wb") as fout:
                total = 0
                while True:
                    chunk = resp.read(1024*1024)
                    if not chunk:
                        break
                    fout.write(chunk)
                    total += len(chunk)
            if total < 500:
                os.remove(local_path)
                last_error = f"Response too small ({total} bytes)"
                continue
            size_mb = round(os.path.getsize(local_path) / (1024*1024), 1)
            log.info(f"DEM downloaded: {safe_name} ({size_mb}MB)")
            return jsonify({"ok": True, "cache_key": cache_key, "local": local_path,
                            "size_mb": size_mb, "cached": False, "source_url": candidate_url})
        except Exception as e:
            last_error = f"{type(e).__name__}: {e}"
            if os.path.exists(local_path):
                os.remove(local_path)
            continue
    return jsonify({
        "error": f"Failed to download DEM. {last_error}",
        "attempted_urls": candidates,
    }), 404


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
