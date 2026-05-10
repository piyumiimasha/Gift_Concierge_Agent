"""
Tier 3 — Semantic Long-Term Memory: Recipient Profile Store.

Implements the ``ProfileStore`` protocol from ``memory.schemas`` using
the Supabase ``recipient_profiles`` table.

Each row represents one person a user buys gifts for (e.g. "Wife", "Mum").
JSONB columns store preferences, dislikes, past gifts, and upcoming
occasions so the agent can personalise every gift recommendation.

Operations
----------
upsert(profile)              — create or fully overwrite a profile
get(user_id, name)           — fetch one profile by recipient name
list_profiles(user_id)       — all profiles for a user
delete(user_id, name)        — remove a profile
add_past_gift(...)           — append a gift to history without full rewrite
add_upcoming_occasion(...)   — append an occasion without full rewrite

Usage
-----
    from memory.profile_store import SupabaseProfileStore
    from memory.schemas import RecipientProfile, PastGift, UpcomingOccasion
    import time, uuid

    store = SupabaseProfileStore()

    profile = RecipientProfile(
        profile_id=str(uuid.uuid4()),
        user_id="u001",
        name="Wife",
        relationship="spouse",
        preferences=["dark chocolate", "orchids"],
        budget_lkr=5000.0,
        created_at=time.time(),
        updated_at=time.time(),
    )
    store.upsert(profile)

    p = store.get("u001", "Wife")
    print(p.preference_summary())
    # Wife | (spouse) | likes: dark chocolate, orchids | budget: LKR 5000
"""

from __future__ import annotations

import json
import time
from typing import List, Optional

from loguru import logger

from infastructure.db.supabase_client import get_supabase_client
from memory.schemas import PastGift, RecipientProfile, UpcomingOccasion

TABLE = "recipient_profiles"


class SupabaseProfileStore:
    """
    Recipient profile store backed by the Supabase ``recipient_profiles`` table.

    Satisfies the ``ProfileStore`` protocol defined in ``memory.schemas``.
    """

    def __init__(self) -> None:
        self._sb = get_supabase_client()

    # ------------------------------------------------------------------
    # ProfileStore protocol
    # ------------------------------------------------------------------

    def upsert(self, profile: RecipientProfile) -> None:
        """
        Insert or fully overwrite a recipient profile.

        Uses Postgres ``ON CONFLICT`` upsert via Supabase's ``upsert()``
        method — the unique index on (user_id, LOWER(name)) ensures that
        saving "Wife" twice updates rather than duplicates the row.
        """
        profile.updated_at = time.time()
        row = self._to_row(profile)
        try:
            self._sb.table(TABLE).upsert(row).execute()
            logger.info(
                "profile_store upserted profile '{}' for user '{}'",
                profile.name, profile.user_id,
            )
        except Exception as exc:
            logger.error("profile_store.upsert failed: {}", exc)
            raise

    def get(self, user_id: str, name: str) -> Optional[RecipientProfile]:
        """
        Fetch a single profile by recipient name (case-insensitive).

        Returns ``None`` if no profile exists for that name.
        """
        try:
            response = (
                self._sb.table(TABLE)
                .select("*")
                .eq("user_id", user_id)
                .ilike("name", name)   # case-insensitive match
                .limit(1)
                .execute()
            )
            rows = response.data or []
            if not rows:
                return None
            return self._from_row(rows[0])
        except Exception as exc:
            logger.error("profile_store.get failed: {}", exc)
            return None

    def list_profiles(self, user_id: str) -> List[RecipientProfile]:
        """Return all recipient profiles for a user, sorted by name."""
        try:
            response = (
                self._sb.table(TABLE)
                .select("*")
                .eq("user_id", user_id)
                .order("name")
                .execute()
            )
            return [self._from_row(r) for r in (response.data or [])]
        except Exception as exc:
            logger.error("profile_store.list_profiles failed: {}", exc)
            return []

    def delete(self, user_id: str, name: str) -> bool:
        """
        Delete a recipient profile by name.

        Returns ``True`` if a row was deleted, ``False`` if it didn't exist.
        """
        try:
            response = (
                self._sb.table(TABLE)
                .delete()
                .eq("user_id", user_id)
                .ilike("name", name)
                .execute()
            )
            deleted = len(response.data or []) > 0
            if deleted:
                logger.info(
                    "profile_store deleted profile '{}' for user '{}'", name, user_id
                )
            return deleted
        except Exception as exc:
            logger.error("profile_store.delete failed: {}", exc)
            return False

    # ------------------------------------------------------------------
    # Convenience mutators (avoid full profile round-trips)
    # ------------------------------------------------------------------

    def add_past_gift(
        self,
        user_id: str,
        name: str,
        gift: PastGift,
    ) -> bool:
        """
        Append a past gift to an existing profile's history.

        Fetches the current profile, appends the gift, and saves back.
        Returns ``False`` if the profile doesn't exist.
        """
        profile = self.get(user_id, name)
        if not profile:
            logger.warning(
                "profile_store.add_past_gift: profile '{}' not found", name
            )
            return False
        profile.past_gifts.append(gift)
        self.upsert(profile)
        return True

    def add_upcoming_occasion(
        self,
        user_id: str,
        name: str,
        occasion: UpcomingOccasion,
    ) -> bool:
        """
        Append an upcoming occasion to an existing profile.

        Returns ``False`` if the profile doesn't exist.
        """
        profile = self.get(user_id, name)
        if not profile:
            logger.warning(
                "profile_store.add_upcoming_occasion: profile '{}' not found", name
            )
            return False
        profile.upcoming_occasions.append(occasion)
        self.upsert(profile)
        return True

    def update_preferences(
        self,
        user_id: str,
        name: str,
        preferences: Optional[List[str]] = None,
        dislikes: Optional[List[str]] = None,
        budget_lkr: Optional[float] = None,
    ) -> bool:
        """
        Patch specific preference fields on an existing profile.

        Only the fields passed as non-None are updated.
        Returns ``False`` if the profile doesn't exist.
        """
        profile = self.get(user_id, name)
        if not profile:
            return False
        if preferences is not None:
            profile.preferences = preferences
        if dislikes is not None:
            profile.dislikes = dislikes
        if budget_lkr is not None:
            profile.budget_lkr = budget_lkr
        self.upsert(profile)
        return True

    # ------------------------------------------------------------------
    # Row serialisation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_row(profile: RecipientProfile) -> dict:
        """
        Convert a RecipientProfile to a flat dict suitable for Supabase.

        JSONB columns (preferences, dislikes, upcoming_occasions, past_gifts)
        are serialised to JSON strings because the supabase-py client does
        not always serialise nested dicts automatically.
        """
        return {
            "profile_id":         profile.profile_id,
            "user_id":            profile.user_id,
            "name":               profile.name,
            "relationship":       profile.relationship,
            "preferences":        json.dumps(profile.preferences),
            "dislikes":           json.dumps(profile.dislikes),
            "budget_lkr":         profile.budget_lkr,
            "upcoming_occasions": json.dumps(
                [o.to_dict() for o in profile.upcoming_occasions]
            ),
            "past_gifts":         json.dumps(
                [g.to_dict() for g in profile.past_gifts]
            ),
            "notes":              profile.notes,
            "created_at":         profile.created_at,
            "updated_at":         profile.updated_at,
        }

    @staticmethod
    def _from_row(row: dict) -> RecipientProfile:
        """
        Deserialise a Supabase row back into a RecipientProfile.

        Handles both raw strings (from the REST client) and already-parsed
        lists/dicts (Supabase sometimes returns JSONB columns pre-parsed).
        """
        def _load(value):
            if isinstance(value, str):
                return json.loads(value)
            return value or []

        return RecipientProfile(
            profile_id=row["profile_id"],
            user_id=row["user_id"],
            name=row["name"],
            relationship=row.get("relationship"),
            preferences=_load(row.get("preferences", "[]")),
            dislikes=_load(row.get("dislikes", "[]")),
            budget_lkr=row.get("budget_lkr"),
            upcoming_occasions=[
                UpcomingOccasion.from_dict(o)
                for o in _load(row.get("upcoming_occasions", "[]"))
            ],
            past_gifts=[
                PastGift.from_dict(g)
                for g in _load(row.get("past_gifts", "[]"))
            ],
            notes=row.get("notes"),
            created_at=row.get("created_at", 0.0),
            updated_at=row.get("updated_at", 0.0),
        )
