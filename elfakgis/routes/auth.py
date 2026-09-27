"""ElfakGISProStudio — auth routes (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
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
auth_bp = Blueprint('auth_bp', __name__)
@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    try:
        username = _validate_username(data.get("username", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    users = _lu()
    is_new = username not in users

    if is_new:
        try:
            user, _tok = _register_user(username)
        except ValueError as e:
            return jsonify({"error": str(e), "taken": True}), 409
        except Exception as e:
            log.error(f"Registration error {username!r}: {e}")
            return jsonify({"error": "Registration failed. Please try again."}), 500
    else:
        try:
            user = _login_existing(username)
        except KeyError:
            try:
                user, _tok = _register_user(username)
            except Exception as e:
                return jsonify({"error": str(e)}), 500
        except Exception as e:
            log.error(f"Login error {username!r}: {e}")
            return jsonify({"error": "Login failed. Please try again."}), 500

    session["username"] = username
    session.permanent = True
    ip = _get_client_ip()
    log.info(f"{'Register' if is_new else 'Login'}: {username!r} from {ip}")

    return jsonify({
        "ok": True,
        "username": username,
        "runs": user.get("runs", [])[-20:],
        "is_new": is_new,
        "message": "Welcome!" if is_new else f"Welcome back, {username}!"
    })

@auth_bp.route("/logout", methods=["POST"])
def logout():
    username = session.get("username")
    _logout_user(username)
    session.clear()
    if username:
        log.info(f"Logout: {username!r} from {_get_client_ip()}")
    return jsonify({"ok": True})

@auth_bp.route("/me")
@_login_required
def me():
    u = _require_login()
    users = _lu()
    user = users.get(u, {})
    return jsonify({"username": u, "runs": user.get("runs", [])[-20:]})

@auth_bp.route("/history")
@_login_required
def history():
    u = _require_login()
    users = _lu()
    user = users.get(u, {})
    return jsonify({"runs": user.get("runs", [])})


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
