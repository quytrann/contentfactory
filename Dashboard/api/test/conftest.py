"""Shared pytest config for the api test-suite.

CRITICAL ISOLATION RULE
=======================
Every test in this suite that touches the DB must run against the DEDICATED test
database ``contentfactory_test`` — NEVER the live ``contentfactory`` DB (which
holds the owner's real pages/jobs/videos). This conftest enforces that BEFORE any
app module is imported:

  1. It forces ``PGDATABASE=contentfactory_test`` into the environment.
  2. It imports ``db`` and re-points the module-global ``db.DATABASE_URL`` (which
     ``db.get_conn`` reads) at the test DB, in case a stale .env value won the race.
  3. It asserts the resolved URL ends in ``/contentfactory_test`` and refuses to run
     otherwise — a hard guard so a misconfiguration can never write to live data.

The test DB itself is created/seeded out-of-band by the tester before the run:

    psql -U postgres -d postgres -c "CREATE DATABASE contentfactory_test;"
    psql -U postgres -d contentfactory_test -f Dashboard/db/schema.sql
    psql -U postgres -d contentfactory_test -f Dashboard/db/seed.sql   # needs_input col

Tests that don't touch the DB (pure helper tests) are unaffected by this.
"""
import os
import sys

# --- make the api package importable (this file lives under api/test/) ---
_API_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _API_DIR not in sys.path:
    sys.path.insert(0, _API_DIR)

# --- force the test DB BEFORE importing db (db builds DATABASE_URL at import) ---
# Load the real .env first (for PGUSER/PGPASSWORD/PGHOST), then override the DB name.
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(_API_DIR, ".env"))
except Exception:
    pass
os.environ["PGDATABASE"] = "contentfactory_test"
# DATABASE_URL (if a full one is set in .env) would bypass PGDATABASE — drop it so
# db._build_url() reassembles from the PG* parts pointing at the test DB.
os.environ.pop("DATABASE_URL", None)

import db  # noqa: E402

# Re-point the module global in case it was computed from a now-removed DATABASE_URL.
db.DATABASE_URL = db._build_url()

# HARD GUARD: never let the suite run against anything but the test DB.
assert db.DATABASE_URL.rstrip("/").endswith("/contentfactory_test"), (
    f"REFUSING TO RUN: tests must target contentfactory_test, got {db.DATABASE_URL!r}. "
    "Set PGDATABASE=contentfactory_test and ensure no DATABASE_URL overrides it."
)
