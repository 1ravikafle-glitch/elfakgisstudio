"""Elfak GIS Studio — Flask app factory (light boot; GIS lazy) (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from flask import Flask, request, jsonify

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _stable_secret_key():
    """Flask signing key that survives restarts: env → disk file → random.

    A random-per-boot key invalidates every login on each restart (Render
    free sleeps). For permanent stability set SECRET_KEY in the host env."""
    env = (os.environ.get("SECRET_KEY") or "").strip()
    if env:
        return env
    try:
        path = os.path.join(_ROOT, ".secret_key")
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as f:
                saved = f.read().strip()
            if len(saved) >= 32:
                return saved
        fresh = secrets.token_hex(32)
        with open(path, "w", encoding="utf-8") as f:
            f.write(fresh)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        log.warning("SECRET_KEY not set — generated and saved to .secret_key "
                    "(set SECRET_KEY in the host env for multi-instance stability)")
        return fresh
    except Exception as e:
        log.warning("SECRET_KEY not set and not savable (%s) — sessions "
                    "won't survive restarts", e)
        return secrets.token_hex(32)


def create_app():
    """Build the Flask app. Imports only light modules; GIS loads per-request."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    app = Flask(__name__,
                template_folder=os.path.join(_ROOT, 'templates'),
                static_folder=os.path.join(_ROOT, 'static'))
    app.secret_key = _stable_secret_key()

    app.config.update(
        SESSION_COOKIE_SECURE    = os.environ.get("HTTPS","0") == "1",
        SESSION_COOKIE_HTTPONLY  = True,
        SESSION_COOKIE_SAMESITE  = "Lax",
        PERMANENT_SESSION_LIFETIME = 86400 * 7,
        MAX_CONTENT_LENGTH       = 2 * 1024 * 1024 * 1024,
    )
    # Cache-busted static assets (split CSS/JS) are immutable; pages stay dynamic.
    app.config.setdefault("SEND_FILE_MAX_AGE_DEFAULT", 86400)

    from elfakgis.routes.pages import pages_bp
    from elfakgis.routes.auth import auth_bp
    from elfakgis.routes.maps import maps_bp
    from elfakgis.routes.dem import dem_bp
    from elfakgis.routes.inspect import inspect_bp
    from elfakgis.routes.pipeline import pipeline_bp
    for _bp in (pages_bp, auth_bp, maps_bp, dem_bp, inspect_bp, pipeline_bp):
        app.register_blueprint(_bp)

    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": "File too large. Maximum upload is 2GB."}), 413

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify({"error": "Too many requests. Please wait a moment.", "retry_after": 5}), 429

    @app.errorhandler(500)
    def server_error(e):
        log.error(f"Unhandled 500: {e}")
        return jsonify({"error": f"Internal server error: {str(e)[:200]}"}), 500

    @app.errorhandler(Exception)
    def unhandled(e):
        log.error(f"Unhandled exception: {type(e).__name__}: {e}")
        return jsonify({"error": f"Unexpected error: {type(e).__name__}: {str(e)[:200]}"}), 500

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Content-Type-Options"]   = "nosniff"
        resp.headers["X-Frame-Options"]          = "DENY"
        resp.headers["X-XSS-Protection"]         = "1; mode=block"
        resp.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"]       = "geolocation=(), camera=(), microphone=()"
        if os.environ.get("HTTPS") == "1":
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        allowed = os.environ.get("ALLOWED_ORIGINS", "*")
        resp.headers["Access-Control-Allow-Origin"]       = allowed
        resp.headers["Access-Control-Allow-Headers"]      = "Content-Type,Authorization,X-Requested-With"
        resp.headers["Access-Control-Allow-Methods"]      = "GET,POST,OPTIONS"
        resp.headers["Access-Control-Allow-Credentials"]  = "true"
        if request.path.startswith(("/login", "/me", "/history", "/progress")):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            resp.headers["Pragma"]        = "no-cache"
        # Version-pinned static (?v=) is immutable: repeat visits + the
        # post-login warmup serve it from browser cache, no re-download.
        if request.path.startswith("/static/") and request.args.get("v"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        resp.headers["X-Accel-Buffering"] = "no"
        return resp

    from elfakgis.core.store import _cleanup_old_prog
    from elfakgis.core.security import _cleanup_old_outputs, _clean_rl
    threading.Thread(target=_cleanup_old_prog, daemon=True).start()
    threading.Thread(target=_cleanup_old_outputs, daemon=True).start()
    threading.Thread(target=_clean_rl, daemon=True).start()
    return app


app = create_app()
