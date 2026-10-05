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

# ── How credentials may be verified when the shared database is down ──
#
# The tiers are tried in order:
#   1. the shared Postgres accounts table (the intended design)
#   2. THIS service, which auto-registers on first sign-in
#
# Tier 2 is not a credential check. It creates an account from whatever
# username and password arrive, then reports success. Treating that as
# "the password is correct" means /login hands a working session to anyone
# who can reach it. It is also not self-defending: the first attempt mints
# the account, so a *second* attempt with the same invented credentials
# looks like a returning user and passes any "did this account exist?" gate.
#
# So the fallback is opt-in and off by default. With neither the database
# nor the fallback configured, verification fails closed and /login answers
# 503. That is the correct failure mode: locked out, rather than open.
#
# Set AUTH_SHARED_FALLBACK=api to re-enable the Forestry check (accepting
# that anyone who can reach /login can register), and additionally
# ALLOW_SELF_REGISTRATION=1 to let those freshly created accounts sign in.
_SHARED_FALLBACK = (os.environ.get("AUTH_SHARED_FALLBACK", "none").strip().lower())
_SELF_REGISTRATION_ALLOWED = (
    os.environ.get("ALLOW_SELF_REGISTRATION", "0").strip().lower()
    in ("1", "true", "yes", "on")
)


def fallback_enabled() -> bool:
    """Whether the auto-registering Forestry API may be consulted."""
    return _SHARED_FALLBACK in ("api", "forestry", "on", "1", "true", "yes")


def self_registration_allowed() -> bool:
    """Whether an account the fallback just created may sign in."""
    return _SELF_REGISTRATION_ALLOWED


# Used when the shared database is unavailable *and* the fallback is enabled.
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

# Ceiling on how long a handoff token may claim to be valid. The default is
# 5 minutes rather than 0 (unlimited): a token in a URL is a bearer
# credential that anyone holding the link can replay, and a long-lived one
# leaks through browser history, proxy logs and Referer headers for as long
# as it is valid. A handoff is a redirect that happens immediately, so five
# minutes is generous; set SSO_MAX_TTL=0 only to restore unlimited lifetime
# for a sibling that genuinely mints long-lived tokens.
SSO_MAX_TTL = int(os.environ.get("SSO_MAX_TTL", "300") or 0)


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


def _verify_via_forestry_api(username: str, password: str) -> Tuple[Optional[bool], bool]:
    """Ask the Forestry PSC service to check the credentials.

    Returns ``(verdict, is_new)`` where verdict is True/False/None
    (True = good password, False = rejected, None = unreachable) and
    ``is_new`` says whether Forestry *created* the account as part of this
    request.

    That second value matters. Forestry auto-registers on first sign-in, so
    a 200 does not prove the account existed — it may mean "welcome, I just
    made you an account for any password you typed". A studio that treats
    that as proof of authentication is, in effect, open to anyone who can
    reach /login.
    """
    if not username or not password:
        return False, False
    try:
        import urllib.request
    except Exception:
        return None, False
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
                is_new = False
                try:
                    body = json.loads(resp.read(65536) or b"{}")
                    if isinstance(body, dict):
                        is_new = bool(body.get("is_new"))
                except Exception:
                    # An unreadable body is not a reason to reject a
                    # password the service just accepted; assume existing.
                    pass
                return True, is_new
            if resp.status in (400, 401, 403):
                return False, False
            return None, False
    except Exception as exc:
        # 401/403 arrive as HTTPError; anything else is a real outage.
        code = getattr(exc, "code", None)
        if code in (400, 401, 403):
            return False, False
        log.warning("Forestry auth unreachable (%s); cannot verify %r", exc, username)
        return None, False


def verify_credentials(username: str, password: str) -> Tuple[bool, str]:
    """
    Verify a username/password pair.

    Returns ``(ok, reason)`` where reason is a short machine-readable tag:
    ``ok`` (known account, correct password), ``new`` (Forestry just
    auto-created the account from this attempt), ``invalid`` or
    ``unavailable``.
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
            else:
                # Legacy plaintext account that just signed in successfully.
                # Upgrade it now, while we hold the plaintext: this is the
                # only moment the real password is available to hash. The
                # shared `users` table is owned by Forestry PSC, so only the
                # local cache is rewritten here — Forestry performs the same
                # upgrade on its side.
                fresh = _bcrypt_hash(password)
                if fresh and db.available():
                    db.cache_hash(username, fresh)
                    log.info("Upgraded a legacy plaintext account to bcrypt: %r", username)
            return True, "ok"
        if verdict is False:
            return False, "invalid"

    # 3) Forestry PSC API — opt-in only, see the note on _SHARED_FALLBACK.
    #    This tier registers unknown accounts, so it is not evidence of a
    #    correct password; it is off unless a deployment asks for it.
    if not fallback_enabled():
        # Distinguish "we asked and the user isn't there" from "we couldn't
        # ask". With the database reachable and no `users` row, the account
        # genuinely does not exist and the honest answer is a rejected login.
        # Reporting that as "unavailable" would turn every mistyped username
        # into an HTTP 503 and tell a real user the site is broken.
        if db.available():
            return False, "invalid"
        log.warning(
            "Cannot verify %r: no shared database and the Forestry API "
            "fallback is disabled (AUTH_SHARED_FALLBACK=none). Failing closed.",
            username,
        )
        return False, "unavailable"

    verdict, is_new = _verify_via_forestry_api(username, password)
    if verdict is True:
        fresh = _bcrypt_hash(password)
        if fresh and db.available():
            db.cache_hash(username, fresh)
        if is_new:
            # Forestry just minted this account from whatever password was
            # typed. It is a real login, but it is NOT evidence that the
            # visitor knows a pre-existing secret, so it is reported
            # distinctly and the route decides whether to admit it.
            log.warning("Account auto-created by Forestry on first sign-in: %r", username)
            return True, "new"
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


# Audience for the reverse handoff: a token naming the Forestry app, so a
# signed-in GIS session can open it already authenticated. Distinct from
# SSO_AUDIENCE so neither direction's token can be replayed as the other.
FORESTRY_AUDIENCE = os.environ.get("SSO_AUDIENCE_OUTBOUND") or "forestrypscprep"


def mint_forestry_token(username: str, is_admin: bool = False) -> Optional[str]:
    """
    Mint a short-lived token the Forestry app will accept as itself.

    The sibling app keeps its session in localStorage, so it cannot be handed a
    session by redirect alone; it accepts this token at /auth/sso/exchange,
    writes its own session and navigates on. Lives in a URL, so it is short.
    """
    secret = _sso_secret()
    if secret is None or not username:
        return None
    payload = {
        "u": username,
        "a": 1 if is_admin else 0,
        "aud": FORESTRY_AUDIENCE,
        "iat": int(time.time()),
        "exp": int(time.time()) + min(SSO_MAX_TTL or 300, 300),
    }
    body = _b64e(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8"))
    sig = _b64e(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return f"{body}.{sig}"


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
