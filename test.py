"""Smoke tests for ElfakGISProStudio — run with: python -m pytest test.py -v"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app


def test_home_returns_index():
    client = app.test_client()
    resp = client.get("/")
    assert resp.status_code == 200, f"GET / failed: {resp.status_code}"
    assert b"Elfak" in resp.data or b"html" in resp.data.lower()


def test_health_routes():
    client = app.test_client()
    for path, ok_codes in [
        ("/robots.txt", {200}),
        ("/sitemap.xml", {200}),
        ("/about", {200}),
        ("/me", {401, 302, 200}),
    ]:
        resp = client.get(path)
        assert resp.status_code in ok_codes, f"{path} -> {resp.status_code}"


def test_security_headers():
    client = app.test_client()
    resp = client.get("/")
    assert resp.headers.get("X-Content-Type-Options") == "nosniff"
    assert resp.headers.get("X-Frame-Options") == "DENY"
