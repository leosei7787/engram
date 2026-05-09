"""
Four-tier memory: working / episodic / semantic / crystallised.

Tiers are assigned at ingestion based on content type, then can be
promoted to crystallised explicitly (decision events) or demoted via
compression as content ages.
"""
import time
from .schemas import (
    TIER_WORKING, TIER_EPISODIC, TIER_SEMANTIC, TIER_CRYSTALLISED,
    TIER_DECAY_BASE, _now,
)


CRYSTALLISED_ENTITY_TYPES = {"decision", "decisionrecord", "commitment"}


def classify_tier_for_entity(entity: dict, source_path: str = "") -> str:
    """
    Initial tier classification at ingestion. Heuristic — no LLM call.
    """
    etype = (entity.get("type") or "").lower()
    s = (source_path or "").lower()

    # Decisions go straight to crystallised
    if etype in CRYSTALLISED_ENTITY_TYPES:
        return TIER_CRYSTALLISED
    if "/decisions/" in s and etype != "person":
        return TIER_CRYSTALLISED

    # Working memory: session transcripts, today's uploads
    if "/sessions/" in s:
        return TIER_WORKING
    if "/working/" in s:
        return TIER_WORKING

    # Episodic: harvested conversations, daily/* files
    if "/episodic/" in s or "/daily/" in s:
        return TIER_EPISODIC

    # Semantic: everything else (people, products, accounts, research, weekly)
    return TIER_SEMANTIC


def classify_tier_for_edge(from_tier: str, to_tier: str, edge_type: str = "") -> str:
    """
    Edge tier = min priority of endpoints, with crystallised bias for
    decision-related edges.
    """
    et = (edge_type or "").lower()
    if et in ("decided", "owns_decision", "approved", "committed_to"):
        return TIER_CRYSTALLISED

    # Use tier hierarchy: working < episodic < semantic < crystallised
    order = [TIER_WORKING, TIER_EPISODIC, TIER_SEMANTIC, TIER_CRYSTALLISED]
    a = order.index(from_tier) if from_tier in order else 2
    b = order.index(to_tier) if to_tier in order else 2
    return order[min(a, b)]


def base_decay_for_tier(tier: str) -> float:
    return TIER_DECAY_BASE.get(tier, TIER_DECAY_BASE[TIER_SEMANTIC])


def crystallise_entity(graph: dict, entity_id: str, reason: str = "manual") -> bool:
    """Promote an entity to crystallised tier. Returns True if changed."""
    ents = graph.get("entities", {})
    ent = ents.get(entity_id)
    if not ent:
        return False
    if ent.get("tier") == TIER_CRYSTALLISED:
        return False
    ent["tier"] = TIER_CRYSTALLISED
    ent["crystallised_at"] = _now()
    ent["crystallised_reason"] = reason
    # Boost salience to permanent floor
    sal = ent.get("salience") or {}
    sal["base"] = max(0.85, float(sal.get("base", 0.5)))
    sal["computed"] = max(0.85, float(sal.get("computed", 0.5)))
    ent["salience"] = sal
    return True


def crystallise_edges_touching(graph: dict, entity_id: str) -> int:
    """When an entity is crystallised, promote edges touching it as well."""
    promoted = 0
    for ed in graph.get("edges", []):
        if ed.get("from") == entity_id or ed.get("to") == entity_id:
            if ed.get("tier") != TIER_CRYSTALLISED:
                ed["tier"] = TIER_CRYSTALLISED
                promoted += 1
    return promoted


def list_crystallised_entities(graph: dict) -> list:
    return [
        {"id": eid, **ent}
        for eid, ent in graph.get("entities", {}).items()
        if ent.get("tier") == TIER_CRYSTALLISED
    ]


def working_memory_files(memory_dir, max_age_hours: int = 48) -> list:
    """Files under MEMORY/working/ younger than max_age_hours."""
    wm = memory_dir / "working"
    if not wm.exists():
        return []
    cutoff = time.time() - max_age_hours * 3600
    return [f for f in wm.rglob("*.md") if f.stat().st_mtime > cutoff]


def expire_working_memory(memory_dir, max_age_hours: int = 48) -> int:
    """Move working/* files older than 48h to episodic/. Returns count moved."""
    wm = memory_dir / "working"
    epi = memory_dir / "episodic"
    if not wm.exists():
        return 0
    epi.mkdir(exist_ok=True)
    cutoff = time.time() - max_age_hours * 3600
    moved = 0
    for f in wm.rglob("*.md"):
        if f.stat().st_mtime < cutoff:
            try:
                target = epi / f.name
                if target.exists():
                    target = epi / f"{f.stem}_{int(f.stat().st_mtime)}.md"
                f.rename(target)
                moved += 1
            except Exception:
                pass
    return moved
