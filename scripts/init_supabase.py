"""
One-time database initialisation script.

Creates all tables and indexes defined in supabase_schema.py.
Safe to re-run — all statements use IF NOT EXISTS.

Usage
-----
    python scripts/init_supabase.py
    python scripts/init_supabase.py --verify
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make src/ importable when running from the project root
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from loguru import logger
from infastructure.db.supabase_schema import ALL_DDL
from infastructure.db.sql_client import execute_ddl, get_engine
from sqlalchemy import text


def init_db() -> None:
    """Run all DDL statements to create tables and indexes."""
    logger.info("Initialising Gift Concierge database...")
    execute_ddl(ALL_DDL)
    logger.success("Database initialised successfully.")


def verify_db() -> None:
    """Print row counts for all managed tables to confirm they exist."""
    tables = ["st_turns", "recipient_profiles"]
    engine = get_engine()
    with engine.connect() as conn:
        for table in tables:
            try:
                count = conn.execute(
                    text(f"SELECT COUNT(*) FROM {table}")
                ).scalar()
                logger.info("  {:30s} {:>6} rows", table, count)
            except Exception as exc:
                logger.error("  {:30s} ERROR — {}", table, exc)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Initialise the Gift Concierge Supabase database."
    )
    parser.add_argument(
        "--verify",
        action="store_true",
        help="After creating tables, print row counts to verify.",
    )
    args = parser.parse_args()

    init_db()

    if args.verify:
        logger.info("Verifying tables...")
        verify_db()
