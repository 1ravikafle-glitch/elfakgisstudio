"""Smoke tests for ElfakGISProStudio — run with: python -m pytest test.py -v"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app


def test_login_page_is_its_own_page():
    """Signed out: /login renders the form, / refuses to serve the app."""
    client = app.test_client()
    page = client.get("/login")
    assert page.status_code == 200, f"GET /login failed: {page.status_code}"
    assert b'id="login-inp"' in page.data
    assert b'id="login-pw"' in page.data
    # The app shell must not be smuggled onto the sign-in page.
    assert b'id="run-btn"' not in page.data

    home = client.get("/")
    assert home.status_code == 302, f"GET / should redirect when signed out: {home.status_code}"
    assert home.headers.get("Location", "").endswith("/login")


def test_home_serves_the_app_to_a_signed_in_visitor():
    """A hard refresh while signed in lands on the studio, never the form."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "tester"
    resp = client.get("/")
    assert resp.status_code == 200, f"GET / failed: {resp.status_code}"
    assert b'id="run-btn"' in resp.data
    assert b'id="login-inp"' not in resp.data, "login markup must not exist on the app page"


def test_signed_in_visitor_is_bounced_off_the_login_page():
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "tester"
    resp = client.get("/login")
    assert resp.status_code == 302, f"GET /login should redirect when signed in: {resp.status_code}"
    assert resp.headers.get("Location", "").endswith("/")


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


def test_login_page_matches_the_shared_suite_design():
    """The sign-in page must carry the Forestry PSC component vocabulary:
    the card, the 3-mode appearance selector, and the password reveal."""
    client = app.test_client()
    page = client.get("/login").get_data(as_text=True)

    assert 'class="login-card"' in page
    # app.css uses `.card` for the studio's mode panels (hidden until
    # .active). The sign-in page must not borrow that class, or every
    # panel would unhide at once.
    assert 'class="card' not in page, "login page must not use the studio's .card tab class"
    for mode in ("light", "dark", "system"):
        assert f'data-mode="{mode}"' in page, f"appearance selector is missing {mode}"
    assert 'id="pw-toggle"' in page, "password reveal toggle is missing"
    assert 'class="input"' in page and 'class="btn btn-primary"' in page

    # The form must be a real <form>, so Enter submits without a keydown hack.
    assert '<form id="login-form"' in page
    assert "onkeydown" not in page, "the reference submits on Enter natively"

    # Same cache-busted assets the templates actually reference, so the
    # browser can never load a stale theme.
    for asset in ("forestry.css?v=", "theme.js?v=", "login.js?v="):
        assert asset in page, f"{asset} is not versioned on the login page"


def test_login_rejects_empty_credentials():
    """The endpoint refuses blank input before it ever touches the backend."""
    client = app.test_client()

    resp = client.post("/login", json={"username": "", "password": "x"})
    assert resp.status_code == 400
    assert b"Username is required" in resp.data

    resp = client.post("/login", json={"username": "someone", "password": ""})
    assert resp.status_code == 400
    assert b"Password is required" in resp.data

    # No session may exist after a rejected attempt.
    assert client.get("/").status_code == 302
