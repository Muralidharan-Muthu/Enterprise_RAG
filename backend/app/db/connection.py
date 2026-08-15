import logging
import threading
from contextlib import contextmanager
from typing import Generator

import psycopg2
import psycopg2.pool
from pgvector.psycopg2 import register_vector

from app.config import settings

logger = logging.getLogger(__name__)

_pool: psycopg2.pool.ThreadedConnectionPool | None = None
# Semaphore mirrors maxconn so getconn() never blocks forever.
# Callers wait at most POOL_TIMEOUT seconds before receiving a 503.
_pool_sem: threading.Semaphore | None = None
POOL_MAXCONN = 20
POOL_TIMEOUT = 8  # seconds


def get_pool() -> psycopg2.pool.ThreadedConnectionPool:
    global _pool, _pool_sem
    if _pool is None:
        _pool = psycopg2.pool.ThreadedConnectionPool(
            minconn=2,
            maxconn=POOL_MAXCONN,
            host=settings.SUPABASE_HOST,
            port=settings.SUPABASE_PORT,
            dbname=settings.SUPABASE_DB,
            user=settings.SUPABASE_USER,
            password=settings.SUPABASE_PASSWORD,
            sslmode=settings.DB_SSLMODE,
            options=f"-c search_path={settings.SUPABASE_SCHEMA},public",
            keepalives=1,
            keepalives_idle=30,
            keepalives_interval=10,
            keepalives_count=5,
        )
        _pool_sem = threading.Semaphore(POOL_MAXCONN)
        logger.info("Database connection pool created (maxconn=%d)", POOL_MAXCONN)
    return _pool


def _get_valid_conn(pool: psycopg2.pool.ThreadedConnectionPool):
    conn = pool.getconn()
    if conn.closed:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
    except psycopg2.OperationalError:
        pool.putconn(conn, close=True)
        conn = pool.getconn()
    return conn


def _prepare_conn(conn) -> None:
    """Pin search_path on the checked-out backend, then register pgvector.

    Under the pgbouncer transaction pooler the connection's startup
    ``options=-c search_path=...`` is not reliably applied to the backend that
    serves register_vector's type lookup, so it intermittently fails with
    "vector type not found in the database". Issuing an explicit SET inside the
    open transaction (same backend) before register_vector makes the lookup
    deterministic; a short retry covers the rare residual transient.
    """
    last_exc = None
    for _ in range(3):
        try:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {settings.SUPABASE_SCHEMA}, public")
            register_vector(conn)
            return
        except psycopg2.ProgrammingError as exc:
            last_exc = exc
            conn.rollback()
    raise last_exc


@contextmanager
def get_db() -> Generator[psycopg2.extensions.connection, None, None]:
    pool = get_pool()
    sem = _pool_sem
    if sem is not None and not sem.acquire(timeout=POOL_TIMEOUT):
        raise RuntimeError(
            f"DB connection pool exhausted — all {POOL_MAXCONN} connections busy. "
            "Try again in a moment."
        )
    conn = None
    try:
        conn = _get_valid_conn(pool)
        _prepare_conn(conn)
        yield conn
        conn.commit()
    except Exception:
        if conn is not None:
            conn.rollback()
        raise
    finally:
        if conn is not None:
            pool.putconn(conn)
        if sem is not None:
            sem.release()


def check_db_health() -> bool:
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return True
    except Exception as exc:
        logger.error("DB health check failed: %s", exc)
        return False
