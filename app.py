"""Compat shim — `gunicorn app:app` and `from app import app` keep working.

The implementation now lives in the :mod:`elfakgis` package (blueprints per
concern; heavy GIS imports lazy-load on first pipeline request, so boot and
light routes stay fast). See ARCHITECTURE.md.
"""
from elfakgis import app  # noqa: F401

if __name__ == "__main__":
    import os
    _debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    _port = int(os.environ.get("PORT", "5000"))
    app.run(debug=_debug, threaded=True, host="0.0.0.0", port=_port)
