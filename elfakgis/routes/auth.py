"""ElfakGISProStudio — auth routes.

Accounts are shared with Forestry PSC Preparation: one username, one
password, both products. See :mod:`elfakgis.core.autshared` for the
verification order and the fail-closed guarantee.
"""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime

log = logging.getLogger("elfakgis")

from flask import (Flask, request, jsonify, send_file, send_from_directory,
                   render_template, session, Response, stream_with_context, abort, g)
from elfakgis.core.config import *
from elfakgis.core.store import (_prog, _PROG, _PROG_LOCK, _save_run_meta, _append_run,
    _require_login, _login_required, _lu, _su, _register_user, _login_existing, _logout_user,
    _runs_for)
from elfakgis.core.security import _rate_limit, _cool_down, _safe_filename, _safe_path, _validate_username, _get_client_ip
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.core import autshared
from elfakgis.geo.kmz import _generate_run_id, _safe_runid

from flask import Blueprint
auth_bp = Blueprint('auth_bp', __name__)


def _establish_session(username):
    """Put a verified username into the Flask session and return its history."""
    session["username"] = username
    session.permanent = True
    runs = _runs_for(username)
    return runs


@auth_bp.route("/login", methods=["POST"])
def login():
    """
    Sign in with a Forestry PSC account.

    New accounts are created on first sign-in by Forestry PSC, so the same
    username and password that work there work here.
    """
    data = request.get_json(silent=True) or {}
    try:
        username = _validate_username(data.get("username", ""))
    except ValueError as e:
        return jsonify({"error": str(e)}), 400

    password = data.get("password", "")
    if not isinstance(password, str):
        password = ""
    if not password:
        return jsonify({"error": "Password is required."}), 400

    ok, reason = autshared.verify_credentials(username, password)
    if not ok:
        ip = _get_client_ip()
        if reason == "unavailable":
            log.error("Sign-in unavailable for %r from %s — cannot verify", username, ip)
            return jsonify({
                "error": "Sign-in is temporarily unavailable. Please try again in a moment."
            }), 503
        log.info("Rejected sign-in for %r from %s", username, ip)
        return jsonify({"error": "Invalid username or password."}), 401

    runs = _establish_session(username)
    autshared.db.record_login(username)
    is_new = not _runs_for(username) and not _lu().get(username)
    log.info("Login: %r from %s", username, _get_client_ip())

    return jsonify({
        "ok": True,
        "username": username,
        "runs": runs[-20:],
        "is_new": is_new,
        "message": f"Welcome back, {username}!",
    })


@auth_bp.route("/sso/exchange", methods=["GET", "POST"])
def sso_exchange():
    """
    Single sign-on handoff from Forestry PSC.

    Accepts a short-lived signed token, verifies it with the shared
    ``SSO_SECRET`` and signs the user straight in. On success it redirects
    to the studio so the click feels seamless; with ``?json=1`` it answers
    with JSON instead, which is easier to test.

    A missing, invalid or expired token is *never* an error page — the user
    is sent to the normal login form, which is exactly what happens when
    single sign-on is not configured.
    """
    token = ""
    if request.method == "POST":
        token = (request.get_json(silent=True) or {}).get("token", "")
    token = token or (request.args.get("t") or request.args.get("token") or "")

    payload = autshared.verify_sso_token((token or "").strip())
    if not payload:
        if request.args.get("json") == "1":
            return jsonify({
                "ok": False,
                "error": "Single sign-on is unavailable or the link has expired.",
                "login_url": "/",
            }), 401
        return Response(_sso_fallback_page(), status=401, mimetype="text/html")

    username = payload["u"]
    runs = _establish_session(username)
    log.info("SSO sign-in: %r from %s", username, _get_client_ip())

    if request.args.get("json") == "1":
        return jsonify({"ok": True, "username": username, "runs": runs[-20:]})

    # 303 so a reload never re-posts the token.
    return Response("", status=303, headers={"Location": "/"})


def _sso_fallback_page():
    """Small self-contained page: explain, then hand back to the login form."""
    import html as _html
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Elfak GIS Pro Studio</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family:-apple-system,BlinkMacSystemFont,'Inter',system-ui,sans-serif;
         background:#eff3ec; color:#12211b; padding:24px; }}
  .card {{ background:#fafbf9; border:1px solid #cbd8cf; border-radius:14px;
           padding:32px 28px; max-width:420px; text-align:center;
           box-shadow:0 12px 40px rgba(18,33,27,.12); }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p  {{ font-size:14px; color:#4c6158; line-height:1.6; margin:0 0 20px; }}
  a.btn {{ display:inline-block; background:#378152; color:#f7faf4; text-decoration:none;
           padding:11px 22px; border-radius:11px; font-weight:600; font-size:14px; }}
  a.btn:hover {{ background:#2f6b45; }}
  .mark {{ width:44px; height:44px; border-radius:12px; margin:0 auto 16px;
           background:linear-gradient(135deg,#4e9a6b,#378152); color:#fff;
           display:flex; align-items:center; justify-content:center;
           font-size:20px; font-weight:800; }}
</style></head>
<body><div class="card">
  <div class="mark" aria-hidden="true">F</div>
  <h1>One more step</h1>
  <p>We couldn't pass your sign-in across automatically.<br>
     Please sign in with the same username and password you use on Forestry PSC Preparation.</p>
  <a class="btn" href="/">Go to sign in</a>
</div></body></html>"""


@auth_bp.route("/logout", methods=["POST"])
def logout():
    username = session.get("username")
    _logout_user(username)
    session.clear()
    if username:
        log.info("Logout: %r from %s", username, _get_client_ip())
    return jsonify({"ok": True})


@auth_bp.route("/me")
@_login_required
def me():
    u = _require_login()
    return jsonify({"username": u, "runs": _runs_for(u)[-20:]})


@auth_bp.route("/history")
@_login_required
def history():
    u = _require_login()
    return jsonify({"runs": _runs_for(u)})


@auth_bp.route("/auth-capabilities")
def auth_capabilities():
    """What sign-in methods this deployment offers (used by diagnostics)."""
    return jsonify({
        "shared_accounts": True,
        "sso": autshared.sso_enabled(),
        "shared_database": autshared.db.available(),
    })


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
