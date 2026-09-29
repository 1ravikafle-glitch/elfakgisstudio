"""Elfak GIS Studio — pipeline concurrency guard (split from app.py — bodies verbatim; see ARCHITECTURE.md)."""
import os, re, io, gc, json, time, math, uuid, zipfile, shutil, traceback, tempfile
import threading, hashlib, html, secrets, logging, urllib.parse
from collections import defaultdict, OrderedDict
from functools import wraps
from datetime import datetime
from flask import session, request, jsonify

log = logging.getLogger("elfakgis")
from elfakgis.core.security import _get_client_ip
# ─── PIPELINE SEMAPHORE ──────────────────────────────────────────────────
_PIPELINE_SEM = threading.Semaphore(int(os.environ.get("MAX_PIPELINES", "4")))
_ACTIVE_PIPELINES = 0
_AP_LOCK = threading.Lock()

def _with_pipeline_sem(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        global _ACTIVE_PIPELINES
        acquired = _PIPELINE_SEM.acquire(timeout=5)
        if not acquired:
            log.warning(f"Pipeline semaphore exhausted for {_get_client_ip()}")
            return jsonify({
                "error": "Server is at capacity. Please wait a moment and retry.",
                "retry_after": 30
            }), 503
        with _AP_LOCK: _ACTIVE_PIPELINES += 1
        try:
            log.info(f"Pipeline started by {session.get('username','?')} "
                     f"[active={_ACTIVE_PIPELINES}] [{request.path}]")
            return fn(*args, **kwargs)
        finally:
            _PIPELINE_SEM.release()
            with _AP_LOCK: _ACTIVE_PIPELINES = max(0, _ACTIVE_PIPELINES - 1)
    return wrapper

def __getattr__(name):
    """PEP 562: heavy GIS names resolve lazily on first use (fast boot)."""
    import elfakgis.lazy as _lz
    return _lz.resolve(name)
