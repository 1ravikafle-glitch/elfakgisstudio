"""ElfakGISProStudio — rate limits, validation, path safety (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import session, request, jsonify, abort

log = logging.getLogger("elfakgis")
from elfakgis.core.config import OUTPUT
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

threading.Thread(target=_clean_rl, daemon=True).start()

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
    xff = request.headers.get("X-Forwarded-For", "")
    if xff:
        ip = xff.split(",")[0].strip()
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", ip) or ":" in ip:
            return ip
    return request.remote_addr or "unknown"


def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
