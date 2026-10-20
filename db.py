"""
db.py
Connection helper for Neon Postgres with read_view logging.
"""
import env  # noqa: F401 - loads .env before DATABASE_URL is read

import os
import re
import psycopg2
from psycopg2.extras import RealDictCursor

# -------------------------------------------------------------
# SQL read logging (set DEBUG_SQL=false in Render env to silence)
# -------------------------------------------------------------
DEBUG_SQL = os.environ.get("DEBUG_SQL", "true").lower() in ("1", "true", "yes")
_VIEW_RE  = re.compile(r'\b(active_[a-z_]+)\b', re.IGNORECASE)

def _log_sql(sql, params, row_count):
    if not DEBUG_SQL:
        return
    views = sorted(set(_VIEW_RE.findall(sql)))
    vstr  = ', '.join(views) if views else '(base tables)'
    pstr  = ''
    if params:
        pstr = repr(params)
        if len(pstr) > 120:
            pstr = pstr[:117] + '...'
        pstr = f' params={pstr}'
    print(f'[SQL-READ] {vstr}{pstr} -> {row_count} row(s)', flush=True)

def read_view(conn, sql, params=None, one=False):
    """Execute a SELECT with automatic logging of views + params + row count."""
    with conn.cursor() as cur:
        cur.execute(sql, params or ())
        result = cur.fetchone() if one else cur.fetchall()
    count = (1 if result else 0) if one else len(result)
    _log_sql(sql, params, count)
    return result

def get_conn():
    return psycopg2.connect(
        os.environ["DATABASE_URL"],
        sslmode="require",
        cursor_factory=RealDictCursor
    )