-- Elfak GIS Studio — local database schema.
--
-- Apply to a LOCAL PostgreSQL only:
--     psql -d elfgis -f schema.sql
--
-- Do NOT run this against the shared Forestry PSC database. That database is
-- owned by the Forestry service: `users` there is its own table, and the
-- studio only ever reads it. The `gis_*` tables below are created by the app
-- itself on first use (elfakgis/core/db.py::_ensure_tables, idempotent), so
-- you only need this file to get a standalone local database that the app
-- can sign in against.

-- ── Accounts ───────────────────────────────────────────────────────
-- Mirrors the columns the studio reads. The studio issues exactly one query
-- against this table:  SELECT password FROM users WHERE username = :u
-- `password` holds a bcrypt hash ($2...) for modern accounts; the studio also
-- still accepts legacy plaintext and upgrades it on first sign-in.
CREATE TABLE IF NOT EXISTS users (
    username     VARCHAR(100) PRIMARY KEY,
    password     TEXT        NOT NULL,
    created_at   TIMESTAMP   DEFAULT NOW()
);

-- ── The four GIS tables ────────────────────────────────────────────
-- Kept identical to db.py::_ensure_tables_uncached so that a database
-- bootstrapped from this file is indistinguishable from one the app created.

CREATE TABLE IF NOT EXISTS gis_user_cache (
    username   VARCHAR(100) PRIMARY KEY,
    pw_hash    TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gis_runs (
    id          SERIAL PRIMARY KEY,
    username    VARCHAR(100) NOT NULL,
    run_id      VARCHAR(128) NOT NULL,
    module      VARCHAR(64),
    description TEXT,
    created_at  TIMESTAMP DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS gis_runs_user_idx ON gis_runs (username, id DESC);

CREATE TABLE IF NOT EXISTS gis_remember (
    username    VARCHAR(100) PRIMARY KEY,
    token_hash  TEXT NOT NULL,
    expires_at  TIMESTAMP NOT NULL DEFAULT NOW() + INTERVAL '30 days',
    created_at  TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS gis_seen (
    username    VARCHAR(100) PRIMARY KEY,
    first_seen  TIMESTAMP DEFAULT NOW()
);

-- ── Local test account ─────────────────────────────────────────────
-- Deliberately NOT seeded here. A bcrypt hash is 60 characters and is
-- rejected outright if even one is dropped, so a hand-copied hash in a SQL
-- file is a silent way to lock yourself out of your own local preview.
-- Create the account with a script that hashes the password itself:
--
--     python scripts/make_local_user.py localpreview
--
-- It prints the password it chose.
