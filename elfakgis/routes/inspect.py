"""Elfak GIS Studio — archive inspection routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
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

from flask import Blueprint
inspect_bp = Blueprint('inspect_bp', __name__)
@inspect_bp.route("/zip_inspect", methods=["POST"])
def zip_inspect():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400
    f = request.files["file"]
    try:
        fname = _safe_filename(f.filename)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not fname.lower().endswith(".zip"):
        return jsonify({"error": "Not a ZIP file"}), 400
    tmp = os.path.join(UPLOAD, "inspect_" + uuid.uuid4().hex[:8])
    os.makedirs(tmp, exist_ok=True)
    try:
        zp = os.path.join(tmp, "upload.zip")
        f.save(zp)
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
        shps = [n for n in names if n.lower().endswith(".shp")]
        return jsonify({"shp_files": shps, "all_files": names})
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    finally:
        try:
            shutil.rmtree(tmp)
        except:
            pass


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
