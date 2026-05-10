"""
Supabase client singleton.

Reads SUPABASE_URL and SUPABASE_SERVICE_KEY from the environment and
returns a single shared ``supabase.Client`` instance for the process.

Usage
-----
    from infastructure.db.supabase_client import get_supabase_client

    sb = get_supabase_client()
    rows = sb.table("st_turns").select("*").execute()
"""

from __future__ import annotations

import os
from functools import lru_cache

from dotenv import load_dotenv
from loguru import logger
from supabase import Client, create_client

load_dotenv()


@lru_cache(maxsize=1)
def get_supabase_client() -> Client:
    """
    Return the process-wide Supabase client (lazy, cached).

    Raises
    ------
    EnvironmentError
        If SUPABASE_URL or SUPABASE_SERVICE_KEY are missing from the env.
    """
    url = os.getenv("SUPABASE_URL", "").strip()
    key = os.getenv("SUPABASE_SERVICE_KEY", "").strip()

    if not url:
        raise EnvironmentError("SUPABASE_URL is not set in the environment.")
    if not key:
        raise EnvironmentError("SUPABASE_SERVICE_KEY is not set in the environment.")

    client: Client = create_client(url, key)
    logger.info("Supabase client initialised ({})", url)
    return client
