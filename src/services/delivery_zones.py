"""
Sri Lankan delivery zone service for kapruka.com.

Pure rule-based static lookup — no LLM required.
Provides delivery feasibility, estimated days, and surcharge for all
25 Sri Lankan districts.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional


@dataclass
class DeliveryZone:
    district:      str
    province:      str
    zone_type:     str    # "same_day" | "next_day" | "two_day" | "extended"
    delivery_days: int    # minimum calendar days
    surcharge_lkr: float  # extra delivery fee (0 for Colombo metro)
    available:     bool   # False = not serviceable


@dataclass
class DeliveryFeasibility:
    district:      str
    feasible:      bool
    zone_type:     str
    delivery_days: int
    surcharge_lkr: float
    notes:         str    # human-readable explanation


# ---------------------------------------------------------------------------
# Static district data — all 25 Sri Lankan districts
# ---------------------------------------------------------------------------

_ZONES: List[DeliveryZone] = [
    # ── Same Day (Western Province metro) ──────────────────────────────────
    DeliveryZone("Colombo",      "Western",        "same_day",  0, 0.0,   True),
    DeliveryZone("Gampaha",      "Western",        "same_day",  0, 0.0,   True),
    DeliveryZone("Kalutara",     "Western",        "same_day",  1, 0.0,   True),

    # ── Next Day ───────────────────────────────────────────────────────────
    DeliveryZone("Kandy",        "Central",        "next_day",  1, 250.0, True),
    DeliveryZone("Galle",        "Southern",       "next_day",  1, 250.0, True),
    DeliveryZone("Matara",       "Southern",       "next_day",  1, 250.0, True),
    DeliveryZone("Kurunegala",   "North Western",  "next_day",  1, 250.0, True),
    DeliveryZone("Ratnapura",    "Sabaragamuwa",   "next_day",  1, 250.0, True),
    DeliveryZone("Kegalle",      "Sabaragamuwa",   "next_day",  1, 250.0, True),
    DeliveryZone("Badulla",      "Uva",            "next_day",  2, 250.0, True),
    DeliveryZone("Nuwara Eliya", "Central",        "next_day",  2, 250.0, True),

    # ── Two Day ────────────────────────────────────────────────────────────
    DeliveryZone("Anuradhapura", "North Central",  "two_day",   2, 400.0, True),
    DeliveryZone("Polonnaruwa",  "North Central",  "two_day",   2, 400.0, True),
    DeliveryZone("Matale",       "Central",        "two_day",   2, 400.0, True),
    DeliveryZone("Ampara",       "Eastern",        "two_day",   3, 400.0, True),
    DeliveryZone("Hambantota",   "Southern",       "two_day",   2, 400.0, True),
    DeliveryZone("Monaragala",   "Uva",            "two_day",   3, 400.0, True),
    DeliveryZone("Puttalam",     "North Western",  "two_day",   2, 400.0, True),
    DeliveryZone("Trincomalee",  "Eastern",        "two_day",   2, 400.0, True),

    # ── Extended ───────────────────────────────────────────────────────────
    DeliveryZone("Jaffna",       "Northern",       "extended",  3, 600.0, True),
    DeliveryZone("Vavuniya",     "Northern",       "extended",  3, 600.0, True),
    DeliveryZone("Mannar",       "Northern",       "extended",  4, 600.0, True),
    DeliveryZone("Mullaitivu",   "Northern",       "extended",  5, 600.0, True),
    DeliveryZone("Kilinochchi",  "Northern",       "extended",  4, 600.0, True),
    DeliveryZone("Batticaloa",   "Eastern",        "extended",  3, 600.0, True),
]

# Build lookup dict: normalised name → DeliveryZone
_DISTRICT_MAP: Dict[str, DeliveryZone] = {
    z.district.lower(): z for z in _ZONES
}

# Common aliases and sub-city names → canonical district
_ALIASES: Dict[str, str] = {
    "colombo 1":  "colombo", "colombo 2":  "colombo", "colombo 3":  "colombo",
    "colombo 4":  "colombo", "colombo 5":  "colombo", "colombo 6":  "colombo",
    "colombo 7":  "colombo", "colombo 8":  "colombo", "colombo 9":  "colombo",
    "colombo 10": "colombo", "colombo 11": "colombo", "colombo 12": "colombo",
    "colombo 13": "colombo", "colombo 14": "colombo", "colombo 15": "colombo",
    "nugegoda":   "colombo", "dehiwala":   "colombo", "mount lavinia": "colombo",
    "kotte":      "colombo", "sri jayawardenepura": "colombo",
    "negombo":    "gampaha", "ja-ela":     "gampaha", "wattala":    "gampaha",
    "kandy city": "kandy",   "katugastota":"kandy",
    "galle city": "galle",   "unawatuna":  "galle",
    "nuwara eliya city": "nuwara eliya", "nuwaraeliya": "nuwara eliya",
    "a'pura":     "anuradhapura",
    "trinco":     "trincomalee",
    "jaffna city":"jaffna",
    "batticaloa city": "batticaloa",
}

_ZONE_NOTES: Dict[str, str] = {
    "same_day":  "Same-day delivery available if ordered before 11 AM.",
    "next_day":  "Next-day delivery. Order before midnight for delivery tomorrow.",
    "two_day":   "2–3 day delivery. Order in advance for time-sensitive occasions.",
    "extended":  "3–5 day delivery to this region. Please order well in advance.",
}


# ---------------------------------------------------------------------------
# Service class
# ---------------------------------------------------------------------------

class DeliveryZoneService:
    """
    Rule-based delivery feasibility checker for Sri Lankan districts.

    Lookup is case-insensitive and handles common city aliases
    (e.g. 'Colombo 7', 'Negombo', 'Kandy City').
    """

    def check_feasibility(self, district: str) -> DeliveryFeasibility:
        """
        Return delivery feasibility for a given district name or alias.

        Never raises — returns a not-found feasibility if the district
        cannot be resolved, so callers always get a usable result.
        """
        zone = self._resolve(district)

        if zone is None:
            return DeliveryFeasibility(
                district=district,
                feasible=False,
                zone_type="unknown",
                delivery_days=0,
                surcharge_lkr=0.0,
                notes=(
                    f"'{district}' was not recognised as a Sri Lankan district. "
                    "Please check the spelling or try a nearby major city."
                ),
            )

        note = _ZONE_NOTES.get(zone.zone_type, "")
        if zone.surcharge_lkr > 0:
            note += f" A delivery surcharge of LKR {zone.surcharge_lkr:.0f} applies."

        return DeliveryFeasibility(
            district=zone.district,
            feasible=zone.available,
            zone_type=zone.zone_type,
            delivery_days=zone.delivery_days,
            surcharge_lkr=zone.surcharge_lkr,
            notes=note,
        )

    def get_zone(self, district: str) -> Optional[DeliveryZone]:
        """Return the raw DeliveryZone for a district, or None if not found."""
        return self._resolve(district)

    def list_districts(self) -> List[str]:
        """Return all canonical district names (title-cased)."""
        return [z.district for z in _ZONES]

    def get_districts_by_zone(self, zone_type: str) -> List[str]:
        """Return district names for a given zone type."""
        return [z.district for z in _ZONES if z.zone_type == zone_type]

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _resolve(self, raw: str) -> Optional[DeliveryZone]:
        """Normalise input and resolve to a DeliveryZone."""
        key = raw.strip().lower()

        # 1. Exact match
        if key in _DISTRICT_MAP:
            return _DISTRICT_MAP[key]

        # 2. Alias match
        if key in _ALIASES:
            return _DISTRICT_MAP.get(_ALIASES[key])

        # 3. Prefix match (handles "Colombo South", "Kandy district", etc.)
        for name, zone in _DISTRICT_MAP.items():
            if key.startswith(name) or name.startswith(key):
                return zone

        return None
