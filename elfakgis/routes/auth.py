"""Elfak GIS Studio — auth routes.

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
    _runs_for, _delete_run, _clear_runs)
from elfakgis.core.security import (_safe_filename, _safe_path, _get_client_ip, _rate_limit,
    login_guard)
from elfakgis.core.csrf import csrf_protect
from elfakgis.core.pipeline import _with_pipeline_sem
from elfakgis.core import autshared
from elfakgis.geo.kmz import _generate_run_id, _safe_runid

from flask import Blueprint
auth_bp = Blueprint('auth_bp', __name__)


def _establish_session(username, load_runs=True):
    """Put a verified username into the Flask session.

    load_runs=False skips the Postgres round-trip (hot paths: /me is hit on
    every page load; runs backfill async via /history instead)."""
    session["username"] = username
    session.permanent = True
    if not load_runs:
        return []
    runs = _runs_for(username)
    return runs


REMEMBER_COOKIE = "elfak_rem"
REMEMBER_MAX_AGE = 30 * 24 * 3600

# Read through autshared so the route and the verification chain can never
# disagree about the policy. See elfakgis/core/autshared.py for why the
# fallback is off by default.
_SELF_REGISTRATION_ALLOWED = autshared.self_registration_allowed()
_FALLBACK_ENABLED = autshared.fallback_enabled()


def _remember_cookie_args():
    """Flags for the stay-signed-in cookie.

    This is a 30-day bearer credential — the token alone is enough to become
    that user — so `secure` is not optional. It used to be read from
    `HTTPS=1`, an environment variable Render never sets, which meant the
    cookie was issued without the Secure flag in production and would have
    been replayed by anything that saw plain HTTP.

    Taken from app.config so the session cookie and this one cannot drift
    apart; the factory already resolves it from the TLS-terminating proxy.
    """
    try:
        from flask import current_app
        secure = bool(current_app.config.get("SESSION_COOKIE_SECURE", False))
    except Exception:
        secure = os.environ.get("HTTPS", "0") == "1"
    return {
        "max_age": REMEMBER_MAX_AGE,
        "httponly": True,
        "samesite": "Lax",
        "secure": secure,
    }


def _issue_remember(resp, username):
    """Attach (or refresh) the stay-signed-in cookie. Never breaks login."""
    try:
        from elfakgis.core import db as _db
        raw = _db.remember_issue(username)
        if raw:
            resp.set_cookie(REMEMBER_COOKIE, raw, **_remember_cookie_args())
    except Exception as e:
        log.warning("remember-me issue failed (%s)", e)


@auth_bp.route("/login", methods=["POST"])
@_rate_limit(limit=20, window=60)
@login_guard
def login():
    """
    Sign in with a Forestry PSC account.

    New accounts are created on first sign-in by Forestry PSC, so the same
    username and password that work there work here.
    """
    data = request.get_json(silent=True) or {}
    raw_username = data.get("username", "")
    username = raw_username.strip() if isinstance(raw_username, str) else ""
    # Forestry owns this account namespace: accept any nonempty Forestry
    # username rather than re-imposing GIS's historical registration rules.
    if not username:
        return jsonify({"error": "Username is required."}), 400
    if len(username) > 100:
        return jsonify({"error": "Username is too long."}), 400

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

    # reason == "new" means Forestry created this account from the password
    # that was just typed. Admitting it is what turns /login into
    # "anyone may register", so it needs to be asked for explicitly.
    if reason == "new" and not _SELF_REGISTRATION_ALLOWED:
        log.warning("Refused a self-registered account (%r from %s): "
                    "ALLOW_SELF_REGISTRATION is off", username, _get_client_ip())
        return jsonify({
            "error": "This account does not exist yet. Please create it in "
                     "Forestry PSC Preparation first, then sign in here."
        }), 403
    if reason == "new":
        log.warning("Admitted a self-registered account (%r from %s): "
                    "ALLOW_SELF_REGISTRATION is on", username, _get_client_ip())

    runs = _establish_session(username)
    # Truthful "new" flag: first GIS sign-in ever (tracked), NOT "has no
    # runs yet" (the old check re-fired on every login until run #1).
    try:
        from elfakgis.core import db as _db
        is_new = _db.mark_seen(username)
    except Exception as e:
        log.warning("seen tracking failed (%s)", e)
        is_new = not _runs_for(username) and not _lu().get(username)
    log.info("Login: %r from %s", username, _get_client_ip())

    resp = jsonify({
        "ok": True,
        "username": username,
        "runs": runs[-20:],
        "is_new": is_new,
        "message": f"Welcome back, {username}!",
    })
    _issue_remember(resp, username)
    return resp


def _already_signed_in():
    """True when this visitor already holds a usable session or remember cookie.

    Used only to pick the right page after a *failed* handoff. Without it a
    visitor who is already signed in is told to "sign in with your username
    and password", clicks through, and is bounced straight back to the app by
    /login — a dead-end loop that looks like the studio is broken.
    """
    if _require_login():
        return True
    raw = (request.cookies.get(REMEMBER_COOKIE) or "").strip()
    if not raw:
        return False
    try:
        from elfakgis.core import db as _db
        return bool(_db.remember_who(raw))
    except Exception as e:
        log.warning("remember-me lookup failed (%s)", e)
        return False


@auth_bp.route("/sso/forestry-handoff")
@_login_required
def sso_forestry_handoff():
    """
    Mint a short-lived token that signs the visitor in to Forestry PSC.

    The reverse of /sso/exchange. The sibling app keeps its session in
    localStorage, which a redirect cannot populate, so it accepts a token at
    its own /auth/sso/exchange and writes the session itself.
    """
    username = session.get("username") or ""
    token = autshared.mint_forestry_token(username) if username else None
    if not token:
        return jsonify({"ok": False, "error": "single sign-on unavailable"}), 503
    return jsonify({"ok": True, "token": token, "expires_in": 300})


@auth_bp.route("/sso/exchange", methods=["GET", "POST"])
@_rate_limit(limit=30, window=60)
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
        # Already signed in? The handoff is irrelevant — go straight to the
        # app instead of telling them to sign in again.
        if not request.args.get("json") and _already_signed_in():
            return Response("", status=303, headers={"Location": "/"})
        return Response(_sso_fallback_page(), status=401, mimetype="text/html")

    username = payload["u"]
    runs = _establish_session(username)
    log.info("SSO sign-in: %r from %s", username, _get_client_ip())

    if request.args.get("json") == "1":
        resp = jsonify({"ok": True, "username": username, "runs": runs[-20:]})
        # A verified handoff earns the same 30-day stay-signed-in cookie a
        # password login gets, so later direct visits need no password either.
        _issue_remember(resp, username)
        return resp

    # 303 so a reload never re-posts the token.
    resp = Response("", status=303, headers={"Location": "/"})
    _issue_remember(resp, username)
    return resp


def _sso_fallback_page():
    """Small self-contained page: explain, then hand back to the login form."""
    import html as _html
    return f"""<!DOCTYPE html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sign in · Elfak GIS Studio</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<style>
  :root {{ color-scheme: light dark; }}
  body {{ margin:0; min-height:100vh; display:flex; align-items:center; justify-content:center;
         font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
         background:#fafafa; color:#18181b; padding:24px; }}
  .card {{ background:#ffffff; border:1px solid #e4e4e7; border-radius:12px;
           padding:32px 28px; max-width:420px; text-align:center;
           box-shadow:0 12px 40px rgba(18,33,27,.12); }}
  h1 {{ font-size:20px; margin:0 0 8px; }}
  p  {{ font-size:14px; color:#3f3f46; line-height:1.6; margin:0 0 20px; }}
  /* Light-theme green with near-black label: 7.8:1, where white on the
     old #16833e was fine but white on the bright end was ~2.3:1. */
  a.btn {{ display:inline-block; background:#2fd06e; color:#06281a; text-decoration:none;
           padding:11px 22px; border-radius:10px; font-weight:600; font-size:14px; }}
  a.btn:hover {{ background:#3ee07a; }}
  .mark {{ width:44px; height:44px; border-radius:12px; margin:0 auto 16px;
           background:linear-gradient(135deg,#3ee07a,#2fd06e); color:#06281a;
           display:flex; align-items:center; justify-content:center;
           font-size:20px; font-weight:800; }}
</style></head>
<body><div class="card">
  <div class="mark" aria-hidden="true">F</div>
  <h1>One more step</h1>
  <p>We couldn't pass your sign-in across automatically.<br>
     Please sign in with the same username and password you use on Forestry PSC Preparation.</p>
  <a class="btn" href="/login">Go to sign in</a>
</div></body></html>"""


@auth_bp.route("/remember", methods=["POST"])
@_rate_limit(limit=30, window=60)
def remember():
    """Silent re-login from the stay-signed-in cookie.

    Used on page load when the Flask session is gone (restart/deploy/expiry)
    but the long-lived cookie is still valid. Missing/invalid credentials
    answer 200-with-empty (normal logged-out state, not console noise);
    only malformed requests are errors."""
    raw = request.cookies.get(REMEMBER_COOKIE, "")
    username = None
    if raw:
        try:
            from elfakgis.core import db as _db
            username = _db.remember_who(raw.strip())
        except Exception as e:
            log.warning("remember-me lookup failed (%s)", e)
    if not username:
        return jsonify({"username": None, "runs": []})
    runs = _establish_session(username, load_runs=False)
    log.info("Remember-me sign-in: %r from %s", username, _get_client_ip())
    return jsonify({"username": username, "runs": runs})


@auth_bp.route("/logout", methods=["POST"])
@csrf_protect
def logout():
    username = session.get("username")
    _logout_user(username)
    try:
        from elfakgis.core import db as _db
        _db.remember_revoke(username)
    except Exception as e:
        log.warning("remember-me revoke failed (%s)", e)
    session.clear()
    resp = jsonify({"ok": True})
    resp.delete_cookie(REMEMBER_COOKIE)
    if username:
        log.info("Logout: %r from %s", username, _get_client_ip())
    return resp


@auth_bp.route("/me")
def me():
    # 200-with-empty (not 401): probed on every page load, and a missing
    # session is the normal logged-out state — not an error worth logging
    # to the browser console on each visit. Runs backfill via /history.
    u = _require_login()
    if not u:
        return jsonify({"username": None, "runs": []})
    return jsonify({"username": u, "runs": []})


@auth_bp.route("/history")
@_login_required
def history():
    u = _require_login()
    return jsonify({"runs": _runs_for(u)})


def _purge_run_output(rid):
    """Delete a run's generated files from disk. Never raises."""
    try:
        from elfakgis.core.config import OUTPUT
        folder = _safe_path(OUTPUT, rid)
        if folder and os.path.isdir(folder):
            shutil.rmtree(folder, ignore_errors=True)
        zip_path = os.path.join(os.path.realpath(OUTPUT), f"{rid}.zip")
        if os.path.isfile(zip_path):
            os.remove(zip_path)
    except Exception as e:
        # A stuck file must never fail the delete the user asked for.
        log.warning("Could not purge output files for %r: %s", rid, e)


@auth_bp.route("/history/<run_id>", methods=["DELETE"])
@_login_required
@csrf_protect
@_rate_limit(limit=60, window=60)
def history_delete(run_id):
    """Delete one run from the signed-in user's history (record + files)."""
    u = _require_login()
    try:
        rid = _safe_runid(run_id)
    except Exception:
        return jsonify({"error": "Invalid run ID."}), 400
    removed = _delete_run(u, rid)
    if not removed:
        return jsonify({"error": "Run not found in your history."}), 404
    _purge_run_output(rid)
    with _PROG_LOCK:
        _PROG.pop(rid, None)
    log.info("History delete: %r run=%s", u, rid)
    return jsonify({"ok": True, "runs": _runs_for(u)})


@auth_bp.route("/history", methods=["DELETE"])
@_login_required
@csrf_protect
@_rate_limit(limit=10, window=60)
def history_clear():
    """Delete the signed-in user's entire run history."""
    u = _require_login()
    # Snapshot the run IDs first: _clear_runs() empties the store, so the
    # file purge has to work from what we captured beforehand.
    rids = [r.get("run_id") for r in _runs_for(u) if r.get("run_id")]
    removed = _clear_runs(u)
    for rid in rids:
        _purge_run_output(rid)
        with _PROG_LOCK:
            _PROG.pop(rid, None)
    log.info("History cleared: %r runs=%s", u, removed)
    return jsonify({"ok": True, "deleted": removed, "runs": []})


@auth_bp.route("/auth-capabilities")
def auth_capabilities():
    """What sign-in methods this deployment offers (used by diagnostics)."""
    try:
        from elfakgis.core import db as _db
        db_status = _db.status()
    except Exception:
        db_status = {"available": False, "reason": "error"}
    return jsonify({
        "shared_accounts": True,
        "sso": autshared.sso_enabled(),
        "shared_database": db_status.get("available", False),
        "db_reason": db_status.get("reason", "unknown"),
        # Sign-in fails closed unless the shared database can verify, or the
        # deployment explicitly opts into the auto-registering fallback.
        "fallback_enabled": _FALLBACK_ENABLED,
        # False means accounts must exist before they may sign in.
        "self_registration": _SELF_REGISTRATION_ALLOWED,
    })


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
