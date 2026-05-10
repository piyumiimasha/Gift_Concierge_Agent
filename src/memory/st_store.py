"""
Tier 1 — Short-Term Memory Store.

Implements the ``ShortTermStore`` protocol from ``memory.schemas`` using
Supabase (Postgres) as the backend.

The store acts as a ring buffer: each ``append`` inserts the new turn and
then prunes the oldest turns beyond ``max_turns``. A TTL-based prune also
removes turns older than ``ttl_seconds`` so stale sessions don't pile up.

Usage
-----
    from memory.st_store import SupabaseSTStore
    from memory.schemas import ConversationTurn

    store = SupabaseSTStore()

    store.append(
        ConversationTurn(
            user_id="u001",
            session_id="s001",
            role="user",
            content="I need a birthday gift for my wife",
            ts=time.time(),
        ),
        max_turns=20,
        ttl_seconds=3600,
    )

    history = store.recent("u001", "s001", k=10)
"""

from __future__ import annotations

import time
from typing import List

from loguru import logger

from infastructure.db.supabase_client import get_supabase_client
from memory.schemas import ConversationTurn


class SupabaseSTStore:
    """
    Short-term memory store backed by the Supabase ``st_turns`` table.

    Satisfies the ``ShortTermStore`` protocol defined in ``memory.schemas``.
    """

    TABLE = "st_turns"

    def __init__(self) -> None:
        self._sb = get_supabase_client()

    # ------------------------------------------------------------------
    # ShortTermStore protocol
    # ------------------------------------------------------------------

    def append(
        self,
        turn: ConversationTurn,
        max_turns: int = 20,
        ttl_seconds: int = 3600,
    ) -> None:
        """
        Insert ``turn`` into the store and enforce the ring-buffer limits.

        After inserting, two pruning passes run:
        1. TTL prune  — delete turns older than ``ttl_seconds``
        2. Count prune — delete oldest turns beyond ``max_turns`` for the
                         (user_id, session_id) pair
        """
        self._insert(turn)
        self._prune_ttl(turn.user_id, turn.session_id, ttl_seconds)
        self._prune_count(turn.user_id, turn.session_id, max_turns)

    def recent(
        self,
        user_id: str,
        session_id: str,
        k: int = 10,
    ) -> List[ConversationTurn]:
        """
        Return the ``k`` most recent turns for a session, oldest first.

        Oldest-first ordering means the list can be passed directly as an
        LLM message history without reversing.
        """
        try:
            response = (
                self._sb.table(self.TABLE)
                .select("*")
                .eq("user_id", user_id)
                .eq("session_id", session_id)
                .order("ts", desc=True)
                .limit(k)
                .execute()
            )
            rows = response.data or []
            # Reverse so the list is chronological (oldest → newest)
            rows.reverse()
            return [ConversationTurn.from_dict(r) for r in rows]
        except Exception as exc:
            logger.error("st_store.recent failed: {}", exc)
            return []

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _insert(self, turn: ConversationTurn) -> None:
        try:
            self._sb.table(self.TABLE).insert(turn.to_dict()).execute()
        except Exception as exc:
            logger.error("st_store._insert failed: {}", exc)
            raise

    def _prune_ttl(self, user_id: str, session_id: str, ttl_seconds: int) -> None:
        """Delete turns older than ``ttl_seconds`` for this session."""
        cutoff = time.time() - ttl_seconds
        try:
            self._sb.table(self.TABLE).delete().eq(
                "user_id", user_id
            ).eq(
                "session_id", session_id
            ).lt("ts", cutoff).execute()
        except Exception as exc:
            logger.warning("st_store._prune_ttl failed: {}", exc)

    def _prune_count(self, user_id: str, session_id: str, max_turns: int) -> None:
        """
        Keep only the ``max_turns`` most recent turns for a session.

        Fetches the (max_turns+1)-th oldest turn's ``ts`` and deletes
        everything older than that, avoiding a subquery that Supabase's
        REST layer doesn't support.
        """
        try:
            # Fetch IDs of all turns for this session, oldest first
            response = (
                self._sb.table(self.TABLE)
                .select("id")
                .eq("user_id", user_id)
                .eq("session_id", session_id)
                .order("ts", desc=False)
                .execute()
            )
            rows = response.data or []
            excess = len(rows) - max_turns
            if excess <= 0:
                return

            # Collect the IDs of the oldest ``excess`` turns
            ids_to_delete = [r["id"] for r in rows[:excess]]
            self._sb.table(self.TABLE).delete().in_(
                "id", ids_to_delete
            ).execute()
            logger.debug(
                "st_store pruned {} old turn(s) for session {}", excess, session_id
            )
        except Exception as exc:
            logger.warning("st_store._prune_count failed: {}", exc)

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------

    def clear_session(self, user_id: str, session_id: str) -> None:
        """Delete all turns for a session (e.g. on logout or reset)."""
        try:
            self._sb.table(self.TABLE).delete().eq(
                "user_id", user_id
            ).eq("session_id", session_id).execute()
            logger.info("st_store cleared session {}", session_id)
        except Exception as exc:
            logger.error("st_store.clear_session failed: {}", exc)

    def session_turn_count(self, user_id: str, session_id: str) -> int:
        """Return the current number of turns stored for a session."""
        try:
            response = (
                self._sb.table(self.TABLE)
                .select("id", count="exact")
                .eq("user_id", user_id)
                .eq("session_id", session_id)
                .execute()
            )
            return response.count or 0
        except Exception as exc:
            logger.error("st_store.session_turn_count failed: {}", exc)
            return 0
