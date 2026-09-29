"""Smoke tests for Elfak GIS Studio — run with: python -m pytest test.py -v"""
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


def test_history_delete_requires_a_session():
    """Nobody may touch run history without being signed in."""
    client = app.test_client()

    assert client.delete("/history/some_run_1").status_code == 401
    assert client.delete("/history").status_code == 401


def test_history_delete_is_scoped_to_the_signed_in_user(monkeypatch):
    """A user can only delete their own runs, never another account's."""
    from elfakgis.core import store

    runs = {"alice": ["alice_run_1"], "bob": ["bob_run_1"]}

    def fake_delete(user, rid):
        # Mirrors the real store: only that user's own rows are touched.
        owned = runs.get(user, [])
        if rid not in owned:
            return 0
        owned.remove(rid)
        return 1

    monkeypatch.setattr(store, "_runs_for", lambda u: [{"run_id": r} for r in runs.get(u, [])])
    monkeypatch.setattr(store, "_delete_run", fake_delete)
    monkeypatch.setattr("elfakgis.routes.auth._runs_for", store._runs_for, raising=False)
    monkeypatch.setattr("elfakgis.routes.auth._delete_run", store._delete_run, raising=False)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"

    # Bob's run is invisible to Alice: 404, and Bob keeps his history.
    assert client.delete("/history/bob_run_1").status_code == 404
    assert runs["bob"] == ["bob_run_1"], "another user's run must survive"

    # Alice's own run goes away, and the reply carries her new history.
    resp = client.delete("/history/alice_run_1")
    assert resp.status_code == 200
    assert b'"runs":[]' in resp.data.replace(b" ", b"")
    assert runs["alice"] == []

    # Unknown run ids are a 404, not a silent success.
    assert client.delete("/history/alice_run_1").status_code == 404


def test_history_delete_rejects_malformed_run_ids(monkeypatch):
    """Path traversal and shell-ish run ids are refused before any delete."""
    from elfakgis.core import store

    called = []
    monkeypatch.setattr(store, "_delete_run", lambda u, rid: called.append(rid) or 1)
    monkeypatch.setattr("elfakgis.routes.auth._delete_run", store._delete_run, raising=False)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"

    # Percent-encoded so the run id arrives intact instead of being
    # normalised away by URL routing. These reach the handler, which must
    # reject them with 400 before any delete runs.
    for bad in ("%2e%2e", "a%20b", "x%27%3B%20DROP", "run%3Fid", "run%23id", "run%00id"):
        resp = client.delete(f"/history/{bad}")
        assert resp.status_code == 400, f"{bad!r} -> {resp.status_code}, expected 400"
    assert called == [], "no delete may run for a malformed run id"

    # Payloads carrying a slash never resolve to this route at all, so the
    # store is never consulted for them either.
    for unroutable in ("%2e%2e%2f%2e%2e%2fetc", "run%2Fid"):
        assert client.delete(f"/history/{unroutable}").status_code == 404
    assert called == [], "no delete may run for an unroutable path"


def test_history_clear_wipes_only_the_signed_in_user(monkeypatch):
    """Clear all empties one account's history, not the whole table."""
    from elfakgis.core import store

    runs = {"alice": ["a1", "a2"], "bob": ["b1"]}
    monkeypatch.setattr(store, "_runs_for", lambda u: [{"run_id": r} for r in runs.get(u, [])])
    monkeypatch.setattr(store, "_clear_runs", lambda u: (runs.pop(u, None), 2)[1])
    monkeypatch.setattr("elfakgis.routes.auth._runs_for", store._runs_for, raising=False)
    monkeypatch.setattr("elfakgis.routes.auth._clear_runs", store._clear_runs, raising=False)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"

    resp = client.delete("/history")
    assert resp.status_code == 200
    assert b'"deleted":2' in resp.data.replace(b" ", b"")
    assert "alice" not in runs, "Alice's history should be gone"
    assert runs["bob"] == ["b1"], "another user's history must survive"


def test_history_delete_keeps_working_after_a_session_dies():
    """The delete routes must never answer a signed-out caller."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "tester"
    assert client.get("/").status_code == 200
    client.post("/logout")
    assert client.delete("/history/any_run").status_code == 401
    assert client.delete("/history").status_code == 401


def test_history_offers_delete_controls_in_the_ui():
    """The drawer must expose per-run delete and a clear-all action."""
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "tester"
    page = client.get("/").get_data(as_text=True)

    assert 'id="hist-clear"' in page, "the history drawer has no clear-all button"
    assert "clearHistory()" in page

    app_js = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               "static", "js", "app.js"), encoding="utf-8").read()
    assert "deleteRun(" in app_js, "no per-run delete handler"
    assert "method: 'DELETE'" in app_js, "delete calls must use the DELETE method"
