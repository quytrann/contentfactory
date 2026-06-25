"""Database connection for the dashboard API.

Reads connection settings from the environment (see .env.example). Prefers a
full DATABASE_URL; otherwise assembles one from the PG* parts. A single local
PostgreSQL is enough for this dashboard, so we open a short-lived connection
per request rather than running a pool.
"""

import os
from contextlib import contextmanager

import psycopg
from dotenv import load_dotenv
from psycopg.rows import dict_row

load_dotenv()


def _build_url() -> str:
    url = os.getenv("DATABASE_URL")
    if url:
        return url
    user = os.getenv("PGUSER", "postgres")
    password = os.getenv("PGPASSWORD", "")
    host = os.getenv("PGHOST", "localhost")
    port = os.getenv("PGPORT", "5432")
    database = os.getenv("PGDATABASE", "contentfactory")
    return f"postgresql://{user}:{password}@{host}:{port}/{database}"


DATABASE_URL = _build_url()


@contextmanager
def get_conn():
    """Yield a connection whose rows come back as dicts."""
    with psycopg.connect(DATABASE_URL, row_factory=dict_row) as conn:
        yield conn
