"""Elfak GIS Studio — rate limits, validation, path safety (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import session, request, jsonify, abort

log = logging.getLogger("elfakgis")
from elfakgis.core.config import OUTPUT, UPLOAD_MAX_BYTES
def _cleanup_old_outputs():
    while True:
        time.sleep(1800)
        try:
            dirs = [(os.path.join(OUTPUT, d),
                     os.path.getmtime(os.path.join(OUTPUT, d)))
                    for d in os.listdir(OUTPUT)
                    if os.path.isdir(os.path.join(OUTPUT, d))]
            dirs.sort(key=lambda x: x[1])
            for path, _ in dirs[:-500]:
                shutil.rmtree(path, ignore_errors=True)
        except Exception as e:
            log.error(f"Cleanup error: {e}")

_RL: dict = defaultdict(list)
_RL_LOCK = threading.Lock()


def _proxy_trusted() -> bool:
    """Whether a reverse proxy sits in front of this process.

    Set TRUST_PROXY=1/0 to decide explicitly. Otherwise it is inferred:
    Render sets RENDER=true and terminates TLS itself, while a bare local
    process talks to the client directly. Getting this wrong is not
    cosmetic — see _get_client_ip.
    """
    explicit = (os.environ.get("TRUST_PROXY") or "").strip().lower()
    if explicit in ("1", "true", "yes", "on"):
        return True
    if explicit in ("0", "false", "no", "off"):
        return False
    return bool(os.environ.get("RENDER")) or (
        (os.environ.get("APP_ENV") or "").strip().lower() == "production")


# Captured at import so _get_client_ip's fast path is a plain global read on
# what is a hot path: every rejected sign-in calls it.
_PROXY_TRUSTED = _proxy_trusted()

def _rate_limit(limit=30, window=60, key_fn=None):
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = _get_client_ip()
            key = (key_fn(request) if key_fn else ip)
            now = time.time()
            with _RL_LOCK:
                _RL[key] = [t for t in _RL[key] if now - t < window]
                if len(_RL[key]) >= limit:
                    retry_after = int(window - (now - _RL[key][0])) + 1
                    log.warning(f"Rate limit hit: {key} on {request.path}")
                    return jsonify({"error": f"Too many requests. Try again in {retry_after}s.",
                                    "retry_after": retry_after}), 429
                _RL[key].append(now)
            return fn(*args, **kwargs)
        return wrapper
    return decorator


# ── Credential-stuffing lockout ──────────────────────────────────────
# Per-IP limits are useless against an attacker with a proxy list, and
# useless outright against a botnet: each source gets its own bucket. A
# per-account limiter is what actually protects a known username, so failed
# sign-ins are counted against the *account* as well as the caller. The
# window grows with the failure count, so guessing one account slowly
# (~10 tries/hour) is the only thing that survives, while a user who fat-
# fingers their password a few times is not locked out for long.
#
# Only failures count. A success clears the account's counter so a legitimate
# user who typos three times is not punished afterwards.
_FAIL_LOCK: dict = defaultdict(list)
_FAIL_LOCK_MAX = 8
_FAIL_WINDOW_BASE = 300          # first lockout lasts 5 minutes
_FAIL_WINDOW_STEP = 300          # +5 minutes per extra failure
_FAIL_LOCK_MAX_WINDOW = 3600    # never lock longer than an hour


def _fail_key(username):
    return f"fail:{str(username or '').strip().lower()[:100]}"


def _lockout_seconds(username):
    """Remaining lockout for this account, or 0 when it may try again."""
    now = time.time()
    with _RL_LOCK:
        stamps = [t for t in _FAIL_LOCK.get(_fail_key(username), []) if now - t < 3600]
        if not stamps:
            _FAIL_LOCK.pop(_fail_key(username), None)
            return 0
        # The penalty is set by the count *before* pruning, so it stays put
        # for the whole window even as old entries age out of the list.
        # The 8th failure is the one that locks: the first 7 are a real user
        # fat-fingering, the 8th is guessing.
        over = len(stamps) - _FAIL_LOCK_MAX + 1
        if over <= 0:
            _FAIL_LOCK[_fail_key(username)] = stamps
            return 0
        window = min(_FAIL_WINDOW_BASE + over * _FAIL_WINDOW_STEP, _FAIL_LOCK_MAX_WINDOW)
        retry = int(window - (now - stamps[0])) + 1
        if retry <= 0:
            return 0
        _FAIL_LOCK[_fail_key(username)] = stamps
        return retry


def _record_login_failure(username):
    with _RL_LOCK:
        key = _fail_key(username)
        now = time.time()
        _FAIL_LOCK[key] = [t for t in _FAIL_LOCK.get(key, []) if now - t < 3600]
        _FAIL_LOCK[key].append(now)
        return len(_FAIL_LOCK[key])


def _clear_login_failures(username):
    with _RL_LOCK:
        _FAIL_LOCK.pop(_fail_key(username), None)


def login_guard(fn):
    """Per-account lockout around the sign-in handler.

    Ordered *inside* @_rate_limit: the IP bucket sheds broad floods first,
    then this one sheds focused guessing against a single known account.
    """
    @wraps(fn)
    def wrapper(*args, **kwargs):
        data = request.get_json(silent=True) or {}
        raw = data.get("username", "") if isinstance(data, dict) else ""
        username = raw if isinstance(raw, str) else ""
        retry = _lockout_seconds(username)
        if retry:
            log.warning("Sign-in locked out for %r from %s (%ds left)",
                        username, _get_client_ip(), retry)
            return jsonify({
                "error": f"Too many failed sign-in attempts. Try again in {retry // 60 + 1} minute(s).",
                "retry_after": retry,
            }), 429
        resp = fn(*args, **kwargs)
        # A view may answer with a Response or with a (body, status) tuple, so
        # normalise before inspecting. Only a *rejected* attempt (401) counts
        # against the account: a 503 means the credential backend was down,
        # which is our failure, not the visitor's, and must never lock a real
        # user out of their own account.
        status = resp[1] if (isinstance(resp, tuple) and len(resp) == 2) else getattr(resp, "status_code", 200)
        if status == 401:
            _record_login_failure(username)
        elif status < 400:
            _clear_login_failures(username)
        return resp
    return wrapper

def _cool_down(seconds=2):
    """Minimal per-IP cooldown for heavy pipeline routes (double-submit
    guard). Maximum wait is `seconds` — no blocking rate limits."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            ip = _get_client_ip()
            key = f"cd:{request.path}:{ip}"
            now = time.time()
            with _RL_LOCK:
                last = _RL.get(key, [0])[-1] if _RL.get(key) else 0
                if now - last < seconds:
                    wait = round(seconds - (now - last), 1)
                    return jsonify({"error": f"Please wait {wait}s and retry.",
                                    "retry_after": wait}), 429
                _RL[key] = [now]
            return fn(*args, **kwargs)
        return wrapper
    return decorator

def _clean_rl():
    while True:
        time.sleep(300)
        with _RL_LOCK:
            now = time.time()
            dead = [k for k, ts in _RL.items() if not ts or now - ts[-1] > 3600]
            for k in dead: del _RL[k]
            dead_fail = [k for k, ts in _FAIL_LOCK.items() if not ts or now - ts[-1] > 3600]
            for k in dead_fail: del _FAIL_LOCK[k]

# NOTE: this thread is started by the app factory (elfakgis.create_app), not
# at import time. Starting it here as well spawned two sweepers per process,
# which doubled the lock contention for no benefit.

# ── Archive / upload guards ─────────────────────────────────────────
# A ZIP is a container that can declare far more content than it carries: a
# 42 KB file can list 10^12 members and still open instantly. Any code that
# walks namelist() or extracts must bound the entry count and the declared
# uncompressed size first, or a single upload is a denial of service.
ZIP_MAX_ENTRIES = 20000           # real shapefile zips are a few hundred files
ZIP_MAX_TOTAL_UNCOMPRESSED = 4 * 1024 * 1024 * 1024   # 4 GB declared
ZIP_MAX_COMPRESSION_RATIO = 200   # guards against a "zip bomb" of zeros


class ArchiveTooLarge(ValueError):
    """Raised when an archive exceeds the entry/size/ratio budget."""


def _assert_zip_within_budget(zf, max_entries=None, max_total=None, max_ratio=None):
    """Reject an archive whose declared contents are unreasonable.

    Reads only the central directory (no decompression), so it is cheap
    enough to run on every upload. Raises :class:`ArchiveTooLarge`.
    """
    max_entries = ZIP_MAX_ENTRIES if max_entries is None else max_entries
    max_total = ZIP_MAX_TOTAL_UNCOMPRESSED if max_total is None else max_total
    max_ratio = ZIP_MAX_COMPRESSION_RATIO if max_ratio is None else max_ratio

    infos = zf.infolist()
    if len(infos) > max_entries:
        raise ArchiveTooLarge(
            f"Archive lists {len(infos)} entries (limit {max_entries}).")

    total = 0
    for info in infos:
        total += int(info.file_size or 0)
        if total > max_total:
            raise ArchiveTooLarge(
                f"Archive declares more than {max_total // (1024 ** 3)} GB uncompressed.")
    # Compare against the compressed size actually on disk. Tiny archives
    # legitimately compress well, so the ratio only bites once there is
    # enough compressed data for the ratio to mean something.
    if total > 8 * 1024 * 1024:
        compressed = sum(int(i.compress_size or 0) for i in infos) or 1
        if total / compressed > max_ratio:
            raise ArchiveTooLarge(
                f"Archive compression ratio {total / compressed:.0f}:1 exceeds {max_ratio}:1.")
    return infos


def _assert_upload_size(path, max_bytes=None):
    """Reject a file on disk that is larger than the service should accept."""
    limit = max_bytes or UPLOAD_MAX_BYTES
    try:
        size = os.path.getsize(path)
    except OSError:
        return 0
    if size > limit:
        raise ValueError(
            f"File is {size / (1024 ** 2):.0f} MB (limit {limit // (1024 ** 2)} MB).")
    return size


_ALLOWED_EXTS = {
    ".csv", ".xls", ".xlsx", ".zip",
    ".shp", ".dbf", ".prj", ".shx", ".cpg",
    ".tif", ".tiff"
}
_BLOCKED_NAMES = {
    "admin","root","system","null","undefined","guest","test","demo",
    "anonymous","api","static","login","logout","upload","download",
    "server","config","env","app","index","user","users","data",
    "script","style","public","private","backend","frontend"
}

def _safe_filename(fname):
    fname = os.path.basename((fname or "upload").replace("\\", "/"))
    fname = re.sub(r"[^A-Za-z0-9._-]", "_", fname)[:200]
    if not fname: fname = "upload"
    ext = os.path.splitext(fname)[1].lower()
    if ext not in _ALLOWED_EXTS:
        raise ValueError(f"File type '{ext}' not allowed. Allowed: {sorted(_ALLOWED_EXTS)}")
    return fname

def _safe_path(base, rel):
    base = os.path.realpath(os.path.abspath(base))
    full = os.path.realpath(os.path.abspath(os.path.join(base, str(rel))))
    if not (full == base or full.startswith(base + os.sep)):
        log.warning(f"Path traversal blocked: base={base!r} rel={rel!r} full={full!r}")
        abort(400, "Path traversal detected.")
    return full

def _validate_username(name):
    name = (name or "").strip()
    if not name:
        raise ValueError("Username is required.")
    if len(name) < 2:
        raise ValueError("Username too short (minimum 2 characters).")
    if len(name) > 40:
        raise ValueError("Username too long (maximum 40 characters).")
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$", name):
        raise ValueError("Username must start with a letter/digit and contain only letters, numbers, spaces, hyphens, or underscores.")
    if name.lower() in _BLOCKED_NAMES:
        raise ValueError("That username is reserved. Please choose a different name.")
    _bad = set("<>'\";&|`")
    if any(c in _bad for c in name):
        raise ValueError("Username contains invalid characters.")
    return name

def _get_client_ip():
    """The client IP used for rate limiting and account lockout.

    This must be the address of the peer the application actually accepted,
    never a value the client got to choose.

    Reading X-Forwarded-For here by hand is a bypass, not a convenience. A
    proxy doing its job *appends* the address it observed to the chain, so a
    request carrying its own `X-Forwarded-For: 1.2.3.4` arrives as
    `1.2.3.4, <real client>`. Taking the leftmost entry — the obvious thing to
    write — hands back whatever the attacker typed, and rotating that one
    header walks straight around the per-IP limiter and the per-account
    lockout, the only things between an attacker and a password list.

    So the address is never parsed from the header here. When a proxy is in
    front, ``ProxyFix`` (applied once at app creation) has already folded the
    chain into ``request.remote_addr`` using the known hop count. Without a
    proxy, ``remote_addr`` is the socket peer, and no header is consulted at
    all because nothing can be trusted to have appended to it.
    """
    return request.remote_addr or "unknown"


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
