"""
SQLAlchemy engine for direct Postgres access.

Used for DDL execution (CREATE TABLE, CREATE INDEX) and raw SQL queries
that the supabase-py REST client does not support (e.g. multi-statement
DDL, DELETE … RETURNING with complex sub-selects).

Usage
-----
    from infastructure.db.sql_client import get_engine, execute_ddl

    engine = get_engine()

    with engine.connect() as conn:
        rows = conn.execute(text("SELECT COUNT(*) FROM st_turns")).fetchall()
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from loguru import logger
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

load_dotenv()


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    """
    Return the process-wide SQLAlchemy engine (lazy, cached).

    Uses SUPABASE_DB_URL from the environment, which must be a standard
    PostgreSQL connection string:
        postgresql://user:password@host:port/dbname

    Raises
    ------
    EnvironmentError
        If SUPABASE_DB_URL is missing from the env.
    """
    db_url = os.getenv("SUPABASE_DB_URL", "").strip()
    if not db_url:
        raise EnvironmentError("SUPABASE_DB_URL is not set in the environment.")

    engine = create_engine(
        db_url,
        pool_pre_ping=True,   # verify connection health before use
        pool_size=5,
        max_overflow=10,
    )
    logger.info("SQLAlchemy engine initialised")
    return engine


def execute_ddl(statements: list[str]) -> None:
    """
    Run a list of DDL statements inside a single autocommit connection.

    DDL in Postgres is transactional, but CREATE TABLE IF NOT EXISTS is
    idempotent so repeated runs are safe.

    Parameters
    ----------
    statements:
        List of SQL strings to execute in order.
    """
    engine = get_engine()
    with engine.begin() as conn:
        for stmt in statements:
            stmt = stmt.strip()
            if not stmt:
                continue
            logger.debug("DDL: {}", stmt[:80].replace("\n", " "))
            conn.execute(text(stmt))
    logger.success("DDL execution complete ({} statements)", len(statements))
