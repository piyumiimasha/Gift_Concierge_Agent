"""
Intent Router — classifies user messages into one of four intents.

Uses Groq (llama-3.3-70b-versatile) with JSON mode for deterministic,
structured output. Falls back to "chitchat" on any error so the
orchestrator always receives a usable result.

Intents
-------
search             → CatalogAgent
preference_update  → inline preference handler in orchestrator
logistics_check    → LogisticsAgent
chitchat           → inline chitchat handler in orchestrator
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from dotenv import load_dotenv
from groq import Groq
from loguru import logger

from agents.prompts.agent_prompts import build_router_prompt
from infastructure.observability import observe, update_current_observation
from memory.schemas import ConversationTurn

load_dotenv()

IntentType = Literal["search", "preference_update", "logistics_check", "chitchat"]

_SAFE_FALLBACK: IntentType = "chitchat"
_KNOWN_INTENTS = {"search", "preference_update", "logistics_check", "chitchat"}


@dataclass
class RouterResult:
    intent:         IntentType
    confidence:     float
    recipient_hint: Optional[str]   # e.g. "wife" — used for profile lookup
    district_hint:  Optional[str]   # e.g. "Jaffna" — passed to logistics agent
    reasoning:      str
    raw_response:   str = field(default="", repr=False)


class IntentRouter:
    """
    Classifies a user message into one of four gift-concierge intents.

    Uses Groq with JSON mode — no regex parsing, no ambiguity.
    Temperature=0 for fully deterministic classification.
    """

    def __init__(
        self,
        model: str = "llama-3.3-70b-versatile",
        temperature: float = 0.0,
    ) -> None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in the environment.")
        self._client = Groq(api_key=api_key)
        self.model = model
        self.temperature = temperature

    @observe(name="intent_router")
    def classify(
        self,
        message: str,
        recent_turns: List[ConversationTurn],
    ) -> RouterResult:
        """
        Classify a user message into an intent category.

        Parameters
        ----------
        message:
            The raw user message to classify.
        recent_turns:
            Last few conversation turns for context (helps with follow-ups
            like "what about Kandy?" following a logistics discussion).

        Returns
        -------
        RouterResult — always returns a result, never raises.
        """
        recent_context = _format_recent_turns(recent_turns[-3:])
        system, user = build_router_prompt(message, recent_context)

        update_current_observation(
            input=message,
            metadata={"model": self.model, "context_turns": len(recent_turns)},
        )

        try:
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                response_format={"type": "json_object"},
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
            )
            raw = response.choices[0].message.content or ""
            result = self._parse(raw, message)

        except Exception as exc:
            logger.error("IntentRouter.classify failed: {}", exc)
            result = RouterResult(
                intent=_SAFE_FALLBACK,
                confidence=0.0,
                recipient_hint=None,
                district_hint=None,
                reasoning=f"Classification error: {exc}",
            )

        update_current_observation(
            output=result.intent,
            metadata={"confidence": result.confidence, "reasoning": result.reasoning},
        )
        logger.info(
            "Intent: {} ({:.0%}) — {}",
            result.intent, result.confidence, result.reasoning,
        )
        return result

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    @staticmethod
    def _parse(raw: str, original_message: str) -> RouterResult:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Router returned non-JSON: {}", raw[:200])
            return RouterResult(
                intent=_SAFE_FALLBACK,
                confidence=0.0,
                recipient_hint=None,
                district_hint=None,
                reasoning="JSON parse failed",
                raw_response=raw,
            )

        intent = data.get("intent", _SAFE_FALLBACK)
        if intent not in _KNOWN_INTENTS:
            logger.warning("Unknown intent '{}', defaulting to chitchat", intent)
            intent = _SAFE_FALLBACK

        return RouterResult(
            intent=intent,
            confidence=float(data.get("confidence", 0.5)),
            recipient_hint=data.get("recipient_hint") or None,
            district_hint=data.get("district_hint") or None,
            reasoning=data.get("reasoning", ""),
            raw_response=raw,
        )


def _format_recent_turns(turns: List[ConversationTurn]) -> str:
    if not turns:
        return "(no prior context)"
    lines = []
    for t in turns:
        role = t.role.capitalize()
        content = t.content[:150] + "…" if len(t.content) > 150 else t.content
        lines.append(f"[{role}] {content}")
    return "\n".join(lines)
