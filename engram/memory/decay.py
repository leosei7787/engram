"""
Salience-Weighted Forgetting — the full v3 decay formula.

weight = (base_strength + ris_accumulated)
       × recency_factor (Ebbinghaus, modulated by salience)
       × frequency_factor (logarithmic — repeated use boosts retention)
       × source_count_bonus
       × source_credibility_multiplier
"""
import math
import time
from .schemas import TIER_FLOORS, TIER_DECAY_BASE, _now
from .salience import effective_decay_rate


def _parse_iso_to_ts(iso_str: str) -> float:
    if not iso_str:
        return time.time()
    try:
        return time.mktime(time.strptime(iso_str[:19], "%Y-%m-%dT%H:%M:%S"))
    except Exception:
        return time.time()


def compute_edge_weight(edge: dict, *, max_activations_in_graph: int = 100,
                        now_ts: float = None) -> float:
    """
    Full v3 weight formula.
    """
    if now_ts is None:
        now_ts = time.time()

    last_activated = _parse_iso_to_ts(edge.get("last_activated", _now()))
    weeks_since = max(0.0, (now_ts - last_activated) / (7 * 86400))

    tier = edge.get("tier", "semantic")
    base_decay = float(edge.get("decay_rate") or TIER_DECAY_BASE.get(tier, 0.02))
    salience_computed = float((edge.get("salience") or {}).get("computed", 0.5))
    priority_floor    = bool(edge.get("priority_floor", False))
    decay = effective_decay_rate(base_decay, salience_computed, priority_floor=priority_floor)

    recency_factor = math.exp(-decay * weeks_since)

    activation_count = max(1, int(edge.get("activation_count", 1)))
    frequency_factor = math.log(1 + activation_count) / math.log(1 + max(max_activations_in_graph, 1))

    source_count = max(1, len(edge.get("sources") or []))
    source_count_bonus = min(1.0, 0.85 + (source_count - 1) * 0.05)

    base_strength = float(edge.get("base_strength", edge.get("weight", 0.5)))
    ris = float(edge.get("ris_accumulated", 0.0))
    src_cred = float(edge.get("source_credibility", 0.5))

    weight = (base_strength + ris) * recency_factor * frequency_factor * source_count_bonus * src_cred

    floor = TIER_FLOORS.get(tier, 0.0)
    return max(floor, min(1.0, weight))


def compute_entity_weight(entity: dict, now_ts: float = None) -> float:
    """Same logic for entities (no source_credibility — use 1.0)."""
    if now_ts is None:
        now_ts = time.time()
    last_activated = _parse_iso_to_ts(entity.get("last_activated", _now()))
    weeks_since = max(0.0, (now_ts - last_activated) / (7 * 86400))

    tier = entity.get("tier", "semantic")
    base_decay = TIER_DECAY_BASE.get(tier, 0.02)
    salience_computed = float((entity.get("salience") or {}).get("computed", 0.5))
    priority_floor    = bool(entity.get("priority_floor", False))
    decay = effective_decay_rate(base_decay, salience_computed, priority_floor=priority_floor)
    recency_factor = math.exp(-decay * weeks_since)

    activation_count = max(1, int(entity.get("activation_count", 1)))
    frequency_factor = math.log(1 + activation_count) / math.log(1 + 100)

    base_strength = float(entity.get("salience", {}).get("base", 0.5))
    ris = float(entity.get("ris_accumulated", 0.0))
    weight = (base_strength + ris) * recency_factor * frequency_factor

    floor = TIER_FLOORS.get(tier, 0.0)
    return max(floor, min(1.0, weight))


def recompute_weights(graph: dict) -> dict:
    """Recompute and update edge.weight + entity.weight for the whole graph."""
    edges = graph.get("edges", [])
    if not edges:
        return {"edges_updated": 0, "edges_archived": 0}

    max_act = max((int(e.get("activation_count", 0)) for e in edges), default=1)
    archived = 0
    updated = 0
    now_ts = time.time()

    keep = []
    for ed in edges:
        w = compute_edge_weight(ed, max_activations_in_graph=max_act, now_ts=now_ts)
        ed["weight"] = round(w, 4)
        updated += 1
        if w < 0.05 and ed.get("tier") != "crystallised":
            archived += 1
            continue
        keep.append(ed)
    graph["edges"] = keep

    for eid, ent in graph.get("entities", {}).items():
        ent["weight"] = round(compute_entity_weight(ent, now_ts=now_ts), 4)

    return {"edges_updated": updated, "edges_archived": archived}
