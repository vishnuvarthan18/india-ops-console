"""Connection pool. Deliberately thinner than the core API's: this app only
reads engine data, so there is no autocommit-per-record machinery here."""

import logging
from contextlib import contextmanager

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from app.settings import get_settings

logger = logging.getLogger(__name__)

_pool: ConnectionPool | None = None


def open_pool() -> ConnectionPool:
    global _pool
    s = get_settings()
    _pool = ConnectionPool(
        conninfo=s.dsn,
        min_size=s.db_pool_min,
        max_size=s.db_pool_max,
        kwargs={"row_factory": dict_row},
        open=True,
        timeout=15,
    )
    _pool.wait(timeout=30)
    logger.info("postgres pool open")
    return _pool


def close_pool() -> None:
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


def pool() -> ConnectionPool:
    if _pool is None:
        raise RuntimeError("connection pool is not open")
    return _pool


@contextmanager
def cursor(commit: bool = False):
    with pool().connection() as conn:
        with conn.cursor() as cur:
            yield cur
        conn.commit() if commit else conn.rollback()


def fetch_all(sql: str, params: tuple = ()) -> list[dict]:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def fetch_one(sql: str, params: tuple = ()) -> dict | None:
    with cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()


def execute(sql: str, params: tuple = ()) -> None:
    with cursor(commit=True) as cur:
        cur.execute(sql, params)
