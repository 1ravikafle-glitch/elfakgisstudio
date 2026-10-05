"""Elfak GIS Studio — Flask app factory (light boot; GIS lazy) (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")
from flask import Flask, request, jsonify
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from elfakgis.core.config import UPLOAD_MAX_BYTES, load_dotenv
from elfakgis.core.security import _PROXY_TRUSTED

# Must run before anything reads os.environ. `elfakgis.core.config` computes
# UPLOAD, OUTPUT and DEM paths from the environment at import time, and
# core.db reads DATABASE_URL lazily, so a .env loaded any later would be
# ignored or half-applied. Doing it here covers every entrypoint: gunicorn
# app:app, `flask --app app`, and `python app.py` all import this package
# first. Real environment variables still win, so this is a no-op in
# production where Render supplies everything.
load_dotenv()

# ── Response compression ───────────────────────────────────────────
# app.js alone is ~160 KB and the two stylesheets ~50 KB, all plain text.
# Gunicorn does not compress, so without this every cold visit pulls ~210 KB
# of text that would fit in ~40 KB gzipped. Only text-ish types are touched;
# images, TIFs and already-encoded bodies pass straight through.
_COMPRESSIBLE = (
    "text/", "application/javascript", "application/json",
    "application/xml", "application/manifest+json", "image/svg+xml",
)
_GZIP_MIN_BYTES = 1024
# The progress stream is server-sent: buffering it would stall live updates,
# so it is never compressed.
_NEVER_COMPRESS = ("/progress", "/download/", "/outputs/", "/dem/")


def _maybe_gzip(resp):
    """Gzip a text response when the client asked for it. Never raises.

    Order matters: the cheap header checks run before anything reads the
    body, so a 200 MB TIF is rejected on its content type alone and never
    pulled into memory.
    """
    try:
        if resp.status_code >= 300:
            return resp
        # A streamed response is either a raw file wrapper (Flask serves
        # /static/* that way, and it is safe to buffer) or a live generator
        # such as the progress SSE stream, which must never be buffered.
        if resp.is_streamed and not resp.direct_passthrough:
            return resp
        if resp.headers.get("Content-Encoding"):
            return resp
        if request.method == "HEAD" or request.path.startswith(_NEVER_COMPRESS):
            return resp

        ctype = (resp.mimetype or "").lower()
        if not any(ctype.startswith(t) or ctype == t for t in _COMPRESSIBLE):
            return resp

        if "gzip" not in (request.headers.get("Accept-Encoding") or ""):
            resp.headers.add("Vary", "Accept-Encoding")
            return resp

        # Flask streams /static/* through a file wrapper, which cannot be
        # read while direct_passthrough is on. Flipping it off lets get_data()
        # buffer the file, which is what compression needs anyway.
        was_passthrough = resp.direct_passthrough
        resp.direct_passthrough = False
        data = resp.get_data()
        if was_passthrough:
            resp.direct_passthrough = True
        if len(data) < _GZIP_MIN_BYTES:
            return resp

        # These bodies are already fully buffered, so a one-shot compress
        # beats streaming a compressor through the response.
        import gzip as _gzip
        packed = _gzip.compress(data, compresslevel=6, mtime=0)
        if len(packed) >= len(data):
            return resp          # already dense; compression would cost bytes

        resp.set_data(packed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(packed))
        resp.headers.add("Vary", "Accept-Encoding")
        # The body is now a different representation, so it needs a different
        # validator. Without this a cache could hand the gzipped body to a
        # client that never asked for it.
        etag = resp.headers.get("ETag")
        if etag and not etag.endswith('-gzip"'):
            resp.headers["ETag"] = etag[:-1] + '-gzip"'
    except Exception as e:       # never let compression break a response
        log.warning("gzip skipped: %s", e)
    return resp

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _stable_secret_key():
    """Flask signing key that survives restarts: env → disk file → random.

    A random-per-boot key invalidates every login on each restart (Render
    free sleeps), so the key is persisted to .secret_key as a fallback.

    In production that fallback is refused, loudly. Render's filesystem is
    ephemeral, so a persisted key is wiped on every deploy — which silently
    signs every user out and invalidates every 30-day remember cookie. A
    hard failure at boot is much better than an app nobody can stay signed
    into. Local dev still gets the file so `flask run` just works.
    """
    env = (os.environ.get("SECRET_KEY") or "").strip()
    if env:
        return env

    # Render sets RENDER=true. Anything else that looks like production
    # (FLASK_ENV / APP_ENV) counts too.
    production = bool(
        os.environ.get("RENDER")
        or (os.environ.get("APP_ENV") or "").lower() == "production"
        or (os.environ.get("FLASK_ENV") or "").lower() == "production"
    )
    if production:
        raise RuntimeError(
            "SECRET_KEY is not set. This deployment looks like production, so "
            "refusing to fall back to a generated key: Render's filesystem is "
            "ephemeral and every deploy would sign all users out. Set SECRET_KEY "
            "in the host environment to a 32+ character random value."
        )

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
        except OSError:
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

    # Behind a reverse proxy, every request arrives as plain HTTP with the
    # real scheme in X-Forwarded-Proto. Without this, Flask believes the
    # connection is insecure, so secure cookies are not marked Secure, HSTS
    # is never sent, and url_for(_external=True) would emit http:// links.
    #
    # The hop count is 1 because there is exactly one proxy (Render) in
    # front. This must match reality: an over-count lets a client forge the
    # header chain and impersonate any address.
    if _PROXY_TRUSTED:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1,
                                x_host=1, x_prefix=0)

    # A Secure cookie is required in production, so do not gate this on an
    # `HTTPS` environment variable that Render never sets. Derive it from the
    # proxy decision instead: when a proxy terminates TLS, treat it as HTTPS.
    https_only = os.environ.get("HTTPS", "0") == "1" or _PROXY_TRUSTED

    app.config.update(
        SESSION_COOKIE_SECURE    = https_only,
        SESSION_COOKIE_HTTPONLY  = True,
        SESSION_COOKIE_SAMESITE  = "Lax",
        PERMANENT_SESSION_LIFETIME = 86400 * 7,
        # Transport-level backstop. The old value was 2 GB on a 512 MB
        # free-tier instance, where a few concurrent uploads OOM-killed the
        # process — which on Render is a boot loop that signs out every
        # user. The authoritative per-file check is in core.security.
        MAX_CONTENT_LENGTH       = UPLOAD_MAX_BYTES,
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
        mb = UPLOAD_MAX_BYTES // (1024 * 1024)
        return jsonify({"error": f"File too large. Maximum upload is {mb} MB."}), 413

    @app.errorhandler(429)
    def rate_limited(e):
        return jsonify({"error": "Too many requests. Please wait a moment.", "retry_after": 5}), 429

    @app.errorhandler(500)
    def server_error(e):
        log.exception("Unhandled 500")
        # The exception text goes to the log, never to the client: it
        # routinely contains absolute paths, SQL fragments and, on Render,
        # enough of the traceback to map the deployment's internals.
        return jsonify({"error": "Internal server error. Please try again."}), 500

    @app.errorhandler(Exception)
    def unhandled(e):
        # Werkzeug's own HTTP errors (404, 405, 400) are control flow, not
        # crashes. Without this branch they fall through to the catch-all
        # below and every unknown URL answers 500 instead of its real status.
        if isinstance(e, HTTPException):
            return jsonify({"error": e.description or e.name}), e.code
        log.exception("Unhandled exception: %s", type(e).__name__)
        return jsonify({"error": "Unexpected error. Please try again."}), 500

    @app.after_request
    def _security_headers(resp):
        resp.headers["X-Content-Type-Options"]   = "nosniff"
        resp.headers["X-Frame-Options"]          = "DENY"
        resp.headers["X-XSS-Protection"]         = "1; mode=block"
        resp.headers["Referrer-Policy"]          = "strict-origin-when-cross-origin"
        resp.headers["Permissions-Policy"]       = "geolocation=(), camera=(), microphone=()"
        # Send HSTS on a genuinely secure connection. The old check was
        # `HTTPS=1`, an environment variable Render does not set, so HSTS was
        # never actually sent in production. ProxyFix has already folded
        # X-Forwarded-Proto into request.is_secure by the time a response
        # runs, so ask the request rather than the environment.
        if request.is_secure or request.headers.get("X-Forwarded-Proto") == "https":
            resp.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        # Content-Security-Policy.
        #
        # Two CDNs are needed, not one. Leaflet and Leaflet-Geoman come from
        # unpkg; JSZip, SheetJS and html2canvas come from cdnjs. cdnjs was
        # missing from script-src, which did not throw and did not look broken:
        # the three libraries are loaded on demand by app.js, so the page
        # rendered fine and every file-dependent action silently did nothing.
        # "Select Excel / CSV" parsed nothing because SheetJS never loaded,
        # and Download ZIP produced no archive because JSZip never loaded.
        #
        # Allowing the host is safe because app.js pins all four with an SRI
        # hash and sets crossOrigin, so a CDN that serves different bytes fails
        # the integrity check instead of executing. 'unsafe-inline' for styles
        # is required by Leaflet's CSS-in-JS and by inline style attributes;
        # script is still NOT allowed inline anywhere, which is the part that
        # neutralises an injected <script> or an inline handler.
        #
        # connect-src needs both CDNs too: prefetch.js warms them with fetch(),
        # and a blocked warm means the library is only discovered at the moment
        # the user clicks the button that needs it.
        csp = (
            "default-src 'self'; "
            "script-src 'self' https://unpkg.com https://cdnjs.cloudflare.com; "
            "style-src 'self' 'unsafe-inline' https://unpkg.com "
            "https://cdnjs.cloudflare.com; "
            "img-src 'self' data: blob: https://*.basemaps.cartocdn.com "
            "https://server.arcgisonline.com https://*.tile.openstreetmap.org; "
            "font-src 'self' data:; "
            "connect-src 'self' https://unpkg.com https://cdnjs.cloudflare.com "
            "https://*.basemaps.cartocdn.com "
            "https://server.arcgisonline.com https://*.tile.openstreetmap.org; "
            "worker-src 'self' blob:; "
            "object-src 'none'; "
            "base-uri 'self'; "
            "form-action 'self'; "
            "frame-ancestors 'none'"
        )
        resp.headers["Content-Security-Policy"] = csp
        if request.path.startswith(("/login", "/me", "/history", "/progress")):
            resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
            resp.headers["Pragma"]        = "no-cache"
        # Version-pinned static (?v=) is immutable: repeat visits + the
        # background prefetch serve it from browser cache, no re-download.
        if request.path.startswith("/static/") and request.args.get("v"):
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        # Same-origin bootstrap files are revalidated so a deploy is picked
        # up, but a 304 costs almost nothing.
        elif request.path == "/sw.js":
            resp.headers["Cache-Control"] = "no-cache"
        # Read-only reference data (dropdowns, DEM catalogue) is identical
        # for every signed-in user, so it may sit in a shared cache for a
        # few minutes. Never applied to anything user-specific.
        elif request.path in ("/thesis_options", "/dem_catalog"):
            resp.headers["Cache-Control"] = "private, max-age=300"
        resp.headers["X-Accel-Buffering"] = "no"
        return _maybe_gzip(resp)

    from elfakgis.core.store import _cleanup_old_prog
    from elfakgis.core.security import _cleanup_old_outputs, _clean_rl
    threading.Thread(target=_cleanup_old_prog, daemon=True).start()
    threading.Thread(target=_cleanup_old_outputs, daemon=True).start()
    threading.Thread(target=_clean_rl, daemon=True).start()
    return app


app = create_app()
