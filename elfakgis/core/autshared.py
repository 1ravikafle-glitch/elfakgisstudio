"""Shared-account authentication for Elfak GIS Studio.

The studio accepts exactly the accounts that Forestry PSC Preparation
accepts. There is no separate GIS account system: one username, one
password, both sites.

Verification order (cheapest first, and resilient by design):

1. **Local cache** — a bcrypt hash stored in ``gis_user_cache`` from a
   previous successful sign-in. No network involved.
2. **Shared database** — the ``users`` table maintained by Forestry PSC.
   Verified in-process, so the studio still works while the Forestry
   service is restarting.
3. **Forestry HTTP API** — a real ``/auth/login`` call, used when no
   database is configured.

Only when all three are unavailable does sign-in fail, and it fails
closed: an unreachable backend never grants access.

Legacy compatibility: Forestry PSC auto-creates accounts on first login and
historically stored some passwords in plaintext, upgrading them to bcrypt
on the next successful sign-in. This module understands both forms, so no
existing account is locked out.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
from typing import Optional, Tuple

from elfakgis.core import db

log = logging.getLogger("elfakgis")

# Only used when the shared database is unavailable.
FORESTRY_AUTH_URL = (
    os.environ.get("FORESTRY_AUTH_URL")
    or "https://forestry-pscpreparation.onrender.com/auth/login"
).strip()

SSO_AUDIENCE = "elfakgisstudio"

# Handoff links minted before the product was renamed carry the old name in
# their audience claim. They are still signed by the same shared secret and
# still mean "this user is signed in on the sibling site", so they are
# accepted rather than failing closed — otherwise every link already in
# circulation, and any sibling site that has not been updated yet, breaks
# at once. New tokens should use SSO_AUDIENCE.
SSO_AUDIENCE_ALIASES = frozenset({
    "elfakgisprostudio",
})

# Optional ceiling on how long a handoff token may claim to be valid.
# 0 (the default) means no cap, for compatibility with siblings that mint
# long-lived tokens. See docs/SINGLE-SIGN-ON.md: a token in a URL is a
# bearer credential that anyone with the link can replay, so the
# recommended value is a few minutes.
SSO_MAX_TTL = int(os.environ.get("SSO_MAX_TTL", "0") or 0)


# ── Password verification ─────────────────────────────────────────

def _bcrypt_check(password: str, stored: str) -> Optional[bool]:
    """Return True/False on a verdict, or None when the hash is unreadable."""
    if not stored:
        return None
    if stored.startswith("$2"):
        try:
            import bcrypt
        except Exception:
            return None
        try:
            return bool(bcrypt.checkpw(password.encode("utf-8"), stored.encode("utf-8")))
        except Exception:
            return None
    # Legacy plaintext (mirrors Forestry PSC's own fallback).
    return hmac.compare_digest(stored, password)


def _bcrypt_hash(password: str) -> Optional[str]:
    try:
        import bcrypt
    except Exception:
        return None
    try:
        return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")
    except Exception:
        return None


def _verify_via_forestry_api(username: str, password: str) -> Optional[bool]:
    """Ask the Forestry PSC service to check the credentials. None = unreachable."""
    try:
        import urllib.request
    except Exception:
        return None
    payload = json.dumps({"username": username, "password": password}).encode("utf-8")
    req = urllib.request.Request(
        FORESTRY_AUTH_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            if resp.status == 200:
                return True
            if resp.status in (400, 401, 403):
                return False
            return None
    except Exception as exc:
        # 401/403 arrive as HTTPError; anything else is a real outage.
        code = getattr(exc, "code", None)
        if code in (400, 401, 403):
            return False
        log.warning("Forestry auth unreachable (%s); cannot verify %r", exc, username)
        return None


def verify_credentials(username: str, password: str) -> Tuple[bool, str]:
    """
    Verify a username/password pair.

    Returns ``(ok, reason)`` where reason is a short machine-readable tag:
    ``ok``, ``invalid`` or ``unavailable``.
    """
    username = (username or "").strip()
    if not username or not password:
        return False, "invalid"

    # 1) local cache
    cached = db.cached_hash(username)
    verdict = _bcrypt_check(password, cached) if cached else None
    if verdict is True:
        return True, "ok"
    if verdict is False:
        return False, "invalid"

    # 2) shared database
    stored = db.get_password_hash(username)
    if stored:
        verdict = _bcrypt_check(password, stored)
        if verdict is True:
            # Remember it so the next sign-in skips the lookup entirely.
            if stored.startswith("$2"):
                db.cache_hash(username, stored)
            return True, "ok"
        if verdict is False:
            return False, "invalid"

    # 3) Forestry PSC API
    verdict = _verify_via_forestry_api(username, password)
    if verdict is True:
        fresh = _bcrypt_hash(password)
        if fresh and db.available():
            db.cache_hash(username, fresh)
        return True, "ok"
    if verdict is False:
        return False, "invalid"

    return False, "unavailable"


def user_is_known(username: str) -> bool:
    """True when the account exists in any store we can see."""
    if db.user_exists(username):
        return True
    cached = db.cached_hash(username)
    if cached:
        return True
    return False


# ── Single sign-on (handoff from Forestry PSC) ─────────────────────

def _sso_secret() -> Optional[bytes]:
    raw = (os.environ.get("SSO_SECRET") or "").strip()
    if len(raw) < 16:
        return None
    return raw.encode("utf-8")


def sso_enabled() -> bool:
    return _sso_secret() is not None


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def verify_sso_token(token: str) -> Optional[dict]:
    """
    Validate a handoff token minted by Forestry PSC.

    Returns the payload on success, None when SSO is unconfigured, the token
    is malformed, the signature does not match, the audience is wrong, or it
    has expired.
    """
    secret = _sso_secret()
    if secret is None or not token or token.count(".") != 1:
        return None
    body, _, sig = token.partition(".")
    if not body or not sig:
        return None
    expected = _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, sig):
        return None
    try:
        payload = json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    audience = payload.get("aud")
    if audience != SSO_AUDIENCE and audience not in SSO_AUDIENCE_ALIASES:
        return None
    if not payload.get("u"):
        return None
    try:
        exp = int(payload.get("exp", 0))
        if exp < int(time.time()):
            return None
        # When a ceiling is configured, a token that claims to live far
        # longer than a sign-in handoff should is refused outright.
        iat = int(payload.get("iat", 0) or 0)
        if SSO_MAX_TTL and iat and (exp - iat) > SSO_MAX_TTL:
            log.warning("SSO token rejected: lifetime %ds exceeds SSO_MAX_TTL %ds",
                        exp - iat, SSO_MAX_TTL)
            return None
    except Exception:
        return None
    return payload
