"""ElfakGISProStudio — progress hub, user store, run meta (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import session, request, jsonify, abort

log = logging.getLogger("elfakgis")
from elfakgis.core.config import USERS_FILE
# ----------------------------------------------------------------------
# User Management, Progress, Rate Limiting
# ----------------------------------------------------------------------

_PROG: dict = {}
_PROG_LOCK = threading.Lock()

def _prog(rid, msg, pct=None):
    o = {
        "msg": str(msg)[:500],
        "pct": max(0, min(100, int(pct))) if pct is not None else None,
        "ts": time.time()
    }
    with _PROG_LOCK:
        if rid not in _PROG: _PROG[rid] = []
        _PROG[rid].append(json.dumps(o))
        _PROG[rid] = _PROG[rid][-500:]
    time.sleep(0.01)

def _cleanup_old_prog():
    while True:
        time.sleep(3600)
        with _PROG_LOCK:
            keys = list(_PROG.keys())
            if len(keys) > 10000:
                for k in keys[:len(keys)//2]:
                    _PROG.pop(k, None)


_USERS_LOCK = threading.RLock()

def _lu():
    with _USERS_LOCK:
        try:
            if not os.path.exists(USERS_FILE): return {}
            with open(USERS_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            log.error(f"_lu error: {e}"); return {}

def _su(users_dict):
    with _USERS_LOCK:
        try:
            tmp = USERS_FILE + ".tmp." + uuid.uuid4().hex[:8]
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(users_dict, f, indent=2, ensure_ascii=False)
            os.replace(tmp, USERS_FILE)
        except Exception as e:
            log.error(f"_su error: {e}")

def _register_user(name):
    name = name.strip()
    with _USERS_LOCK:
        u = _lu()
        if name in u:
            raise ValueError(f'Username "{name}" is already taken. Choose a different name.')
        token = secrets.token_hex(32)
        u[name] = {
            "username":   name,
            "created_at": datetime.now().isoformat(),
            "token_hash": hashlib.sha256(token.encode()).hexdigest(),
            "runs":       [],
            "active_sessions": 0,
        }
        _su(u)
        log.info(f"New user registered: {name!r}")
        return u[name], token

def _login_existing(name):
    name = name.strip()
    with _USERS_LOCK:
        u = _lu()
        if name not in u:
            raise KeyError(f'User "{name}" not found.')
        u[name]["active_sessions"] = u[name].get("active_sessions", 0) + 1
        u[name]["last_login"] = datetime.now().isoformat()
        _su(u)
        return u[name]

def _logout_user(name):
    if not name: return
    with _USERS_LOCK:
        u = _lu()
        if name in u:
            u[name]["active_sessions"] = max(0, u[name].get("active_sessions", 1) - 1)
            _su(u)

def _runs_for(uname):
    """Run history for a user: shared Postgres first, users.json as fallback."""
    if not uname: return []
    try:
        from elfakgis.core import db as _db
        rows = _db.get_runs(uname, limit=100)
        if rows is not None:
            return rows
    except Exception as e:
        log.warning("Postgres run history unavailable (%s); using users.json", e)
    with _USERS_LOCK:
        u = _lu()
        return list(u.get(uname, {}).get("runs", []))


def _append_run(uname, rid, mod, desc=""):
    if not uname: return
    # Primary store: shared PostgreSQL, so history survives redeploys.
    try:
        from elfakgis.core import db as _db
        if _db.add_run(uname, rid, mod, desc):
            return
    except Exception as e:
        log.warning("Postgres run append failed (%s); using users.json", e)
    with _USERS_LOCK:
        u = _lu()
        record = u.setdefault(uname, {
            "username": uname,
            "created_at": datetime.now().isoformat(),
            "runs": [],
        })
        record.setdefault("runs", []).append({
                "run_id":    rid,
                "module":    mod,
                "description": desc[:200],
                "timestamp": datetime.now().isoformat(),
            })
        record["runs"] = record["runs"][-100:]
        _su(u)

def _require_login():
    return session.get("username")

def _login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not _require_login():
            return jsonify({"error": "Authentication required. Please log in."}), 401
        return fn(*args, **kwargs)
    return wrapper
# META HELPER & MAP EDITOR ROUTE
# ----------------------------------------------------------------------

def _save_run_meta(out_dir, forest_name, area_ha=None, **extra):
    """Save run metadata alongside output for the map editor."""
    meta = {
        "forest_name": str(forest_name) if forest_name else "Forest Boundary",
        "area_ha": float(area_ha) if area_ha is not None else None,
    }
    meta.update(extra)
    try:
        with open(os.path.join(out_dir, "meta.json"), "w", encoding="utf-8") as f:
            json.dump(meta, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.warning(f"_save_run_meta failed: {e}")



def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
