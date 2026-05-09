"""
v3 schemas — extensions to graph entities and edges, plus new node types.

v2 graph.json structure:
  {"entities": {eid: {...}}, "edges": [{...}]}

v3 adds these fields to each entity:
  - tier:                 "working" | "episodic" | "semantic" | "crystallised"
  - salience:             {base, modifiers, computed}
  - ris_accumulated:      float (0.0 - 0.25)
  - activation_count:     int
  - last_activated:       ISO timestamp
  - crystallised_at:      ISO timestamp (if crystallised)

v3 adds these fields to each edge:
  - tier:                 (same as above, inherited from min(from_tier, to_tier))
  - salience:             {base, modifiers, computed}
  - ris_accumulated:      float
  - source_credibility:   float (from SOURCE_CREDIBILITY map)
  - last_activated:       ISO
  - activation_count:     int

New top-level files:
  - open_questions.json:   list of OpenQuestion nodes
  - contradictions.json:   list of Contradiction nodes
  - communities.json:      {community_id: {label, icon, members: [eid]}}
  - priming/session_priming.json: {session_id: {nodes: {eid: strength}}}
"""

from dataclasses import dataclass, field, asdict
from typing import Optional
import time


# ─── Tiers ──────────────────────────────────────────────────────────────────
TIER_WORKING       = "working"
TIER_EPISODIC      = "episodic"
TIER_SEMANTIC      = "semantic"
TIER_CRYSTALLISED  = "crystallised"

TIERS = (TIER_WORKING, TIER_EPISODIC, TIER_SEMANTIC, TIER_CRYSTALLISED)

TIER_FLOORS = {
    TIER_WORKING:      0.0,
    TIER_EPISODIC:     0.0,
    TIER_SEMANTIC:     0.2,
    TIER_CRYSTALLISED: 0.8,
}

# Default base decay rates per week, modulated by salience
TIER_DECAY_BASE = {
    TIER_WORKING:      0.4,    # decays completely in 48h-ish
    TIER_EPISODIC:     0.035,  # mid-range of 0.020-0.050
    TIER_SEMANTIC:     0.006,  # mid-range of 0.003-0.010
    TIER_CRYSTALLISED: 0.001,
}


# ─── Source credibility ─────────────────────────────────────────────────────
SOURCE_CREDIBILITY = {
    # Tier 1: Primary — user's own voice
    "user_statement":        1.00,
    "user_upload":           0.95,
    # Tier 2: Direct — internal documents
    "internal_deck":         0.85,
    "internal_email":        0.80,
    "internal_meeting":      0.78,
    "internal_doc":          0.75,
    # Tier 3: Partner
    "partner_doc":           0.70,
    # Tier 4: Secondary
    "analyst_report":        0.60,
    "news_article":          0.45,
    # Tier 5: Tertiary
    "inferred_from_context": 0.30,
    "web_fetch":             0.25,
    "unknown":               0.50,
}

TIME_BOUND_RELATIONS = {
    "reports_to", "owns", "leads", "manages", "located_at",
    "current_status", "current_phase", "status", "deadline",
}


# ─── Dataclasses ────────────────────────────────────────────────────────────
@dataclass
class Salience:
    base: float = 0.5
    modifiers: dict = field(default_factory=dict)
    computed: float = 0.5

    def to_dict(self): return asdict(self)

    @staticmethod
    def from_dict(d):
        if not d: return Salience()
        return Salience(
            base=float(d.get("base", 0.5)),
            modifiers=dict(d.get("modifiers", {})),
            computed=float(d.get("computed", d.get("base", 0.5))),
        )


@dataclass
class OpenQuestion:
    id: str
    text: str
    status: str = "open"   # open | answered | superseded | stale
    linked_entities: list = field(default_factory=list)
    created_at: str = field(default_factory=lambda: _now())
    created_from: str = ""
    answered_by: Optional[str] = None
    answered_at: Optional[str] = None
    priority: str = "medium"   # high | medium | low
    last_surfaced: Optional[str] = None

    def to_dict(self): return asdict(self)


@dataclass
class Contradiction:
    id: str
    type: str = "factual_conflict"   # factual_conflict | role_conflict | status_conflict
    severity: str = "medium"          # high | medium | low
    claim_A: dict = field(default_factory=dict)
    claim_B: dict = field(default_factory=dict)
    status: str = "unresolved"        # unresolved | resolved_A | resolved_B | superseded
    resolved_by: Optional[str] = None
    resolved_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: _now())
    related_edge_id: Optional[str] = None

    def to_dict(self): return asdict(self)


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


# ─── Helpers ────────────────────────────────────────────────────────────────
def default_entity_v3() -> dict:
    """Default v3 fields to add to a new or migrated entity."""
    return {
        "tier":              TIER_SEMANTIC,
        "salience":          Salience().to_dict(),
        "ris_accumulated":   0.0,
        "activation_count":  0,
        "last_activated":    _now(),
        "crystallised_at":   None,
    }


def default_edge_v3() -> dict:
    """Default v3 fields to add to a new or migrated edge."""
    return {
        "tier":               TIER_SEMANTIC,
        "salience":           Salience().to_dict(),
        "ris_accumulated":    0.0,
        "source_credibility": SOURCE_CREDIBILITY["unknown"],
        "last_activated":     _now(),
        "activation_count":   0,
    }
