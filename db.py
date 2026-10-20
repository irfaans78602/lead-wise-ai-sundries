"""
db.py
Connection helper for Master DB and Tenant DBs.
Includes advanced production logging for Render.com.
"""
import env  # noqa: F401
import os
import re
import sys
import time
import logging
import psycopg2
from psycopg2.extras import RealDictCursor

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

def get_master_conn():
    t0 = time.perf_counter()
    try:
        conn = psycopg2.connect(MASTER_DATABASE_URL, sslmode="require", cursor_factory=RealDictCursor)
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.info(f"[DB-CONNECT] Master DB connected in {duration_ms:.1f}ms")
        return conn
    except Exception as e:
        logger.error(f"[DB-CONNECT-ERROR] Master DB failed to connect: {e}")
        raise

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
        conn.close()

def get_conn(instance_id):
    config = get_tenant_config(instance_id)
    t0 = time.perf_counter()
    try:
        conn = psycopg2.connect(config["db_connect_string"], sslmode="require", cursor_factory=RealDictCursor)
        duration_ms = (time.perf_counter() - t0) * 1000
        logger.info(f"[DB-CONNECT] Tenant DB connected for {instance_id[:8]}... in {duration_ms:.1f}ms")
        return conn
    except Exception as e:
        logger.error(f"[DB-CONNECT-ERROR] Tenant DB failed to connect for {instance_id[:8]}...: {e}")
        raise