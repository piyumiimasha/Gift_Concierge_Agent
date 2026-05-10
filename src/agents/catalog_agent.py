"""
Catalog Agent — RAG-based gift product recommendation specialist.

Flow
----
1. Enrich the user query with recipient profile preferences
2. Map occasion keywords to category hints
3. Semantic search via QdrantRAGStore
4. Post-filter: remove disliked and previously gifted products
5. Format product candidates for LLM context
6. Groq LLM selects the 2–3 best matches and writes a warm recommendation (Draft)
7. Reflection: critique draft against recipient dislikes/allergies
8. Revise: if violations found, rewrite recommendation avoiding flagged products
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional

from dotenv import load_dotenv
from groq import Groq
from loguru import logger

from agents.prompts.agent_prompts import (
    build_catalog_prompt,
    build_reflect_prompt,
    build_revise_prompt,
)
from infastructure.observability import observe, update_current_observation
from memory.rag_store import QdrantRAGStore
from memory.schemas import ConversationTurn, ProductSearchResult, RecipientProfile

load_dotenv()

# Occasion keyword → preferred categories (None = no restriction)
_OCCASION_CATEGORY: dict[str, Optional[str]] = {
    "birthday":    None,
    "anniversary": None,
    "wedding":     "flowers",
    "christmas":   "hampers",
    "valentine":   "flowers",
    "valentines":  "flowers",
    "mother":      "flowers",
    "fathers":     None,
    "baby":        "baby",
    "graduation":  None,
}

# Hard category keywords in the message → direct category filter
_CATEGORY_KEYWORDS: dict[str, str] = {
    "cake":        "cakes",
    "flower":      "flowers",
    "chocolate":   "chocolates",
    "hamper":      "hampers",
    "jewel":       "jewellery",
    "toy":         "softtoys",
    "soft toy":    "softtoys",
    "grocery":     "grocery",
    "book":        "books",
    "sport":       "sports",
    "electronic":  "electronics",
    "cosmetic":    "cosmatics",
    "fashion":     "fashion",
    "clothing":    "clothing",
    "pharmacy":    "pharmacy",
    "fruit":       "fruitbaskets",
    "combo":       "combogifts",
}

_NO_RESULTS_REPLY = (
    "I searched the kapruka.com catalog but couldn't find products that "
    "match your request closely enough. Could you give me a bit more detail — "
    "perhaps the occasion, a category (cakes, flowers, chocolates…), or a "
    "budget range in LKR? I'll find something perfect!"
)


@dataclass
class CatalogResponse:
    reply:                str
    products_shown:       List[ProductSearchResult] = field(default_factory=list)
    query_used:           str = ""
    recipient_name:       Optional[str] = None
    reflection_triggered: bool = False
    violations_found:     bool = False


class CatalogAgent:
    """
    Gift product recommendation specialist using Qdrant semantic search
    + Groq LLM narration.

    Retrieves k=8 candidates so the LLM can select the 2–3 best fits
    rather than just presenting the top semantic match.
    """

    def __init__(
        self,
        rag_store: QdrantRAGStore,
        model: str = "llama-3.3-70b-versatile",
        k: int = 8,
        threshold: float = 0.30,
        temperature: float = 0.7,
    ) -> None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in the environment.")
        self._client = Groq(api_key=api_key)
        self._rag = rag_store
        self.model = model
        self.k = k
        self.threshold = threshold
        self.temperature = temperature

    @observe(name="catalog_agent")
    def recommend(
        self,
        message: str,
        profile: Optional[RecipientProfile] = None,
        recent_turns: Optional[List[ConversationTurn]] = None,
    ) -> CatalogResponse:
        """
        Produce a personalised gift recommendation.

        Parameters
        ----------
        message:
            Raw user request (e.g. "birthday gift for my wife under 3000").
        profile:
            Recipient profile with preferences, dislikes, past gifts.
            If None the agent still works — just without personalisation.
        recent_turns:
            Recent conversation turns for context (currently informational).
        """
        update_current_observation(input=message)

        # Step 1 — enrich query
        enriched, category_filter = self._enrich_query(message, profile)

        # Step 2 — RAG search
        results = self._rag.query(
            text=enriched,
            k=self.k,
            threshold=self.threshold,
            category_filter=category_filter,
        )

        # Step 3 — post-filter dislikes and past gifts
        if profile:
            results = self._filter_dislikes(results, profile)
            results = self._filter_past_gifts(results, profile)

        update_current_observation(
            metadata={
                "products_retrieved": len(results),
                "query_enriched": enriched,
                "category_filter": category_filter,
            }
        )

        if not results:
            logger.info("CatalogAgent: no results after filtering for query: {}", enriched)
            return CatalogResponse(
                reply=_NO_RESULTS_REPLY,
                query_used=enriched,
                recipient_name=profile.name if profile else None,
            )

        # Step 4 — format product block
        products_block = _format_products(results)
        profile_block = profile.preference_summary() if profile else "No profile available"

        # Step 5 — LLM recommendation
        try:
            system, user = build_catalog_prompt(products_block, profile_block, message)
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=600,
            )
            reply = response.choices[0].message.content or _NO_RESULTS_REPLY
        except Exception as exc:
            logger.error("CatalogAgent LLM call failed: {}", exc)
            reply = _plain_recommendation(results[:3])

        # Step 6 — Reflection loop (only when profile has dislikes or allergy notes)
        reflection_triggered = False
        violations_found = False

        if profile and (profile.dislikes or profile.notes):
            reflection_triggered = True
            try:
                critique, has_violations = self._reflect(reply, profile)
                logger.info(
                    "Reflection: violations={} — {}", has_violations, critique[:80]
                )
                if has_violations:
                    violations_found = True
                    reply = self._revise(critique, profile, products_block, message)
            except Exception as exc:
                logger.warning("Reflection loop failed: {} — keeping draft", exc)

        update_current_observation(
            output=reply[:200],
            metadata={
                "reflection_triggered": reflection_triggered,
                "violations_found": violations_found,
            },
        )
        return CatalogResponse(
            reply=reply,
            products_shown=results,
            query_used=enriched,
            recipient_name=profile.name if profile else None,
            reflection_triggered=reflection_triggered,
            violations_found=violations_found,
        )

    # ------------------------------------------------------------------
    # Reflection loop
    # ------------------------------------------------------------------

    def _reflect(
        self, draft_reply: str, profile: RecipientProfile
    ) -> tuple[str, bool]:
        """Critique draft against recipient dislikes/allergies. Returns (critique, has_violations)."""
        profile_block = profile.preference_summary()
        system, user = build_reflect_prompt(draft_reply, profile_block)
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=200,
        )
        critique = response.choices[0].message.content or "NO_VIOLATIONS"
        has_violations = "NO_VIOLATIONS" not in critique.upper()
        return critique, has_violations

    def _revise(
        self,
        critique: str,
        profile: RecipientProfile,
        products_block: str,
        original_message: str,
    ) -> str:
        """Rewrite the recommendation avoiding products flagged in the critique."""
        profile_block = profile.preference_summary()
        system, user = build_revise_prompt(
            critique, profile_block, products_block, original_message
        )
        response = self._client.chat.completions.create(
            model=self.model,
            temperature=self.temperature,
            messages=[
                {"role": "system", "content": system},
                {"role": "user",   "content": user},
            ],
            max_tokens=600,
        )
        return response.choices[0].message.content or ""

    # ------------------------------------------------------------------
    # Query enrichment
    # ------------------------------------------------------------------

    def _enrich_query(
        self,
        message: str,
        profile: Optional[RecipientProfile],
    ) -> tuple[str, Optional[str]]:
        """
        Build an enriched query string and determine a category filter.

        Returns (enriched_query, category_filter).
        """
        enriched = message
        msg_lower = message.lower()

        # Append profile preferences (top 3) and dislikes (top 2)
        if profile:
            if profile.preferences:
                enriched += " | preferences: " + ", ".join(profile.preferences[:3])
            if profile.dislikes:
                enriched += " | avoid: " + ", ".join(profile.dislikes[:2])
            if profile.budget_lkr:
                enriched += f" | budget LKR {profile.budget_lkr:.0f}"

        # Determine category filter — hard keyword wins over occasion hint
        category_filter: Optional[str] = None

        for keyword, category in _CATEGORY_KEYWORDS.items():
            if keyword in msg_lower:
                category_filter = category
                break

        if category_filter is None:
            for occasion, category in _OCCASION_CATEGORY.items():
                if occasion in msg_lower:
                    category_filter = category
                    break

        return enriched, category_filter

    # ------------------------------------------------------------------
    # Post-retrieval filters
    # ------------------------------------------------------------------

    @staticmethod
    def _filter_dislikes(
        results: List[ProductSearchResult],
        profile: RecipientProfile,
    ) -> List[ProductSearchResult]:
        if not profile.dislikes:
            return results
        filtered = []
        for r in results:
            text = (r.product.name + " " + (r.product.description or "")).lower()
            if not any(d.lower() in text for d in profile.dislikes):
                filtered.append(r)
        removed = len(results) - len(filtered)
        if removed:
            logger.debug("Filtered {} product(s) matching dislikes", removed)
        return filtered

    @staticmethod
    def _filter_past_gifts(
        results: List[ProductSearchResult],
        profile: RecipientProfile,
    ) -> List[ProductSearchResult]:
        if not profile.past_gifts:
            return results
        past_names = {g.product_name.lower() for g in profile.past_gifts}
        filtered = [
            r for r in results if r.product.name.lower() not in past_names
        ]
        removed = len(results) - len(filtered)
        if removed:
            logger.debug("Filtered {} product(s) already gifted before", removed)
        return filtered


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_products(results: List[ProductSearchResult]) -> str:
    lines = []
    for i, r in enumerate(results, 1):
        p = r.product
        desc = (p.description or "")[:150]
        price = f"LKR {p.price:,.0f}" if p.price else "Price not available"
        lines.append(
            f"Product {i}: {p.name}\n"
            f"  Category:     {p.category}\n"
            f"  Price:        {price}\n"
            f"  Availability: {p.availability}\n"
            f"  Description:  {desc}\n"
            f"  URL:          {p.url}\n"
            f"  Match score:  {r.score:.2f}\n"
            f"  ---"
        )
    return "\n".join(lines)


def _plain_recommendation(results: List[ProductSearchResult]) -> str:
    """Fallback plain-text recommendation when the LLM call fails."""
    if not results:
        return _NO_RESULTS_REPLY
    lines = ["Here are some gift ideas from kapruka.com:\n"]
    for r in results:
        p = r.product
        price = f"LKR {p.price:,.0f}" if p.price else "price on request"
        lines.append(f"• **{p.name}** ({price})\n  {p.url}\n")
    return "\n".join(lines)
