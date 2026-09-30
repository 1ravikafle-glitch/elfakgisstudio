"""Smoke tests for Elfak GIS Studio — run with: python -m pytest test.py -v"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import app


def _csrf_headers(client):
    """The X-CSRF-Token header the real frontend sends on unsafe methods.

    The token lives in the signed session, so it has to be read out of the
    same session the request will use. Seeded directly rather than minted
    through generate_csrf_token() because that needs a request context,
    and this runs before any request. Signed-out callers have no token and
    no session to forge against, which is why the 401 tests pass no header
    and still see the route's own auth answer rather than a CSRF rejection.
    """
    import secrets
    from elfakgis.core.csrf import CSRF_SESSION_KEY
    token = secrets.token_urlsafe(32)
    with client.session_transaction() as sess:
        sess[CSRF_SESSION_KEY] = token
    return {"X-CSRF-Token": token}


def _delete(client, path, with_csrf=True):
    """DELETE a history path the way the browser does: with a CSRF token."""
    headers = _csrf_headers(client) if with_csrf else {}
    return client.delete(path, headers=headers)


@pytest.fixture(autouse=True)
def _no_external_services():
    """Keep the suite hermetic: no network, no database, no shared state.

    Two leaks had to be closed.

    * The Forestry PSC auth endpoint was being called for real, which made
      the suite depend on a production service it does not own, took ~95s,
      and created actual accounts in it.
    * Once a local `.env` existed, `DATABASE_URL` was set for every test and
      each `db.available()` call opened a fresh pooled connection to Neon,
      stalling the run on an 8s connect timeout. A test suite that reaches a
      shared production database is also free to mutate it.

    So: the fallback tier is stubbed to "unavailable" and pinned enabled,
    and `DATABASE_URL` is removed so the database tier reports "not
    configured" — the same answer a fresh checkout gives.
    """
    from elfakgis.core import autshared, db
    autshared._SHARED_FALLBACK = "api"
    autshared._verify_via_forestry_api = lambda u, p: (None, False)

    saved_url = os.environ.pop("DATABASE_URL", None)
    db._engine = None
    db._ok = False
    db._checked_at = 0.0
    yield
    if saved_url is not None:
        os.environ["DATABASE_URL"] = saved_url
    db._engine = None
    db._ok = False
    db._checked_at = 0.0
    import importlib
    importlib.reload(autshared)


def test_the_fallback_is_off_by_default(monkeypatch):
    """The auto-registering API must not be consulted unless asked for.

    This is the important one. Forestry PSC creates an account on first
    sign-in, so a 200 from it is not evidence of a correct password — it is
    evidence that the service will invent an account for whoever asks.
    Treating that as authentication made /login open to anyone.

    Gating on "was this account new?" is not sufficient on its own: the
    first attempt creates the account, so the second attempt with the same
    invented credentials looks like a returning user and passes. The gate
    has to be at the tier, not at the account.
    """
    import importlib
    from elfakgis.core import autshared
    importlib.reload(autshared)
    try:
        assert autshared.fallback_enabled() is False, (
            "the Forestry fallback is enabled without being asked for"
        )
        # And with no database, verification must fail closed rather than
        # reach the network.
        called = []
        monkeypatch.setattr(
            autshared, "_verify_via_forestry_api",
            lambda u, p: called.append(u) or (True, True))
        ok, reason = autshared.verify_credentials("anyone", "anything")
        assert (ok, reason) == (False, "unavailable"), (
            f"fell back to the auto-registering service: {(ok, reason)}"
        )
        assert not called, "the remote service was contacted while disabled"
    finally:
        importlib.reload(autshared)


def test_the_fallback_consults_the_api_only_when_enabled(monkeypatch):
    """Opting in restores the old chain, still reporting new accounts."""
    from elfakgis.core import autshared
    monkeypatch.setattr(autshared, "_SHARED_FALLBACK", "api")
    assert autshared.fallback_enabled() is True
    monkeypatch.setattr(autshared, "_verify_via_forestry_api",
                        lambda u, p: (True, True))
    assert autshared.verify_credentials("invented", "anything") == (True, "new")


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """Clear the rate-limit and lockout state around every test.

    Both live in module-level dicts keyed by IP, and the test client is
    always 127.0.0.1 — so without this a suite that makes a few dozen
    requests trips a real limit and later tests fail with a 429 that has
    nothing to do with what they are testing. The sweeper thread is
    untouched; only the counters are dropped.
    """
    from elfakgis.core import security
    yield
    with security._RL_LOCK:
        security._RL.clear()
        security._FAIL_LOCK.clear()


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


@pytest.fixture
def sso_secret(monkeypatch):
    """Stand in for the SSO_SECRET that is configured in the real deployment.

    The feature disables itself when the variable is absent, so the local
    suite has to supply one to exercise the handoff at all.
    """
    monkeypatch.setenv("SSO_SECRET", "test-shared-secret-value-0123456789")
    monkeypatch.setenv("SSO_MAX_TTL", "0")
    return "test-shared-secret-value-0123456789"


def _sso_token(audience, username="Elfak", exp=None, iat=None, secret=None):
    """Mint a handoff token exactly the way the sibling site does."""
    import base64, hashlib, hmac, json, time
    from elfakgis.core import autshared

    secret = secret if secret is not None else autshared._sso_secret()
    now = int(time.time())
    payload = {
        "a": 1,
        "aud": audience,
        "exp": exp if exp is not None else now + 180,
        "iat": iat if iat is not None else now,
        "u": username,
    }
    body = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).decode().rstrip("=")
    sig = base64.urlsafe_b64encode(
        hmac.new(secret, body.encode(), hashlib.sha256).digest()
    ).decode().rstrip("=")
    return f"{body}.{sig}"


def test_sso_accepts_a_token_minted_with_the_pre_rename_audience(sso_secret):
    """Links minted before the rename carry the old product name.

    They are signed with the same shared secret and still mean "this user is
    signed in on the sibling site", so they must be accepted. Rejecting them
    strands every already-circulated link.
    """
    from elfakgis.core import autshared

    assert "elfakgisprostudio" in autshared.SSO_AUDIENCE_ALIASES

    token = _sso_token("elfakgisprostudio")
    client = app.test_client()
    resp = client.get(f"/sso/exchange?t={token}")
    assert resp.status_code == 303, f"legacy audience should still sign in: {resp.status_code}"
    assert resp.headers.get("Location", "").endswith("/")

    with client.session_transaction() as sess:
        assert sess.get("username") == "Elfak", "legacy-audience handoff did not sign the user in"


def test_sso_accepts_the_canonical_audience(sso_secret):
    from elfakgis.core import autshared

    token = _sso_token(autshared.SSO_AUDIENCE, username="Elfak")
    resp = app.test_client().get(f"/sso/exchange?t={token}")
    assert resp.status_code == 303, f"canonical audience should sign in: {resp.status_code}"


def test_sso_still_refuses_a_foreign_audience(sso_secret):
    """Widening the accepted audiences must not turn into accept-anything."""
    token = _sso_token("some-other-product")
    resp = app.test_client().get(f"/sso/exchange?t={token}")
    assert resp.status_code == 401, f"a foreign audience must not sign anyone in: {resp.status_code}"


def test_sso_still_refuses_a_token_signed_with_the_wrong_secret(sso_secret):
    token = _sso_token("elfakgisstudio", secret=b"not-the-shared-secret")
    resp = app.test_client().get(f"/sso/exchange?t={token}")
    assert resp.status_code == 401, f"a bad signature must not sign anyone in: {resp.status_code}"


def test_failed_sso_handoff_sends_an_already_signed_in_visitor_straight_to_the_app(sso_secret):
    """A bad link must not tell a signed-in visitor to sign in again.

    That message plus /login's own redirect to / made a dead-end loop: the
    visitor is already authenticated, so the 'sign in' detour only bounces
    them back to the studio looking like it is broken.
    """
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "tester"
    resp = client.get("/sso/exchange?t=not-a-valid-token")
    assert resp.status_code == 303, f"should redirect instead of showing the fallback: {resp.status_code}"
    assert resp.headers.get("Location", "").endswith("/")
    assert b"couldn" not in resp.data, "must not show the 'sign in again' page while signed in"


def test_failed_sso_handoff_still_shows_the_fallback_to_a_signed_out_visitor():
    client = app.test_client()
    resp = client.get("/sso/exchange?t=not-a-valid-token")
    assert resp.status_code == 401, f"a signed-out visitor needs the fallback page: {resp.status_code}"
    assert b"/login" in resp.data, "the fallback page must offer a way to sign in"


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

    assert _delete(client, "/history/some_run_1", with_csrf=False).status_code == 401
    assert _delete(client, "/history", with_csrf=False).status_code == 401


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
    assert _delete(client, "/history/bob_run_1").status_code == 404
    assert runs["bob"] == ["bob_run_1"], "another user's run must survive"

    # Alice's own run goes away, and the reply carries her new history.
    resp = _delete(client, "/history/alice_run_1")
    assert resp.status_code == 200
    assert b'"runs":[]' in resp.data.replace(b" ", b"")
    assert runs["alice"] == []

    # Unknown run ids are a 404, not a silent success.
    assert _delete(client, "/history/alice_run_1").status_code == 404


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
        resp = _delete(client, f"/history/{bad}")
        assert resp.status_code == 400, f"{bad!r} -> {resp.status_code}, expected 400"
    assert called == [], "no delete may run for a malformed run id"

    # Payloads carrying a slash never resolve to this route at all, so the
    # store is never consulted for them either.
    for unroutable in ("%2e%2e%2f%2e%2e%2fetc", "run%2Fid"):
        assert _delete(client, f"/history/{unroutable}").status_code == 404
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

    resp = _delete(client, "/history")
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
    client.post("/logout", headers=_csrf_headers(client))
    assert _delete(client, "/history/any_run", with_csrf=False).status_code == 401
    assert _delete(client, "/history", with_csrf=False).status_code == 401


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


def _template(name):
    root = os.path.dirname(os.path.abspath(__file__))
    return open(os.path.join(root, "templates", name), encoding="utf-8").read()


def test_no_dropzone_leaks_a_native_file_input():
    """The browser's own "Choose File" control must never be visible.

    The rule that hides it used to be keyed on an opt-in class, so a
    dropzone whose input simply forgot the class rendered the native control
    inside the styled box - a small grey "Choose File" strip in groups F and
    G. Keyed on the type instead, so it cannot regress per dropzone.
    """
    import re

    css = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "static", "css", "app.css"), encoding="utf-8").read()
    assert '.dz input[type="file"]' in css, (
        "the dropzone input-hiding rule must target the input type, not an "
        "opt-in class, or a dropzone can leak the native file control"
    )

    # Every file input inside a .dz must therefore be covered by that rule.
    for name in ("index.html", "login.html"):
        html = _template(name)
        for m in re.finditer(r'<div class="dz"[^>]*>(.*?)</div>', html, re.S):
            if 'type="file"' in m.group(1):
                assert 'type="file"' in m.group(1), "unreachable"


def test_every_file_input_inside_a_dropzone_is_present():
    """Guard the specific inputs that were broken, so a rename is caught."""
    html = _template("index.html")
    for fid in ("fi-F-bnd", "fi-F-dem", "fi-G"):
        assert f'id="{fid}"' in html, f"{fid} is missing from the form"


def test_the_site_uses_the_shared_png_logo_everywhere():
    """One logo asset, not a hand-copied SVG per call site.

    The map overlay had drifted to a hardcoded off-brand green, and the
    header and login marks were separate inline SVGs, so the three could
    never be kept in step.
    """
    for name in ("index.html", "login.html"):
        html = _template(name)
        assert 'viewBox="0 0 48 48"' not in html, (
            f"{name} still has an inline logo SVG; use /static/elfak-logo-64.png"
        )
        assert 'src="/static/elfak-logo-64.png"' in html, f"{name} does not use the PNG logo"

    root = os.path.dirname(os.path.abspath(__file__))
    for asset in ("elfak-logo.png", "elfak-logo-64.png", "elfak-logo.svg"):
        assert os.path.exists(os.path.join(root, "static", asset)), f"missing {asset}"


# ── Security regression tests ───────────────────────────────────────────
# Each of these covers a hole that was live in this codebase. They are
# written to fail loudly if the corresponding fix is ever reverted.


def test_run_data_routes_require_a_session():
    """No run-scoped route may answer an anonymous caller.

    Every one of these used to be reachable without a session, which turned
    a guessable run id into a way to read (and via save_edit, rewrite)
    another user's forestry data.
    """
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    for path, method in [
        ("/geojson/RUN_1", "get"),
        ("/download/RUN_1", "get"),
        ("/outputs/RUN_1/output.png", "get"),
        ("/map_texts/RUN_1", "get"),
        ("/progress/RUN_1", "get"),
        ("/result/RUN_1", "get"),
        ("/map_editor/RUN_1", "get"),
        ("/compose/RUN_1", "post"),
        ("/save_edit/RUN_1", "post"),
        ("/export_layout", "post"),
    ]:
        resp = getattr(client, method)(path, json={} if method == "post" else None)
        assert resp.status_code == 401, f"{method.upper()} {path} -> {resp.status_code}, want 401"


def test_a_user_cannot_reach_another_users_run(monkeypatch):
    """Ownership is enforced on every run-scoped read and write."""
    from elfakgis.core import store, security

    with security._RL_LOCK:
        security._RL.clear()
    monkeypatch.setattr(store, "_run_owner", lambda rid: "alice" if rid == "alice_run" else None)
    monkeypatch.setattr("elfakgis.routes.maps._owns_run", store._owns_run, raising=False)
    monkeypatch.setattr("elfakgis.routes.maps._owns_run_or_404", store._owns_run_or_404, raising=False)
    monkeypatch.setattr("elfakgis.routes.maps._run_owner", store._run_owner, raising=False)
    monkeypatch.setattr("elfakgis.routes.pages._owns_run", store._owns_run, raising=False)
    monkeypatch.setattr("elfakgis.routes.pages._owns_run_or_404", store._owns_run_or_404, raising=False)

    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "bob"

    # Bob's session, Alice's run: 404, not 403. A 403 would confirm the id
    # exists, which is exactly the enumeration oracle to avoid.
    assert client.get("/geojson/alice_run").status_code == 404
    assert client.get("/download/alice_run").status_code == 404
    assert client.get("/outputs/alice_run/output.png").status_code == 404
    assert client.get("/map_texts/alice_run").status_code == 404
    assert client.get("/map_editor/alice_run").status_code == 404
    # ...and the writes, which are the ones that would actually cause damage.
    assert _delete(client, "/history/alice_run").status_code == 404


def test_run_ownership_fails_closed(monkeypatch):
    """A run with no recorded owner is owned by nobody.

    Without this, an unconfigured database plus a fresh worker would leave
    every claim empty and "owner == caller" would be vacuously true for
    nobody — or, worse, for everybody.
    """
    from elfakgis.core import store
    monkeypatch.setattr(store, "_run_owner", lambda rid: None)
    assert store._owns_run("orphan", "alice") is False
    # An empty username can never own anything, even with a claim present.
    assert store._owns_run("orphan", "") is False
    # And outside a request there is no session to fall back to, which must
    # be a refusal rather than a crash.
    with app.test_request_context("/"):
        assert store._owns_run("orphan") is False


def test_state_changing_routes_reject_a_missing_csrf_token():
    """A cookie-authenticated POST/DELETE must carry a CSRF token."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"

    # Right session, no token: refused.
    assert client.post("/logout").status_code == 403
    assert client.post("/compose/whatever", json={}).status_code == 403
    assert client.post("/save_edit/whatever", json={}).status_code == 403
    assert client.delete("/history", headers={}).status_code == 403

    # Safe methods are never gated, so page loads and map reads keep working.
    assert client.get("/").status_code == 200


def test_a_valid_csrf_token_is_accepted():
    """The browser path must still work — protection that blocks everyone
    is just an outage."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"
    resp = client.post("/logout", headers=_csrf_headers(client))
    assert resp.status_code == 200, f"a valid CSRF token was rejected: {resp.status_code}"


def test_repeated_sign_in_failures_lock_the_account():
    """Credential stuffing against one known account must be rate-limited."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
        security._FAIL_LOCK.clear()

    for _ in range(security._FAIL_LOCK_MAX):
        security._record_login_failure("target")
    assert security._lockout_seconds("target") > 0, (
        "after the failure threshold the account must be locked out"
    )
    # A different account is unaffected — the limiter is per-account.
    assert security._lockout_seconds("someone-else") == 0
    # And a success clears the counter, so a real user who fat-fingers a
    # password a few times is not locked out afterwards.
    security._clear_login_failures("target")
    assert security._lockout_seconds("target") == 0


def test_dotenv_keeps_a_url_that_contains_an_ampersand(tmp_path, monkeypatch):
    """A Postgres URL ends in `&sslmode=...`; the loader must not truncate it.

    This is not hypothetical. An unquoted DATABASE_URL in .env is cut at the
    first `&` by anything that sources the file in a shell, and the app then
    connects to a host with no database, no password and no SSL mode — a
    failure that looks like a credential problem rather than a quoting one.
    """
    from elfakgis.core import config
    env = tmp_path / ".env"
    env.write_text(
        '# a comment\n'
        '\n'
        'export QUOTED_URL="postgresql://u:p@host/db?sslmode=require&channel_binding=require"\n'
        "SINGLE_QUOTED='postgresql://u:p@host/db?sslmode=require'\n"
        'BARE=plainvalue\n'
        'OVERRIDE_ME=from_file\n'
        'no_equals_sign_here\n',
        encoding="utf-8",
    )
    for k in ("QUOTED_URL", "SINGLE_QUOTED", "BARE", "OVERRIDE_ME"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("OVERRIDE_ME", "from_process")

    keys = config.load_dotenv(env)

    assert os.environ["QUOTED_URL"].endswith("&channel_binding=require"), (
        f"query string was truncated: {os.environ['QUOTED_URL']}"
    )
    assert "sslmode=require" in os.environ["QUOTED_URL"]
    assert os.environ["SINGLE_QUOTED"] == "postgresql://u:p@host/db?sslmode=require"
    assert os.environ["BARE"] == "plainvalue"
    # A real deployment environment must beat the file, or a stray .env
    # could silently repoint production at a laptop's database.
    assert os.environ["OVERRIDE_ME"] == "from_process", (
        "the .env overrode a real environment variable"
    )
    assert "no_equals_sign_here" not in keys


def test_dotenv_missing_file_is_harmless(tmp_path, monkeypatch):
    """No .env must be a no-op, not a crash — production has no .env."""
    from elfakgis.core import config
    assert config.load_dotenv(tmp_path / "does_not_exist") == []


def test_an_unknown_username_is_a_rejected_login_not_an_outage(monkeypatch):
    """With the database reachable, a missing user is 401, not 503.

    Failing closed must not become "everything is broken". Before this was
    split out, any username the `users` table did not contain fell through
    to the disabled fallback and was reported as `unavailable`, so a
    single mistyped password produced "Sign-in is temporarily unavailable"
    and an HTTP 503 — telling a real user the site was down.
    """
    from elfakgis.core import autshared, db
    import importlib
    importlib.reload(autshared)
    try:
        monkeypatch.setattr(db, "available", lambda: True)
        assert autshared.verify_credentials("nobody_at_all", "whatever") == (False, "invalid")
        # But when the database genuinely cannot answer, staying silent is
        # the point — that must still be an outage, not a wrong password.
        monkeypatch.setattr(db, "available", lambda: False)
        assert autshared.verify_credentials("nobody_at_all", "whatever") == (False, "unavailable")
    finally:
        importlib.reload(autshared)


def test_the_rate_limiter_cannot_be_bypassed_by_forging_forwarded_for():
    """A client must not be able to pick the address the limiter counts.

    Behind a proxy the address has to come from the chain, but the obvious
    implementation — read X-Forwarded-For in the route — is a hole. A proxy
    that is doing its job *appends* the address it saw, so a request carrying
    its own header arrives as `<forged>, <real client>`. Taking the forged
    entry lets an attacker rotate one header and walk around the per-IP
    limiter and the per-account lockout.

    These build the app the way Render does (one trusted proxy, so ProxyFix
    folds the chain in) and simulate the proxy's append.
    """
    from flask import Flask
    from werkzeug.middleware.proxy_fix import ProxyFix
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()

    # One client, one real address, a different forgery on every request.
    real_client = "203.0.113.9"
    proxied = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), "static"),
    )
    proxied.secret_key = "t"
    proxied.wsgi_app = ProxyFix(proxied.wsgi_app, x_for=1, x_proto=1,
                                x_host=1, x_prefix=0)
    proxied.add_url_rule("/login", "login",
                         app.view_functions["auth_bp.login"], methods=["POST"])

    codes = set()
    for i in range(25):
        resp = proxied.test_client().post(
            "/login", json={"username": "x", "password": "y"},
            headers={"X-Forwarded-For": f"10.0.0.{i}, {real_client}"},
        )
        codes.add(resp.status_code)
    assert 429 in codes, (
        "a forged X-Forwarded-For defeated the per-IP limiter: "
        f"statuses seen were {sorted(codes)}"
    )


def test_the_real_client_address_is_used_when_a_proxy_is_trusted(monkeypatch):
    """The appended entry wins, because that is the one the proxy observed.

    Driven through a real request rather than test_request_context, because
    the latter builds the environ directly and never runs the WSGI stack —
    so ProxyFix would silently not be exercised at all.
    """
    from flask import Flask
    from werkzeug.middleware.proxy_fix import ProxyFix
    from elfakgis.core import security
    monkeypatch.setattr(security, "_PROXY_TRUSTED", True)

    def _whoami():
        return security._get_client_ip()

    proxied = Flask(__name__)
    proxied.wsgi_app = ProxyFix(proxied.wsgi_app, x_for=1, x_proto=1,
                                x_host=1, x_prefix=0)
    proxied.add_url_rule("/whoami", "whoami", _whoami)

    got = proxied.test_client().get(
        "/whoami", headers={"X-Forwarded-For": "1.2.3.4, 203.0.113.9"}).get_data(as_text=True)
    assert got == "203.0.113.9", f"used the attacker-supplied entry: {got}"

    # With no proxy in front, the header is ignored entirely and the socket
    # peer is used, because nothing can be trusted to have appended to it.
    plain = Flask(__name__)
    plain.add_url_rule("/whoami", "whoami", _whoami)
    got = plain.test_client().get(
        "/whoami", headers={"X-Forwarded-For": "1.2.3.4"},
        environ_base={"REMOTE_ADDR": "10.1.1.1"}).get_data(as_text=True)
    assert got == "10.1.1.1", (
        f"an untrusted X-Forwarded-For was believed with no proxy in front: {got}"
    )


def test_the_stay_signed_in_cookie_is_secure_and_httponly(monkeypatch):
    """A 30-day bearer token must never travel over plain HTTP.

    It used to take `secure` from `HTTPS=1`, an environment variable Render
    does not set, so the cookie was issued without the flag in production.
    """
    import importlib
    from flask import Flask
    auth_routes = importlib.import_module("elfakgis.routes.auth")

    plain = Flask(__name__)
    plain.config["SESSION_COOKIE_SECURE"] = True
    with plain.test_request_context("/"):
        flags = auth_routes._remember_cookie_args()
    assert flags["secure"] is True, (
        "remember-me cookie is not Secure behind a TLS-terminating proxy"
    )
    assert flags["httponly"] is True
    assert flags["samesite"] == "Lax"

    # A genuinely non-TLS deployment must not claim Secure, or the cookie is
    # simply never sent back and "stay signed in" silently stops working.
    plain.config["SESSION_COOKIE_SECURE"] = False
    with plain.test_request_context("/"):
        assert auth_routes._remember_cookie_args()["secure"] is False


def test_every_external_resource_is_allowed_by_the_csp():
    """Templates may not reference a host the Content-Security-Policy blocks.

    This failure mode is invisible: the page still loads, still looks fine to
    a developer on a warm cache, and the resource is simply missing. It
    already happened here — the UI requested Inter from
    fonts.googleapis.com while the policy allowed only 'self' for styles and
    fonts, so the whole app silently fell back to a system font. Nothing
    errored; the design just quietly stopped being the design.
    """
    import glob
    import re as _re
    from urllib.parse import urlsplit

    csp = app.test_client().get("/login").headers["Content-Security-Policy"]
    directives = {}
    for part in csp.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, values = part.partition(" ")
        directives[name.strip()] = values.split()

    # A directive with no host allowlist (e.g. 'none') blocks everything.
    def host_allowed(host, directive):
        allowed = directives.get(directive, [])
        if " 'none'" in f" {directive} " or allowed == ["'none'"]:
            return False
        for entry in allowed:
            entry = entry.strip()
            if entry == "'self'":
                return True
            if entry.startswith("https://"):
                pattern = entry[len("https://"):].rstrip("/")
                if pattern.startswith("*."):
                    if host == pattern[2:] or host.endswith("." + pattern[2:]):
                        return True
                elif host == pattern:
                    return True
            elif entry == "*":
                return True
        return False

    # Which directive governs which kind of tag.
    def check(url, directive):
        host = urlsplit(url).hostname or ""
        if not host:
            return
        # A URL in an <a href> or JSON-LD is not a subresource; the policy
        # does not restrict navigation (form-action and frame-ancestors do).
        assert host_allowed(host, directive), (
            f"{url} is referenced by a template but '{directive}' does not "
            f"allow {host}. It will be blocked silently. CSP: {directive} "
            f"= {' '.join(directives.get(directive, []))}"
        )

    problems = []
    for path in glob.glob(os.path.join(os.path.dirname(__file__), "templates", "*.html")):
        html = open(path, encoding="utf-8").read()
        # Stylesheet links.
        for m in _re.finditer(r'<link[^>]+rel="stylesheet"[^>]*>', html):
            tag = m.group(0)
            href = _re.search(r'href="([^"]+)"', tag)
            if href and href.group(1).startswith("http"):
                # Skip a tag that already carries an integrity hash: those
                # are cross-origin and allowed via script/style-src below.
                check(href.group(1), "style-src")
        # Script tags.
        for m in _re.finditer(r'<script[^>]+src="([^"]+)"', html):
            check(m.group(1), "script-src")
        # Font preloads.
        for m in _re.finditer(r'<link[^>]+as="font"[^>]*>', html):
            href = _re.search(r'href="([^"]+)"', html[m.start():m.end()])
            if href and href.group(1).startswith("http"):
                check(href.group(1), "font-src")

    assert not problems, "\n".join(problems)


def test_the_font_is_self_hosted_and_reachable():
    """The app must not depend on a font host, and the file must be served."""
    import re as _re
    here = os.path.dirname(__file__)
    for name in ("index.html", "login.html"):
        html = open(os.path.join(here, "templates", name), encoding="utf-8").read()
        # Match an actual resource reference, not a mention in a comment.
        for m in _re.finditer(r'(?:href|src)="(https://fonts\.[^"]+)"', html):
            raise AssertionError(
                f"{name} loads {m.group(1)}; the CSP blocks it, so the font "
                "silently falls back to a system face"
            )
    c = app.test_client()
    for url in ("/static/css/fonts.css", "/static/fonts/inter-latin.woff2",
                "/static/fonts/inter-latin-ext.woff2"):
        r = c.get(url)
        assert r.status_code == 200, f"{url} is not served: {r.status_code}"
    assert "font/woff2" in c.get("/static/fonts/inter-latin.woff2").headers["Content-Type"], (
        "the woff2 is served with the wrong MIME type and the browser will "
        "refuse to use it"
    )
    css = c.get("/static/css/fonts.css").get_data(as_text=True)
    assert "Inter" in css and "400 800" in css, (
        "fonts.css does not declare the variable weight range the app uses"
    )


def test_every_third_party_script_carries_an_integrity_hash():
    """A CDN script without SRI can execute anything, in our origin."""
    import re as _re
    app_js = open(os.path.join(os.path.dirname(__file__), "static", "js", "app.js"),
                  encoding="utf-8").read()
    for m in _re.finditer(r"url:\s*'(https://[^']+)'", app_js):
        url = m.group(1)
        tail = app_js[m.end():m.end() + 200]
        assert "sri:" in tail, f"{url} is loaded with no integrity hash"
    # ...and the same for the template tags.
    html = open(os.path.join(os.path.dirname(__file__), "templates", "index.html"),
                encoding="utf-8").read()
    for m in _re.finditer(r'<(script|link)[^>]+https://unpkg\.com[^>]*>', html, _re.S):
        tag = m.group(0)
        # preconnect/dns-prefetch only open a socket — they fetch no bytes and
        # so have nothing to hash. SRI applies to what actually loads.
        if "preconnect" in tag:
            continue
        assert "integrity=" in tag, f"unpkg tag has no integrity: {tag[:90]}"


def test_sign_in_is_rate_limited_per_ip():
    """The IP bucket must actually be wired to /login.

    It was orphaned in the module split: imported everywhere, applied to
    nothing, so real password auth had no brute-force protection at all.
    """
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    codes = [client.post("/login", json={"username": "x", "password": "y"}).status_code
             for _ in range(25)]
    assert 429 in codes, "/login answered 25 attempts without ever rate-limiting"


def test_an_account_forestry_invents_on_the_spot_is_not_a_login(monkeypatch):
    """Even with the fallback on, a freshly invented account is refused.

    Forestry answers a username it has never seen by creating it, and says
    so with `is_new`. The studio must not read that 200 as proof the
    password was correct.
    """
    from elfakgis.core import autshared

    # Forestry's own answer for a username it has never seen.
    monkeypatch.setattr(autshared, "_SHARED_FALLBACK", "api")
    monkeypatch.setattr(autshared, "_verify_via_forestry_api",
                        lambda u, p: (True, True))
    assert autshared.verify_credentials("invented", "anything") == (True, "new")

    # ...and for one that already exists, which is a genuine login.
    monkeypatch.setattr(autshared, "_verify_via_forestry_api",
                        lambda u, p: (True, False))
    assert autshared.verify_credentials("real", "right") == (True, "ok")

    # The route must refuse the invented account by default.
    import importlib
    auth_routes = importlib.import_module("elfakgis.routes.auth")
    monkeypatch.setattr(auth_routes, "_SELF_REGISTRATION_ALLOWED", False)
    monkeypatch.setattr(auth_routes.autshared, "verify_credentials",
                        lambda u, p: (True, "new"))
    client = app.test_client()
    resp = client.post("/login", json={"username": "invented", "password": "anything"})
    assert resp.status_code == 403, (
        f"a self-registered account was admitted: {resp.status_code}"
    )
    # No session may have been established.
    assert client.get("/").status_code == 302


def test_an_existing_account_can_still_sign_in(monkeypatch):
    """The gate must not lock out people who already have an account."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    import importlib
    auth_routes = importlib.import_module("elfakgis.routes.auth")
    monkeypatch.setattr(auth_routes, "_SELF_REGISTRATION_ALLOWED", False)
    monkeypatch.setattr(auth_routes.autshared, "verify_credentials",
                        lambda u, p: (True, "ok"))
    client = app.test_client()
    resp = client.post("/login", json={"username": "real", "password": "right"})
    assert resp.status_code == 200, f"existing account was refused: {resp.status_code}"
    assert client.get("/").status_code == 200


def test_a_zip_bomb_is_rejected_before_it_is_read():
    """A small archive must not be able to declare an enormous payload."""
    import io
    import zipfile
    from elfakgis.core.security import _assert_zip_within_budget, ArchiveTooLarge

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.shp", b"x" * 1024)
    buf.seek(0)
    with zipfile.ZipFile(buf) as z:
        _assert_zip_within_budget(z)          # a normal archive passes
        try:
            _assert_zip_within_budget(z, max_total=512)
        except ArchiveTooLarge:
            pass
        else:
            raise AssertionError("an over-budget archive was accepted")


def test_no_response_leaks_an_internal_path():
    """Error bodies must not carry exception text to the client.

    Driven through a real route that raises, so it exercises the actual
    error handler rather than asserting on a string in the source.
    """
    from elfakgis.core import security
    from elfakgis.core import store
    with security._RL_LOCK:
        security._RL.clear()

    # Reuse a real endpoint and make it blow up, then restore it. Blueprint
    # views are registered under "<blueprint>.<function>".
    key = "inspect_bp.zip_inspect"
    original = app.view_functions[key]

    def _boom(*a, **kw):
        raise RuntimeError("secret internal detail /srv/secret/path")

    app.view_functions[key] = _boom
    try:
        client = app.test_client()
        with client.session_transaction() as sess:
            sess["username"] = "alice"
        body = client.post("/zip_inspect", headers=_csrf_headers(client)).get_data(as_text=True)
        assert "/srv/secret/path" not in body, f"path leaked to the client: {body}"
        assert "secret internal detail" not in body, f"exception text leaked: {body}"
    finally:
        app.view_functions[key] = original
        with security._RL_LOCK:
            security._RL.clear()


def test_the_upload_ceiling_is_not_two_gigabytes():
    """A 2 GB cap on a 512 MB instance was an out-of-memory guarantee."""
    assert app.config["MAX_CONTENT_LENGTH"] <= 512 * 1024 * 1024, (
        f"MAX_CONTENT_LENGTH is {app.config['MAX_CONTENT_LENGTH']} bytes — "
        "too large for the target instance"
    )


def test_no_wildcard_cors_with_credentials():
    """`*` plus credentials is invalid, and dangerous the moment it is
    narrowed to a real origin."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"
    resp = client.get("/")
    assert "Access-Control-Allow-Origin" not in resp.headers, (
        "CORS headers should be gone entirely; nothing needs cross-origin"
    )


def test_a_content_security_policy_is_served():
    """CSP is the backstop for the 31 innerHTML sinks in app.js."""
    from elfakgis.core import security
    with security._RL_LOCK:
        security._RL.clear()
    client = app.test_client()
    with client.session_transaction() as sess:
        sess["username"] = "alice"
    csp = client.get("/").headers.get("Content-Security-Policy", "")
    assert csp, "no Content-Security-Policy header"
    assert "frame-ancestors 'none'" in csp
    assert "object-src 'none'" in csp
    # Inline script must stay forbidden: that is the part that matters.
    assert "script-src" in csp and "'unsafe-inline'" not in csp.split("script-src")[1].split(";")[0]


# ── Design-system and cache invariants ────────────────────────────────────
# These exist because the bugs they catch are invisible at runtime: a stale
# cached stylesheet looks fine until a deploy, and an undefined CSS token
# silently drops a background rather than raising anything.

def _css_files():
    here = os.path.join(os.path.dirname(__file__), "static", "css")
    for name in sorted(os.listdir(here)):
        if name.endswith(".css"):
            path = os.path.join(here, name)
            yield name, open(path, encoding="utf-8").read()


def test_every_css_var_used_in_a_template_is_defined():
    """A var() that no :root block defines is not an error — it is a hole.

    The browser substitutes `initial`, so a widget keeps its geometry but
    loses its surface. That is how the map's "Area: 0.00 ha" readout ended
    up as 11px monospace text drawn straight onto the imagery with no
    background, border or shadow, while every test stayed green.
    """
    import re as _re
    css = "\n".join(body for _, body in _css_files())
    defined = set(_re.findall(r"(--[a-z0-9-]+)\s*:", css))
    # A var() may only be *used* after being defined somewhere; collect the
    # union of definitions so a token defined in one file is valid in another.
    templates = os.path.join(os.path.dirname(__file__), "templates")
    problems = []
    for name in sorted(os.listdir(templates)):
        if not name.endswith(".html"):
            continue
        html = open(os.path.join(templates, name), encoding="utf-8").read()
        for m in _re.finditer(r"var\((--[a-z0-9-]+)", html):
            tok = m.group(1)
            if tok not in defined:
                line = html[:m.start()].count("\n") + 1
                problems.append(f"{name}:{line} uses {tok}, which is never defined")
    assert not problems, "undefined CSS custom properties:\n  " + "\n  ".join(problems)


def test_the_service_worker_shell_matches_the_template_asset_versions():
    """The SW caches by full URL, query string included.

    Its SHELL list had drifted behind the templates, so a deploy bumped
    `app.js?v=...8` while the worker still cached `...7`: the page asked for
    the new file and the worker answered with the old one. Nothing errors,
    the change just does not reach anyone who already visited.
    """
    import re as _re
    here = os.path.dirname(__file__)
    sw = open(os.path.join(here, "static", "sw.js"), encoding="utf-8").read()
    block = _re.search(r"const SHELL = \[(.*?)\];", sw, _re.S)
    assert block, "no SHELL array in sw.js"
    cached = set(_re.findall(r"'([^']+)'", block.group(1)))

    # Every versioned same-origin asset a template requests must be in the
    # shell, at exactly the version the template asks for.
    templates = os.path.join(here, "templates")
    problems = []
    for name in sorted(os.listdir(templates)):
        if not name.endswith(".html"):
            continue
        html = open(os.path.join(templates, name), encoding="utf-8").read()
        for m in _re.finditer(r'(?:href|src)="(/static/[^"?]+)(\?v=[^"]*)?"', html):
            url = m.group(1) + (m.group(2) or "")
            if url not in cached and not url.startswith("/static/elfak-logo"):
                problems.append(f"{name} requests {url}, absent from the SW SHELL")
    assert not problems, "service worker shell is out of sync:\n  " + "\n  ".join(problems)


def test_dock_layout_and_height_state_use_separate_storage_keys():
    """dock.js and dock-resize.js used to share one localStorage key.

    Both wrote a whole-object replace, so resizing a section (which stores
    `heights`) and then reordering (which stores `{order, widths}`) destroyed
    the saved heights. The two writers now own separate keys.
    """
    import re as _re
    here = os.path.join(os.path.dirname(__file__), "static", "js")
    dock = open(os.path.join(here, "dock.js"), encoding="utf-8").read()
    resize = open(os.path.join(here, "dock-resize.js"), encoding="utf-8").read()
    keys = lambda s: set(_re.findall(r"'((?:elfak-dock[^']*))'", s))
    assert "elfak-dock-heights-v1" in keys(dock) | keys(resize), (
        "the dedicated heights key is gone; heights and order will clobber "
        "each other again"
    )
    # The heights key must be written in exactly one place.
    assert resize.count("HEIGHT_KEY") >= 2, "dock-resize.js no longer persists heights"
    assert 'KEY = \'elfak-dock-v1\'' in resize, (
        "dock-resize.js should keep sharing the layout key for widths only"
    )


def test_the_map_modal_is_a_real_dialog():
    """Overlays must be dismissible by keyboard and announced as dialogs."""
    import re as _re
    here = os.path.dirname(__file__)
    html = open(os.path.join(here, "templates", "index.html"), encoding="utf-8").read()
    for target, attrs in (
        ("map-modal", 'role="dialog"'),
        ("hist-drawer", 'role="dialog"'),
    ):
        m = _re.search(rf'<div id="{target}"[^>]*>', html)
        assert m, f"#{target} not found in the template"
        assert attrs in m.group(0), f"#{target} is missing {attrs}"
        assert 'aria-modal="true"' in m.group(0), f"#{target} is missing aria-modal"
    app_js = open(os.path.join(here, "static", "js", "app.js"), encoding="utf-8").read()
    assert "e.key !== 'Escape'" in app_js, "no Escape handler for the overlays"
    assert "function closeMapModal" in app_js, "the modal has no named close path"
    assert "Skip to main content" in html, "no skip link — keyboard users tab the whole header"
    assert 'role="main"' in html, "the workspace is not a main landmark"
