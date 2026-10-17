"""
db.py
Connection helper for Neon Postgres.
"""
import env  # noqa: F401 - loads .env before DATABASE_URL is read

import os
import psycopg2
from psycopg2.extras import RealDictCursor

def get_conn():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        sslmode="require",
        cursor_factory=RealDictCursor
    )