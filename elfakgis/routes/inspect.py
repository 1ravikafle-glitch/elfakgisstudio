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
from elfakgis.core.security import (_rate_limit, _cool_down, _safe_filename, _safe_path,
    _validate_username, _get_client_ip, _assert_upload_size, _assert_zip_within_budget,
    ArchiveTooLarge)
from elfakgis.core.csrf import csrf_protect
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.geo.kmz import _generate_run_id, _safe_runid

log = logging.getLogger("elfakgis")

from flask import Blueprint
inspect_bp = Blueprint('inspect_bp', __name__)
@inspect_bp.route("/zip_inspect", methods=["POST"])
@_login_required
@csrf_protect
@_rate_limit(limit=30, window=60)
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
        # Bound the upload on disk, then bound what the archive *claims*.
        # Without the second check a 42 KB zip can list 10^12 members and
        # be echoed straight back at the caller.
        _assert_upload_size(zp)
        with zipfile.ZipFile(zp) as z:
            names = z.namelist()
            shps = [n for n in names if n.lower().endswith(".shp")]
            return jsonify({"shp_files": shps, "all_files": names})
    except (ArchiveTooLarge, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        log.warning("zip_inspect failed: %s", type(e).__name__)
        return jsonify({"error": "Could not read that archive."}), 400
    finally:
        try:
            shutil.rmtree(tmp)
        except OSError as e:
            log.warning("Could not remove inspect temp dir %r: %s", tmp, e)


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
