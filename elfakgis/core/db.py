"""Shared PostgreSQL access for Elfak GIS Pro Studio.

Elfak GIS runs as its own Render service but shares the Forestry PSC
PostgreSQL database, so a single Forestry PSC account signs in to both
products with the same username and password.

Two responsibilities:

1. **Credential verification.** Read the ``users`` table that Forestry PSC
   already maintains and verify the bcrypt hash locally. No network hop, and
   the studio keeps working even if the Forestry service is restarting.
2. **Run history.** Persist completed runs in ``gis_runs`` instead of the
   on-disk ``users.json``, which lives on Render's ephemeral filesystem and
   is therefore wiped on every redeploy.

Safety contract
---------------
Everything in here is optional and fails soft. If ``DATABASE_URL`` is unset,
unreachable, or the schema is missing, :func:`available` returns False and the
caller falls back to the original ``users.json`` behaviour. The studio is
never taken down by its shared database.
"""

import os
import threading
import time
from typing import List, Optional, Tuple

try:  # SQLAlchemy ships with the Forestry service; treat it as optional here.
    from sqlalchemy import create_engine, text
    _SA = True
except Exception:  # pragma: no cover
    create_engine = text = None
    _SA = False


_lock = threading.Lock()
_engine = None
_checked_at = 0.0
_ok = False
_RETRY_SECONDS = 30.0


from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


GIS_USERNAME_LEN = 100


def _database_url() -> str:
    return (os.getenv("DATABASE_URL") or "").strip()


def _normalize_database_url(raw: str) -> str:
    """
    Normalize a PostgreSQL URL without changing its credentials or options.

    This accepts Render-style ``postgres://`` URLs, selects SQLAlchemy's
    psycopg2 dialect explicitly, preserves Neon parameters such as
    ``sslmode`` and ``channel_binding``, and adds ``sslmode=require`` only
    when the deployment did not specify one.
    """
    url = (raw or "").strip()
    if not url:
        return ""
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    parts = urlsplit(url)
    if parts.scheme not in ("postgresql", "postgresql+psycopg2"):
        return url
    scheme = "postgresql+psycopg2" if parts.scheme == "postgresql" else parts.scheme
    query = parse_qsl(parts.query, keep_blank_values=True)
    if "sslmode" not in {name for name, _ in query}:
        query.append(("sslmode", "require"))
    return urlunsplit((scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _connect():
    """Return a live engine, or None. Re-probes at most every 30s."""
    global _engine, _checked_at, _ok
    if not _SA:
        return None
    url = _normalize_database_url(_database_url())
    if not url:
        return None
    with _lock:
        if _ok and _engine is not None and (time.time() - _checked_at) < _RETRY_SECONDS:
            return _engine
        try:
            _engine = create_engine(
                url,
                pool_pre_ping=True,
                pool_size=3,
                max_overflow=2,
                pool_recycle=600,
                connect_args={"connect_timeout": 8},
            )
            with _engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            _ok = True
            _checked_at = time.time()
            return _engine
        except Exception:
            _engine = None
            _ok = False
            _checked_at = time.time()
            return None


def available() -> bool:
    """True when the shared database is usable right now."""
    return _connect() is not None


def _exec(sql: str, params: Optional[dict] = None):
    eng = _connect()
    if eng is None:
        return None
    try:
        with eng.begin() as conn:
            return conn.execute(text(sql), params or {})
    except Exception:
        return None


# ── Credential verification ────────────────────────────────────────

def get_password_hash(username: str) -> Optional[str]:
    """
    Return the stored password hash for ``username`` from the shared
    ``users`` table, or None when unknown/unavailable.

    Mirrors Forestry PSC's own storage: modern accounts hold a bcrypt hash
    (``$2…``); legacy accounts may still hold plaintext, which that service
    upgrades on first login.
    """
    row = _exec(
        "SELECT password FROM users WHERE username = :u LIMIT 1", {"u": username}
    )
    if row is None:
        return None
    try:
        return row.first()[0]
    except Exception:
        return None


def user_exists(username: str) -> bool:
    return get_password_hash(username) is not None


# ── Run history ───────────────────────────────────────────────────

_GIS_USERNAME_COLUMNS = (
    ("gis_user_cache", "username"),
    ("gis_runs", "username"),
    ("gis_seen", "username"),
    ("gis_remember", "username"),
)


def _ensure_username_length(table: str, column: str) -> bool:
    """
    Keep GIS-owned username columns as wide as Forestry's ``users.username``.

    Only GIS-owned tables are inspected or altered. Forestry's own schema is
    never modified here.
    """
    if (table, column) not in _GIS_USERNAME_COLUMNS:
        return False
    row = _exec(
        """
        SELECT character_maximum_length
        FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c
        """,
        {"t": table, "c": column},
    )
    if row is None:
        return False
    try:
        width = row.first()[0]
    except Exception:
        return False
    if width is None or int(width) >= GIS_USERNAME_LEN:
        return True
    altered = _exec(
        f"ALTER TABLE {table} ALTER COLUMN {column} TYPE VARCHAR({GIS_USERNAME_LEN})"
    )
    return altered is not None


def _ensure_tables() -> bool:
    """Create the GIS-side tables if they are missing. Idempotent."""
    ok = _exec(
        """
        CREATE TABLE IF NOT EXISTS gis_user_cache (
            username   VARCHAR(100) PRIMARY KEY,
            pw_hash    TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT NOW()
        )
        """
    )
    if ok is None:
        return False
    ok2 = _exec(
        """
        CREATE TABLE IF NOT EXISTS gis_runs (
            id          SERIAL PRIMARY KEY,
            username    VARCHAR(100) NOT NULL,
            run_id      VARCHAR(128) NOT NULL,
            module      VARCHAR(64),
            description TEXT,
            created_at  TIMESTAMP DEFAULT NOW()
        )
        """
    )
    if ok2 is None:
        return False
    _exec("CREATE INDEX IF NOT EXISTS gis_runs_user_idx ON gis_runs (username, id DESC)")
    ok3 = _exec(
        """
        CREATE TABLE IF NOT EXISTS gis_remember (
            username    VARCHAR(100) PRIMARY KEY,
            token_hash  TEXT NOT NULL,
            expires_at  TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '30 days',
            created_at  TIMESTAMP DEFAULT NOW()
        )
        """
    )
    if ok3 is None:
        return False
    ok4 = _exec(
        """
        CREATE TABLE IF NOT EXISTS gis_seen (
            username    VARCHAR(100) PRIMARY KEY,
            first_seen  TIMESTAMP DEFAULT NOW()
        )
        """
    )
    if ok4 is None:
        return False
    for table, column in _GIS_USERNAME_COLUMNS:
        if not _ensure_username_length(table, column):
            return False
    return True


def cache_hash(username: str, pw_hash: str) -> None:
    """
    Remember a verified bcrypt hash so later logins do not depend on the
    Forestry service being reachable.
    """
    if not username or not pw_hash:
        return
    _exec(
        """
        INSERT INTO gis_user_cache (username, pw_hash) VALUES (:u, :h)
        ON CONFLICT (username) DO UPDATE SET pw_hash = EXCLUDED.pw_hash
        """,
        {"u": username, "h": pw_hash},
    )


def cached_hash(username: str) -> Optional[str]:
    if not _ensure_tables():
        return None
    row = _exec(
        "SELECT pw_hash FROM gis_user_cache WHERE username = :u LIMIT 1", {"u": username}
    )
    if row is None:
        return None
    try:
        return row.first()[0]
    except Exception:
        return None


def add_run(username: str, run_id: str, module: str, description: str = "") -> bool:
    """Append a run record. Returns False when it could not be stored."""
    if not username or not run_id:
        return False
    if not _ensure_tables():
        return False
    # Keep each user's history bounded, matching the old 100-record cap.
    _exec(
        """
        INSERT INTO gis_runs (username, run_id, module, description)
        VALUES (:u, :r, :m, :d)
        """,
        {"u": username, "r": run_id, "m": module, "d": (description or "")[:200]},
    )
    _exec(
        """
        DELETE FROM gis_runs
        WHERE username = :u AND id NOT IN (
            SELECT id FROM gis_runs WHERE username = :u ORDER BY id DESC LIMIT 100
        )
        """,
        {"u": username},
    )
    return True


def get_runs(username: str, limit: int = 100) -> Optional[List[dict]]:
    """
    Return run history, newest first. None means "no database", which tells
    the caller to fall back to users.json.
    """
    if not username or not _ensure_tables():
        return None
    rows = _exec(
        """
        SELECT run_id, module, description, created_at
        FROM gis_runs WHERE username = :u
        ORDER BY id DESC LIMIT :lim
        """,
        {"u": username, "lim": int(limit)},
    )
    if rows is None:
        return None
    try:
        return [
            {
                "run_id": r[0],
                "module": r[1],
                "description": r[2] or "",
                "timestamp": str(r[3]),
            }
            for r in rows.fetchall()
        ]
    except Exception:
        return None


# ── Stay-signed-in tokens ────────────────────────────────────────
# One random token per user (sha256 at rest, 30-day sliding expiry) backs the
# long-lived HttpOnly cookie. Survives restarts/deploys via shared Postgres;
# every function fails soft (None/False) so auth degrades to plain login.

import hashlib as _hashlib
import secrets as _secrets

REMEMBER_DAYS = 30


def remember_issue(username: str) -> Optional[str]:
    """Create (or rotate) a stay-signed-in token. Returns the raw token."""
    if not username or not _ensure_tables():
        return None
    raw = _secrets.token_hex(32)
    digest = _hashlib.sha256(raw.encode()).hexdigest()
    ok = _exec(
        """
        INSERT INTO gis_remember (username, token_hash, expires_at)
        VALUES (:u, :h, NOW() + INTERVAL '30 days')
        ON CONFLICT (username) DO UPDATE SET
            token_hash = EXCLUDED.token_hash,
            expires_at = EXCLUDED.expires_at,
            created_at = NOW()
        """,
        {"u": username, "h": digest},
    )
    return raw if ok is not None else None


def remember_who(raw: str) -> Optional[str]:
    """Validate a presented token → username (renews sliding expiry)."""
    if not raw or not _ensure_tables():
        return None
    digest = _hashlib.sha256(raw.encode()).hexdigest()
    row = _exec(
        "SELECT username FROM gis_remember "
        "WHERE token_hash = :h AND expires_at > NOW() LIMIT 1",
        {"h": digest},
    )
    if row is None:
        return None
    try:
        username = row.first()[0]
    except Exception:
        return None
    if not username:
        return None
    _exec(
        "UPDATE gis_remember SET expires_at = NOW() + INTERVAL '30 days' "
        "WHERE username = :u",
        {"u": username},
    )
    _exec("DELETE FROM gis_remember WHERE expires_at <= NOW()")
    return username


def remember_revoke(username: str) -> None:
    """Drop a user's stay-signed-in token (logout). Never raises."""
    if not username:
        return
    try:
        _exec("DELETE FROM gis_remember WHERE username = :u", {"u": username})
    except Exception:
        pass


# ── First-seen tracking (truthful "new account" message) ─────────────
# Login accounts are owned by Forestry; GIS must not claim "created" on
# every sign-in. This records each username once: True only the very first
# time, False forever after. Fails soft to an in-memory set (per worker)
# so a DB outage can at worst repeat the message, never break login.

_seen_fallback: set = set()
_seen_lock = threading.Lock()


def mark_seen(username: str) -> bool:
    """Return True exactly once per username (first GIS sign-in ever)."""
    if not username:
        return False
    if _ensure_tables():
        row = _exec(
            "INSERT INTO gis_seen (username) VALUES (:u) "
            "ON CONFLICT (username) DO NOTHING RETURNING username",
            {"u": username},
        )
        if row is not None:
            try:
                return row.first() is not None
            except Exception:
                return False
    with _seen_lock:
        if username in _seen_fallback:
            return False
        _seen_fallback.add(username)
        return True
