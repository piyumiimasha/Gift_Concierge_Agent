"""
Gift Concierge Orchestrator — main entry point for the agent.

Manages a full conversation turn:
  1. Load short-term history and recipient profiles
  2. Classify intent via IntentRouter
  3. Dispatch to the appropriate specialist
  4. Persist both turns to Supabase short-term store
  5. Return a structured OrchestratorResponse

Usage
-----
    from agents.orchestrator import GiftOrchestrator
    from memory.st_store import SupabaseSTStore
    from memory.profile_store import SupabaseProfileStore
    from memory.rag_store import QdrantRAGStore
    from memory.embedder import OpenRouterEmbedder

    orch = GiftOrchestrator(
        st_store=SupabaseSTStore(),
        profile_store=SupabaseProfileStore(),
        rag_store=QdrantRAGStore(embedder=OpenRouterEmbedder()),
    )

    response = orch.chat(
        user_id="u001",
        session_id="s001",
        message="Find a birthday gift for my wife who loves dark chocolate",
    )
    print(response.reply)
"""

from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from groq import Groq
from loguru import logger

from agents.catalog_agent import CatalogAgent
from agents.logistics_agent import LogisticsAgent
from agents.prompts.agent_prompts import (
    build_chitchat_prompt,
    build_preference_extract_prompt,
)
from agents.prompts.memory_prompts import format_recipient_profiles
from agents.router import IntentRouter, RouterResult
from infastructure.observability import observe, update_current_observation
from memory.profile_store import SupabaseProfileStore
from memory.rag_store import QdrantRAGStore
from memory.schemas import ConversationTurn, RecipientProfile
from memory.st_store import SupabaseSTStore
from services.delivery_zones import DeliveryZoneService

load_dotenv()


@dataclass
class OrchestratorResponse:
    reply:      str
    intent:     str
    confidence: float
    session_id: str
    user_id:    str
    metadata:   Dict[str, Any] = field(default_factory=dict)


class GiftOrchestrator:
    """
    Top-level orchestrator for the Gift Concierge Agent.

    Sub-components (router, catalog_agent, logistics_agent) are lazily
    instantiated if not injected — this allows tests to pass mocks without
    touching real APIs.
    """

    def __init__(
        self,
        st_store: SupabaseSTStore,
        profile_store: SupabaseProfileStore,
        rag_store: QdrantRAGStore,
        router: Optional[IntentRouter] = None,
        catalog_agent: Optional[CatalogAgent] = None,
        logistics_agent: Optional[LogisticsAgent] = None,
        delivery_service: Optional[DeliveryZoneService] = None,
        model: str = "llama-3.3-70b-versatile",
        max_history_turns: int = 10,
        st_max_turns: int = 20,
        st_ttl_seconds: int = 3600,
    ) -> None:
        self._st = st_store
        self._profiles = profile_store
        self._rag = rag_store
        self.model = model
        self.max_history_turns = max_history_turns
        self.st_max_turns = st_max_turns
        self.st_ttl_seconds = st_ttl_seconds

        # Lazy-init sub-components
        self._router = router
        self._catalog = catalog_agent
        self._delivery_svc = delivery_service
        self._logistics = logistics_agent

        # Groq client for inline handlers (preference_update, chitchat)
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in the environment.")
        self._groq = Groq(api_key=api_key)

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    @observe(name="orchestrator_chat")
    def chat(
        self,
        user_id: str,
        session_id: str,
        message: str,
    ) -> OrchestratorResponse:
        """
        Process one user message and return the agent's response.

        Parameters
        ----------
        user_id:    Stable identifier for the user (e.g. UUID or email hash).
        session_id: Identifier for the current conversation session.
        message:    Raw user input text.
        """
        update_current_observation(
            input=message,
            metadata={"user_id": user_id, "session_id": session_id},
        )

        # Step 1 — load context
        history  = self._st.recent(user_id, session_id, k=self.max_history_turns)
        profiles = self._profiles.list_profiles(user_id)

        # Step 2 — classify intent
        router_result = self._get_router().classify(message, history[-3:])
        intent = router_result.intent

        logger.info(
            "[{}] intent={} confidence={:.0%} recipient={} district={}",
            session_id[:8], intent, router_result.confidence,
            router_result.recipient_hint, router_result.district_hint,
        )

        # Step 3 — dispatch
        reply, metadata = self._dispatch(
            intent, message, user_id, router_result, profiles, history
        )

        # Step 4 — persist both turns
        now = time.time()
        self._st.append(
            ConversationTurn(user_id, session_id, "user", message, now),
            max_turns=self.st_max_turns,
            ttl_seconds=self.st_ttl_seconds,
        )
        self._st.append(
            ConversationTurn(user_id, session_id, "assistant", reply, now + 0.001),
            max_turns=self.st_max_turns,
            ttl_seconds=self.st_ttl_seconds,
        )

        update_current_observation(output=reply[:300])

        return OrchestratorResponse(
            reply=reply,
            intent=intent,
            confidence=router_result.confidence,
            session_id=session_id,
            user_id=user_id,
            metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Dispatch
    # ------------------------------------------------------------------

    def _dispatch(
        self,
        intent: str,
        message: str,
        user_id: str,
        router_result: RouterResult,
        profiles: List[RecipientProfile],
        history: List[ConversationTurn],
    ) -> tuple[str, dict]:

        if intent == "search":
            return self._handle_search(message, user_id, router_result, profiles, history)

        if intent == "preference_update":
            return self._handle_preference_update(
                user_id, message, router_result, profiles
            )

        if intent == "logistics_check":
            response = self._get_logistics().check(
                message, district_hint=router_result.district_hint
            )
            metadata = {
                "district": response.district,
                "feasible": response.feasibility.feasible
                if response.feasibility else None,
            }
            return response.reply, metadata

        # chitchat fallback
        return self._handle_chitchat(message, profiles), {}

    # ------------------------------------------------------------------
    # Search handler
    # ------------------------------------------------------------------

    def _handle_search(
        self,
        message: str,
        user_id: str,
        router_result: RouterResult,
        profiles: List[RecipientProfile],
        history: List[ConversationTurn],
    ) -> tuple[str, dict]:
        # Resolve recipient profile
        profile: Optional[RecipientProfile] = None

        if router_result.recipient_hint:
            profile = self._profiles.get(user_id, router_result.recipient_hint)

        # Fall back to sole profile if user has exactly one and gave no hint
        if profile is None and len(profiles) == 1:
            profile = profiles[0]

        response = self._get_catalog().recommend(message, profile, history)
        metadata = {
            "products_retrieved":   len(response.products_shown),
            "query_enriched":       response.query_used,
            "recipient":            response.recipient_name,
            "reflection_triggered": response.reflection_triggered,
            "violations_found":     response.violations_found,
        }
        return response.reply, metadata

    # ------------------------------------------------------------------
    # Preference update handler (inline — no separate agent class)
    # ------------------------------------------------------------------

    def _handle_preference_update(
        self,
        user_id: str,
        message: str,
        router_result: RouterResult,
        existing_profiles: List[RecipientProfile],
    ) -> tuple[str, dict]:

        recipient_name = router_result.recipient_hint
        if not recipient_name:
            return (
                "I'd love to remember that! Who is this for? "
                "(e.g. 'my wife', 'dad', 'friend Sarah')",
                {},
            )

        # Extract structured preferences via LLM
        extracted = self._extract_preferences(message)

        # Fetch or create profile
        profile = self._profiles.get(user_id, recipient_name)
        is_new = profile is None

        if is_new:
            profile = RecipientProfile(
                profile_id=str(uuid.uuid4()),
                user_id=user_id,
                name=recipient_name.capitalize(),
                relationship=extracted.get("relationship"),
                preferences=extracted.get("preferences") or [],
                dislikes=extracted.get("dislikes") or [],
                budget_lkr=extracted.get("budget_lkr"),
                notes=extracted.get("notes"),
                created_at=time.time(),
                updated_at=time.time(),
            )
            self._profiles.upsert(profile)
            action = "created a new profile"
        else:
            self._profiles.update_preferences(
                user_id=user_id,
                name=recipient_name,
                preferences=extracted.get("preferences") or profile.preferences,
                dislikes=extracted.get("dislikes") or profile.dislikes,
                budget_lkr=extracted.get("budget_lkr") or profile.budget_lkr,
            )
            action = "updated the profile"

        # Build confirmation summary
        prefs = extracted.get("preferences", [])
        dislikes = extracted.get("dislikes", [])
        budget = extracted.get("budget_lkr")

        parts = []
        if prefs:
            parts.append(f"likes {', '.join(prefs)}")
        if dislikes:
            parts.append(f"dislikes {', '.join(dislikes)}")
        if budget:
            parts.append(f"budget around LKR {budget:,.0f}")

        summary = " and ".join(parts) if parts else "their updated preferences"
        name_display = recipient_name.capitalize()

        reply = (
            f"Got it! I've {action} for {name_display} — I now know they {summary}. "
            f"I'll use this to personalise every gift recommendation. "
            f"Would you like me to find something for them now?"
        )

        return reply, {
            "recipient": name_display,
            "action": action,
            "is_new_profile": is_new,
        }

    def _extract_preferences(self, message: str) -> dict:
        """Call LLM to extract structured preference fields from message."""
        system, user = build_preference_extract_prompt(message)
        try:
            response = self._groq.chat.completions.create(
                model=self.model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=200,
            )
            raw = response.choices[0].message.content or "{}"
            return json.loads(raw)
        except Exception as exc:
            logger.warning("Preference extraction failed: {}", exc)
            return {}

    # ------------------------------------------------------------------
    # Chitchat handler (inline)
    # ------------------------------------------------------------------

    def _handle_chitchat(
        self,
        message: str,
        profiles: List[RecipientProfile],
    ) -> str:
        profiles_block = format_recipient_profiles(profiles)
        system, user = build_chitchat_prompt(message, profiles_block)
        try:
            response = self._groq.chat.completions.create(
                model=self.model,
                temperature=0.7,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=150,
            )
            return response.choices[0].message.content or _default_greeting()
        except Exception as exc:
            logger.warning("Chitchat LLM call failed: {}", exc)
            return _default_greeting()

    # ------------------------------------------------------------------
    # Lazy sub-component initialisation
    # ------------------------------------------------------------------

    def _get_router(self) -> IntentRouter:
        if self._router is None:
            self._router = IntentRouter(model=self.model)
        return self._router

    def _get_catalog(self) -> CatalogAgent:
        if self._catalog is None:
            self._catalog = CatalogAgent(rag_store=self._rag, model=self.model)
        return self._catalog

    def _get_logistics(self) -> LogisticsAgent:
        if self._logistics is None:
            svc = self._delivery_svc or DeliveryZoneService()
            self._logistics = LogisticsAgent(delivery_service=svc, model=self.model)
        return self._logistics


def _default_greeting() -> str:
    return (
        "Hi! I'm your gift concierge for kapruka.com. "
        "I can find the perfect gift for any occasion, remember your recipients' "
        "preferences, and check delivery across all Sri Lankan districts. "
        "What can I help you with today?"
    )
