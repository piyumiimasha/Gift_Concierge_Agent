"""
Logistics Agent — checks delivery feasibility for Sri Lankan districts.

Hybrid design:
  - Rule-based lookup (DeliveryZoneService) is the single source of truth
    for correctness — the LLM cannot hallucinate delivery promises.
  - Groq LLM narrates the pre-computed result conversationally.

The agent never decides whether delivery is feasible; it only phrases
the answer in a warm, helpful tone.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv
from groq import Groq
from loguru import logger

from agents.prompts.agent_prompts import build_logistics_prompt
from infastructure.observability import observe, update_current_observation
from services.delivery_zones import DeliveryFeasibility, DeliveryZoneService

load_dotenv()


@dataclass
class LogisticsResponse:
    reply:          str
    district:       Optional[str]
    feasibility:    Optional[DeliveryFeasibility]
    district_found: bool


class LogisticsAgent:
    """
    Answers delivery feasibility questions for Sri Lankan districts.

    Rule-based for accuracy, LLM for natural language output.
    """

    def __init__(
        self,
        delivery_service: Optional[DeliveryZoneService] = None,
        model: str = "llama-3.3-70b-versatile",
        temperature: float = 0.3,
    ) -> None:
        api_key = os.getenv("GROQ_API_KEY", "").strip()
        if not api_key:
            raise EnvironmentError("GROQ_API_KEY is not set in the environment.")
        self._client = Groq(api_key=api_key)
        self._delivery = delivery_service or DeliveryZoneService()
        self.model = model
        self.temperature = temperature

    @observe(name="logistics_agent")
    def check(
        self,
        message: str,
        district_hint: Optional[str] = None,
    ) -> LogisticsResponse:
        """
        Check delivery feasibility and return a natural-language response.

        Parameters
        ----------
        message:
            Raw user query (e.g. "Can you deliver to Jaffna by Saturday?")
        district_hint:
            District name pre-extracted by the router (may be None).

        Returns
        -------
        LogisticsResponse — always returns a result, never raises.
        """
        update_current_observation(input=message)

        # Step 1 — resolve district
        district = self._resolve_district(message, district_hint)

        if not district:
            reply = (
                "I'd be happy to check delivery for you! "
                "Could you let me know which district or city in Sri Lanka "
                "you need delivery to?"
            )
            return LogisticsResponse(
                reply=reply,
                district=None,
                feasibility=None,
                district_found=False,
            )

        # Step 2 — rule-based feasibility lookup
        feasibility = self._delivery.check_feasibility(district)

        # Step 3 — format structured data block for LLM
        feasibility_block = _format_feasibility(feasibility)

        # Step 4 — LLM narration (only writes natural language, cannot change facts)
        try:
            system, user = build_logistics_prompt(feasibility_block, message)
            response = self._client.chat.completions.create(
                model=self.model,
                temperature=self.temperature,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user",   "content": user},
                ],
                max_tokens=300,
            )
            reply = response.choices[0].message.content or feasibility.notes
        except Exception as exc:
            logger.warning("LogisticsAgent LLM call failed: {} — using fallback", exc)
            reply = _fallback_reply(feasibility)

        update_current_observation(
            output=reply,
            metadata={
                "district": feasibility.district,
                "zone_type": feasibility.zone_type,
                "feasible": feasibility.feasible,
            },
        )

        return LogisticsResponse(
            reply=reply,
            district=feasibility.district,
            feasibility=feasibility,
            district_found=True,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve_district(
        self, message: str, hint: Optional[str]
    ) -> Optional[str]:
        """
        Try to resolve a district from the router hint first, then by
        scanning the message text against the known district list.
        """
        # Try hint from router
        if hint:
            zone = self._delivery.get_zone(hint)
            if zone:
                return zone.district

        # Scan message for known district names (case-insensitive)
        msg_lower = message.lower()
        for district in self._delivery.list_districts():
            if district.lower() in msg_lower:
                return district

        return None


def _format_feasibility(f: DeliveryFeasibility) -> str:
    """Format DeliveryFeasibility into a structured block for the LLM."""
    zone_label = {
        "same_day": "Same-Day",
        "next_day": "Next-Day",
        "two_day":  "2–3 Day",
        "extended": "Extended (3–5 Day)",
    }.get(f.zone_type, f.zone_type)

    lines = [
        f"District:          {f.district}",
        f"Delivery zone:     {zone_label}",
        f"Available:         {'Yes' if f.feasible else 'No'}",
        f"Estimated days:    {f.delivery_days} day(s)",
        f"Delivery surcharge: LKR {f.surcharge_lkr:.0f}",
        f"Notes:             {f.notes}",
    ]
    return "\n".join(lines)


def _fallback_reply(f: DeliveryFeasibility) -> str:
    """Plain-text fallback when the LLM call fails."""
    if not f.feasible:
        return (
            f"Unfortunately I couldn't confirm delivery to {f.district}. "
            f"{f.notes}"
        )
    surcharge = (
        f" A delivery surcharge of LKR {f.surcharge_lkr:.0f} applies."
        if f.surcharge_lkr > 0 else ""
    )
    return (
        f"Great news — kapruka.com delivers to {f.district}! "
        f"Estimated delivery: {f.delivery_days} day(s).{surcharge} "
        f"{f.notes} Would you like me to help find a gift?"
    )
