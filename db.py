"""
db.py
Connection helper for Master DB and Tenant DBs.
Implements multi-tenant connection pooling for Render.
"""
import env  # noqa: F401
import os
import re
import sys
import time
import logging
import psycopg2
import psycopg2.pool
import psycopg2.extras
from psycopg2.extras import RealDictCursor

# -------------------------------------------------------------
# 1. Logging Setup
# -------------------------------------------------------------
logger = logging.getLogger("db")
logger.setLevel(logging.INFO)
if not logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] [db] %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)

DEBUG_SQL = os.environ.get("DEBUG_SQL", "true").lower() in ("1", "true", "yes")
if not DEBUG_SQL:
    logger.setLevel(logging.WARNING)

MASTER_DATABASE_URL = os.environ.get("MASTER_DATABASE_URL") or os.environ.get("DATABASE_URL")
_VIEW_RE = re.compile(r'\b(active_[a-z_]+)\b', re.IGNORECASE)

# -------------------------------------------------------------
# 2. Multi-Tenant Pool Manager
# -------------------------------------------------------------
_pools = {}       # db_url -> ThreadedConnectionPool
_conn_pools = {}  # id(conn) -> ThreadedConnectionPool

def get_pool(db_url):
    global _pools
    if db_url not in _pools:
        _pools[db_url] = psycopg2.pool.ThreadedConnectionPool(
            minconn=1,
            maxconn=10,  # Max 10 connections per tenant
            dsn=db_url,
            client_encoding="utf8",
            cursor_factory=RealDictCursor
        )
    return _pools[db_url]

def _ping_conn(conn, pool):
    """Checks if the connection is alive, replaces it if dead."""
    try:
        if conn.closed != 0:
            raise psycopg2.OperationalError("Connection is closed")
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.OperationalError:
        logger.warning("[DB] Dead connection detected. Replacing...")
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn

def get_master_conn():
    pool = get_pool(MASTER_DATABASE_URL)
    conn = pool.getconn()
    conn = _ping_conn(conn, pool)
    _conn_pools[id(conn)] = pool
    return conn

def get_conn(instance_id):
    config = get_tenant_config(instance_id)
    db_url = config["db_connect_string"]
    pool = get_pool(db_url)
    conn = pool.getconn()
    conn = _ping_conn(conn, pool)
    _conn_pools[id(conn)] = pool
    return conn

def release_conn(conn):
    """Returns the connection to its correct pool."""
    pool = _conn_pools.pop(id(conn), None)
    if pool:
        try: conn.rollback()
        except Exception: pass
        pool.putconn(conn)
    else:
        try: conn.close()
        except Exception: pass

# -------------------------------------------------------------
# 3. Query Logging & Execution
# -------------------------------------------------------------
def _log_sql(sql, params, row_count, duration_ms):
    if not DEBUG_SQL and duration_ms < 500:
        return
    sql_clean = " ".join(sql.split())
    if len(sql_clean) > 150: sql_clean = sql_clean[:147] + "..."
    pstr = ""
    if params:
        pstr = repr(params)
        if len(pstr) > 150: pstr = pstr[:147] + "..."
        pstr = f" | params={pstr}"
    views = sorted(set(_VIEW_RE.findall(sql)))
    vstr = f" [{','.join(views)}]" if views else ""
    if duration_ms > 500:
        logger.warning(f"[SLOW-SQL] {duration_ms:.1f}ms -> {row_count} rows{vstr} | {sql_clean}{pstr}")
    else:
        logger.info(f"[SQL-READ] {duration_ms:.1f}ms -> {row_count} rows{vstr} | {sql_clean}{pstr}")

def read_view(conn, sql, params=None, one=False):
    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
            result = cur.fetchone() if one else cur.fetchall()
    except Exception as e:
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.error(f"[SQL-ERROR] {duration_ms:.1f}ms | {sql} | params={params} | Error: {e}")
        raise
    duration_ms = (time.perf_counter() - t0) * 1000
    count = (1 if result else 0) if one else len(result)
    _log_sql(sql, params, count, duration_ms)
    return result

def execute_write(conn, sql, params=None, commit=True):
    t0 = time.perf_counter()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params or ())
        if commit: conn.commit()
    except Exception as e:
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.error(f"[SQL-ERROR] {duration_ms:.1f}ms | {sql} | params={params} | Error: {e}")
        try: conn.rollback()
        except Exception: pass
        raise
    duration_ms = (time.perf_counter() - t0) * 1000
    if DEBUG_SQL:
        sql_clean = " ".join(sql.split())
        if len(sql_clean) > 150: sql_clean = sql_clean[:147] + "..."
        logger.info(f"[SQL-WRITE] {duration_ms:.1f}ms | {sql_clean}")

# -------------------------------------------------------------
# 4. Tenant Config
# -------------------------------------------------------------
def get_tenant_config(instance_id):
    conn = get_master_conn()
    try:
        row = read_view(
            conn, 
            "SELECT db_connect_string, gemini_api_key, instance_type, allowed_origins, brand_color FROM v_tenant_config WHERE instance_id = %s", 
            (instance_id,), 
            one=True
        )
        if not row:
            logger.warning(f"[TENANT-CONFIG] Instance not found or inactive: {instance_id}")
            raise ValueError(f"Instance config not found for {instance_id}")
        return row
    except Exception as e:
        if not isinstance(e, ValueError):
            logger.error(f"[TENANT-CONFIG-ERROR] Failed to fetch config for {instance_id}: {e}")
        raise
    finally:
        release_conn(conn)