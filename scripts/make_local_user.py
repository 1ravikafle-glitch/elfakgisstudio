#!/usr/bin/env python
"""Create or update an account in a LOCAL PostgreSQL, hashing the password here.

Why a script instead of an INSERT in schema.sql: a bcrypt hash is 60
characters and is silently invalid if a single one is dropped while being
copied by hand. That failure is quiet — the row exists, the login is refused,
and the cause is not obvious. Hashing in-process removes the copy step.

Refuses to touch anything that is not obviously local.

    python scripts/make_local_user.py localpreview
    python scripts/make_local_user.py localpreview --password 'chosen-here'
"""

import argparse
import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import bcrypt
except ImportError:
    sys.exit("bcrypt is required: pip install bcrypt")

# Hosts that are unambiguously not a remote production database.
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1", "/tmp", "/var/run", "")


def _assert_local(database_url: str) -> str:
    """Refuse to write to anything that looks remote."""
    from urllib.parse import urlsplit

    parts = urlsplit(database_url)
    host = parts.hostname or ""
    if host in _LOCAL_HOSTS:
        return host or "local socket"
    if host.endswith(".neon.tech") or host.endswith(".onrender.com"):
        sys.exit(
            f"Refusing to write to {host}.\n"
            "This script is for a LOCAL database only. The shared Forestry PSC\n"
            "database owns its own `users` table, and seeding a throwaway\n"
            "password into it would lock a real user out."
        )
    sys.exit(
        f"Refusing to write to remote host {host}.\n"
        "Set DATABASE_URL to a local database, or pass --allow-remote if you\n"
        "are certain this is a throwaway instance."
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("username")
    ap.add_argument("--password", help="prompted for securely when omitted")
    ap.add_argument("--database-url", help="defaults to $DATABASE_URL")
    ap.add_argument("--allow-remote", action="store_true",
                    help="bypass the local-only guard")
    args = ap.parse_args()

    url = (args.database_url or os.environ.get("DATABASE_URL") or "").strip()
    if not url:
        sys.exit("No DATABASE_URL. Set it in .env or pass --database-url.")
    if not args.allow_remote:
        _assert_local(url)

    password = args.password or (getpass.getpass("Password: ") or "")
    if not password:
        password = secrets.token_urlsafe(12)
        print(f"Generated password: {password}")

    # Hash in-process. Verify before writing, so a bad hash can never land.
    pw_hash = bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt(rounds=12)).decode()
    assert len(pw_hash) == 60, f"bcrypt produced a {len(pw_hash)}-char hash, expected 60"
    if not bcrypt.checkpw(password.encode("utf-8"), pw_hash.encode()):
        sys.exit("Generated hash failed verification; refusing to write it.")

    from elfakgis.core import db
    db.os.environ["DATABASE_URL"] = url
    if not db.available():
        sys.exit(f"Cannot connect: {db.status()}")

    from sqlalchemy import text
    with db._connect().begin() as conn:
        conn.execute(text("""
            INSERT INTO users (username, password) VALUES (:u, :h)
            ON CONFLICT (username) DO UPDATE SET password = EXCLUDED.password
        """), {"u": args.username, "h": pw_hash})

    print(f"Account {args.username!r} written to {db._normalize_database_url(url).split('@')[-1]}")
    print("It can now sign in at /login.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
