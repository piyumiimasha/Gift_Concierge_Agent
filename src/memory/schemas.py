"""
Memory schemas and interfaces for the Gift Concierge Agent.

Dataclasses for conversation turns, catalog products, recipient profiles,
gift occasions, and past gift history.
Protocol definitions for short-term store, RAG store, profile store,
embedder, and clock contracts.
"""

from dataclasses import dataclass, field
from typing import List, Optional, Literal, Dict, Protocol, Iterable


# ===========================================================================
# TIER 1 — Short-Term Memory  (session conversation buffer)
# ===========================================================================

@dataclass
class ConversationTurn:
    """
    A single turn in a gift concierge conversation (user or assistant).

    Stored in Supabase ``st_turns`` table as a ring buffer per session.
    """
    user_id:    str
    session_id: str
    role:       Literal["user", "assistant", "system", "tool"]
    content:    str
    ts:         float  # epoch seconds

    def to_dict(self) -> Dict:
        return {
            "user_id":    self.user_id,
            "session_id": self.session_id,
            "role":       self.role,
            "content":    self.content,
            "ts":         self.ts,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "ConversationTurn":
        return cls(
            user_id=data["user_id"],
            session_id=data["session_id"],
            role=data["role"],
            content=data["content"],
            ts=data["ts"],
        )


# ===========================================================================
# TIER 2 — Long-Term RAG Memory  (Qdrant vector store over catalog.json)
# ===========================================================================

@dataclass
class CatalogProduct:
    """
    A kapruka.com product ingested from catalog.json.

    Stored in Qdrant with a vector of ``embed_text`` for semantic search.
    ``embed_text`` concatenates the fields most useful for gift queries
    (e.g. "I need something sweet under LKR 2000 for my mum").
    """
    product_id:   str
    name:         str
    category:     str
    url:          str
    price:        Optional[float] = None        # LKR
    description:  Optional[str]  = None
    availability: str             = "Unknown"   # "In Stock" | "Out of Stock"
    scraped_at:   str             = ""
    similarity:   Optional[float] = None        # populated during retrieval

    @property
    def embed_text(self) -> str:
        """Text sent to the embedding model."""
        parts = [self.name, self.category]
        if self.description:
            parts.append(self.description)
        if self.price is not None:
            parts.append(f"Price LKR {self.price:.0f}")
        parts.append(f"Availability {self.availability}")
        return " | ".join(parts)

    def to_dict(self) -> Dict:
        return {
            "product_id":   self.product_id,
            "name":         self.name,
            "category":     self.category,
            "url":          self.url,
            "price":        self.price,
            "description":  self.description,
            "availability": self.availability,
            "scraped_at":   self.scraped_at,
            "similarity":   self.similarity,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "CatalogProduct":
        return cls(
            product_id=data["product_id"],
            name=data["name"],
            category=data["category"],
            url=data["url"],
            price=data.get("price"),
            description=data.get("description"),
            availability=data.get("availability", "Unknown"),
            scraped_at=data.get("scraped_at", ""),
            similarity=data.get("similarity"),
        )


@dataclass
class ProductSearchResult:
    """
    A single result from a Qdrant semantic search over the gift catalog.
    """
    product:    CatalogProduct
    score:      float   # cosine similarity 0–1
    query:      str     # the gift-finder query that produced this result

    def to_dict(self) -> Dict:
        return {
            "product": self.product.to_dict(),
            "score":   self.score,
            "query":   self.query,
        }


# ===========================================================================
# TIER 3 — Semantic Long-Term Memory  (Recipient Profiles)
# ===========================================================================

OCCASION_TYPES = Literal[
    "birthday", "anniversary", "wedding",
    "christmas", "valentines", "mothers_day",
    "fathers_day", "new_year", "other",
]


@dataclass
class PastGift:
    """
    A previously purchased gift recorded against a recipient.

    Lets the agent avoid repeating gifts and understand spending history.
    """
    occasion:     OCCASION_TYPES
    product_name: str
    price:        Optional[float] = None   # LKR
    date:         Optional[str]   = None   # ISO date e.g. "2025-12-25"
    notes:        Optional[str]   = None

    def to_dict(self) -> Dict:
        return {
            "occasion":     self.occasion,
            "product_name": self.product_name,
            "price":        self.price,
            "date":         self.date,
            "notes":        self.notes,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "PastGift":
        return cls(
            occasion=data["occasion"],
            product_name=data["product_name"],
            price=data.get("price"),
            date=data.get("date"),
            notes=data.get("notes"),
        )


@dataclass
class UpcomingOccasion:
    """
    A future gift occasion for a recipient.

    Used to surface proactive reminders (e.g. "Wife's birthday in 7 days").
    """
    occasion: OCCASION_TYPES
    date:     str            # ISO date e.g. "2026-06-15"
    notes:    Optional[str] = None

    def to_dict(self) -> Dict:
        return {"occasion": self.occasion, "date": self.date, "notes": self.notes}

    @classmethod
    def from_dict(cls, data: Dict) -> "UpcomingOccasion":
        return cls(
            occasion=data["occasion"],
            date=data["date"],
            notes=data.get("notes"),
        )


@dataclass
class RecipientProfile:
    """
    Semantic profile for a gift recipient.

    Persisted as JSON (Supabase or local ``profiles.json``) and loaded
    into the agent context when the user mentions a person.

    Example:
        RecipientProfile(
            profile_id="p001",
            user_id="u001",
            name="Wife",
            relationship="spouse",
            preferences=["dark chocolate", "orchids", "spa"],
            budget_lkr=5000.0,
        )
    """
    profile_id:          str
    user_id:             str
    name:                str                      # "Wife", "Mum", "John", …
    relationship:        Optional[str] = None     # "spouse", "mother", "friend", …
    preferences:         List[str]     = field(default_factory=list)
    dislikes:            List[str]     = field(default_factory=list)
    budget_lkr:          Optional[float] = None
    upcoming_occasions:  List[UpcomingOccasion] = field(default_factory=list)
    past_gifts:          List[PastGift]         = field(default_factory=list)
    notes:               Optional[str] = None    # free-form notes from the user
    created_at:          float = 0.0             # epoch seconds
    updated_at:          float = 0.0             # epoch seconds

    def preference_summary(self) -> str:
        """One-line summary injected into agent prompts."""
        parts = [self.name]
        if self.relationship:
            parts.append(f"({self.relationship})")
        if self.preferences:
            parts.append(f"likes: {', '.join(self.preferences)}")
        if self.dislikes:
            parts.append(f"dislikes: {', '.join(self.dislikes)}")
        if self.budget_lkr:
            parts.append(f"budget: LKR {self.budget_lkr:.0f}")
        return " | ".join(parts)

    def to_dict(self) -> Dict:
        return {
            "profile_id":         self.profile_id,
            "user_id":            self.user_id,
            "name":               self.name,
            "relationship":       self.relationship,
            "preferences":        self.preferences,
            "dislikes":           self.dislikes,
            "budget_lkr":         self.budget_lkr,
            "upcoming_occasions": [o.to_dict() for o in self.upcoming_occasions],
            "past_gifts":         [g.to_dict() for g in self.past_gifts],
            "notes":              self.notes,
            "created_at":         self.created_at,
            "updated_at":         self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict) -> "RecipientProfile":
        return cls(
            profile_id=data["profile_id"],
            user_id=data["user_id"],
            name=data["name"],
            relationship=data.get("relationship"),
            preferences=data.get("preferences", []),
            dislikes=data.get("dislikes", []),
            budget_lkr=data.get("budget_lkr"),
            upcoming_occasions=[
                UpcomingOccasion.from_dict(o)
                for o in data.get("upcoming_occasions", [])
            ],
            past_gifts=[
                PastGift.from_dict(g)
                for g in data.get("past_gifts", [])
            ],
            notes=data.get("notes"),
            created_at=data.get("created_at", 0.0),
            updated_at=data.get("updated_at", 0.0),
        )


# ===========================================================================
# Protocol interfaces
# ===========================================================================

class ShortTermStore(Protocol):
    """
    Short-term memory store (Supabase ``st_turns`` table).

    Stores recent conversation turns as a ring buffer with TTL.
    """

    def append(
        self,
        turn: ConversationTurn,
        max_turns: int,
        ttl_seconds: int,
    ) -> None: ...

    def recent(
        self,
        user_id: str,
        session_id: str,
        k: int,
    ) -> List[ConversationTurn]: ...


class RAGStore(Protocol):
    """
    Long-term RAG store (Qdrant vector store over kapruka catalog).

    Products are embedded and queried by natural-language gift requests.
    """

    def upsert(self, products: Iterable[CatalogProduct]) -> None: ...

    def query(
        self,
        text: str,
        k: int,
        threshold: float,
        category_filter: Optional[str],
    ) -> List[ProductSearchResult]: ...

    def delete(self, product_id: str) -> None: ...

    def count(self) -> int: ...


class ProfileStore(Protocol):
    """
    Semantic long-term store for recipient profiles (Supabase or JSON file).
    """

    def upsert(self, profile: RecipientProfile) -> None: ...

    def get(self, user_id: str, name: str) -> Optional[RecipientProfile]: ...

    def list_profiles(self, user_id: str) -> List[RecipientProfile]: ...

    def delete(self, user_id: str, name: str) -> bool: ...


class Embedder(Protocol):
    """Text embedder — wraps the embedding model for a consistent interface."""

    def embed(self, texts: List[str]) -> List[List[float]]: ...


class Clock(Protocol):
    """Clock abstraction — allows fast-forwarding time in tests."""

    def now(self) -> float: ...
