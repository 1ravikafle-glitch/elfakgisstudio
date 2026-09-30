"""Elfak GIS Studio — CSRF protection.

Every state-changing route is cookie-authenticated: the Flask session cookie
and the 30-day ``elfak_rem`` cookie both ride along automatically on any
cross-site request the browser decides to make. ``SameSite=Lax`` blocks the
*cross-site POST* case in current browsers, but it is a single layer that
has been bypassed before (top-level GET navigations, some redirect chains,
older clients) and it protects nothing once a browser is old or lenient.

So the state-changing routes carry their own token as well. The token is
derived from the signed session cookie rather than stored server-side, so
there is no lookup, no per-session table, and nothing to invalidate: a
rotation of SECRET_KEY invalidates every token at once, which is the
behaviour you want anyway.

This is deliberately a ~40-line implementation rather than a Flask-WTF
dependency — the studio is otherwise dependency-light for a good reason
(GIS wheels are enormous and slow to build on Render).
"""
import hmac
import logging
import secrets
from functools import wraps

from flask import request, session, jsonify

log = logging.getLogger("elfakgis")

CSRF_SESSION_KEY = "_csrf"
CSRF_HEADER = "X-CSRF-Token"
CSRF_FORM_FIELD = "csrf_token"
# Read from the JSON body, a form field, or the header — the frontend is a
# mix of fetch() and FormData, and both must work without special-casing.
CSRF_SAFE_METHODS = ("GET", "HEAD", "OPTIONS", "TRACE")


def generate_csrf_token() -> str:
    """Return this session's CSRF token, minting one on first use."""
    token = session.get(CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[CSRF_SESSION_KEY] = token
    return token


def _is_valid(token) -> bool:
    expected = session.get(CSRF_SESSION_KEY)
    if not expected or not token or not isinstance(token, str):
        return False
    return hmac.compare_digest(expected, token)


def _extract_token():
    # Header first: this is what every fetch() in the app sends.
    token = request.headers.get(CSRF_HEADER, "")
    if token:
        return token.strip()
    # Then a form field, for any non-JS submit.
    if request.form:
        token = (request.form.get(CSRF_FORM_FIELD) or "").strip()
        if token:
            return token
    # Then the JSON body. force=True is silent when there is no body, and
    # cheap when the route is multipart (which never carries JSON).
    data = request.get_json(silent=True)
    if isinstance(data, dict):
        return str(data.get(CSRF_FORM_FIELD) or data.get("_csrf") or "").strip()
    return ""


def csrf_protect(fn):
    """Reject a state-changing request that carries no valid CSRF token.

    Applied to authenticated, state-changing routes only. Safe methods pass
    through untouched, so a plain GET of a map or a page load never breaks.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method in CSRF_SAFE_METHODS:
            return fn(*args, **kwargs)
        # No session means no authenticated action to forge — the route's own
        # auth decorator answers 401 — so there is nothing to protect here.
        if not session.get("username"):
            return fn(*args, **kwargs)
        if _is_valid(_extract_token()):
            return fn(*args, **kwargs)
        log.warning("CSRF rejected: %s %s from %s",
                    request.method, request.path, request.remote_addr)
        return jsonify({
            "error": "Security token missing or invalid. Reload the page and try again.",
        }), 403
    return wrapper
