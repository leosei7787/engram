"""
Contradiction engine.

Detects when new triples conflict with existing graph edges, classifies
severity, creates Contradiction nodes, and surfaces them in an inbox.

Detection rule:
  - Same (subject, relation) but different object → factual_conflict
  - Same (subject, object) but incompatible relation → role_conflict
"""
import json
import uuid
from pathlib import Path
from .schemas import Contradiction, _now, TIER_CRYSTALLISED, TIME_BOUND_RELATIONS


# Relations that can have only one valid target at a time.
# These are strict: a person reports to ONE manager, a thing has ONE current_status.
# Note: "leads" was removed — a person can lead multiple things.
# "manages" likewise — a manager can manage many people.
SINGLE_VALUED_RELATIONS = {
    # Org / reporting (a person has ONE current manager, role, employer)
    "reports_to",
    "current_manager",
    "current_role", "current_title", "current_position",
    "current_employer", "currently_works_at", "employed_by",
    "ceo_of", "cto_of", "cfo_of", "cpo_of", "coo_of",  # one-per-company-at-a-time
    # Location / status (one current value at a time)
    "located_at", "based_in", "headquartered_in",
    "current_status", "current_phase", "current_state",
    # Ownership / responsibility (single current owner)
    "primary_owner",
    "responsible_for_account",
    "directly_responsible", "dri",
    # Sourcing / vendor (single current chosen vendor for a deal)
    "current_vendor", "selected_vendor",
}

# Mutually exclusive relation pairs
INCOMPATIBLE_PAIRS = [
    ({"reports_to", "manages"}, "role_conflict"),
    ({"owns", "owned_by"},      "ownership_conflict"),
    ({"competes_with", "partners_with"}, "stance_conflict"),
]


def find_contradictions(
    new_triples: list,
    graph: dict,
    confidence_threshold: float = 0.7,
) -> list:
    """
    new_triples: list of dicts {from, to, type, confidence, source, props?}
    Returns list of Contradiction dicts.
    """
    out = []
    edges = graph.get("edges", [])
    entities = graph.get("entities", {})

    # Index existing edges
    by_from_rel: dict = {}
    by_pair: dict = {}
    for ed in edges:
        f, t, et = ed.get("from"), ed.get("to"), ed.get("type")
        by_from_rel.setdefault((f, et), []).append(ed)
        by_pair.setdefault((f, t), []).append(ed)

    for tr in new_triples:
        if tr.get("confidence", 1.0) < confidence_threshold:
            continue
        f = tr.get("from"); t = tr.get("to"); et = tr.get("type")
        if not (f and t and et):
            continue

        # Same (from, relation) different to → factual conflict
        if et in SINGLE_VALUED_RELATIONS:
            for ed in by_from_rel.get((f, et), []):
                if ed.get("to") != t:
                    # Skip if this triple is a duplicate of the existing edge
                    # (some pipelines re-emit the same triple)
                    out.append(_build_contradiction(
                        type="factual_conflict",
                        existing_edge=ed,
                        new_triple=tr,
                        entities=entities,
                    ))

        # Same (from, to) but incompatible relation
        for ed in by_pair.get((f, t), []):
            existing_rel = ed.get("type")
            for pair, kind in INCOMPATIBLE_PAIRS:
                if existing_rel in pair and et in pair and existing_rel != et:
                    out.append(_build_contradiction(
                        type=kind,
                        existing_edge=ed,
                        new_triple=tr,
                        entities=entities,
                    ))

    return out


def _build_contradiction(*, type: str, existing_edge: dict,
                         new_triple: dict, entities: dict) -> dict:
    severity = compute_severity(existing_edge, new_triple, entities)
    f = entities.get(existing_edge.get("from"), {}).get("name", existing_edge.get("from"))
    t_a = entities.get(existing_edge.get("to"), {}).get("name", existing_edge.get("to"))
    t_b = entities.get(new_triple.get("to"), {}).get("name", new_triple.get("to"))

    cid = f"contradiction_{uuid.uuid4().hex[:10]}"
    return Contradiction(
        id=cid,
        type=type,
        severity=severity,
        claim_A={
            "statement": f"{f} {existing_edge.get('type')} {t_a}",
            "source": (existing_edge.get("sources") or [""])[0],
            "date":   existing_edge.get("last_activated") or existing_edge.get("created_at", ""),
            "weight": existing_edge.get("weight", existing_edge.get("base_strength", 0.5)),
            "edge_id": _edge_signature(existing_edge),
        },
        claim_B={
            "statement": f"{f} {new_triple.get('type')} {t_b}",
            "source": new_triple.get("source", ""),
            "date":   _now(),
            "weight": new_triple.get("confidence", 0.7),
        },
        related_edge_id=_edge_signature(existing_edge),
    ).to_dict()


def _edge_signature(edge: dict) -> str:
    return f"{edge.get('from')}__{edge.get('type')}__{edge.get('to')}"


def compute_severity(existing_edge: dict, new_triple: dict, entities: dict) -> str:
    f_ent = entities.get(existing_edge.get("from"), {})
    t_ent = entities.get(existing_edge.get("to"), {})

    if (existing_edge.get("tier") == TIER_CRYSTALLISED
            or f_ent.get("tier") == TIER_CRYSTALLISED
            or t_ent.get("tier") == TIER_CRYSTALLISED):
        return "high"
    if existing_edge.get("type", "").lower() in {"reports_to", "owns", "leads", "decides"}:
        return "high"
    if existing_edge.get("weight", 0.5) > 0.6:
        return "medium"
    return "low"


# ─── Auto-resolution ──────────────────────────────────────────────────────
def auto_resolve(contradiction: dict, graph: dict) -> tuple:
    """
    For low-severity contradictions, apply auto-resolution rules.
    Returns (resolved, rule_name).
    """
    if contradiction.get("severity") != "low":
        return False, None

    A = contradiction["claim_A"]; B = contradiction["claim_B"]

    # Rule 1: low existing confidence + high new confidence
    if float(A.get("weight", 0.5)) < 0.3 and float(B.get("weight", 0.5)) > 0.8:
        contradiction["status"] = "resolved_B"
        contradiction["resolved_by"] = "low_existing_confidence"
        contradiction["resolved_at"] = _now()
        return True, "low_existing_confidence"

    # Rule 2: date recency for time-bound facts
    edge_type = (A.get("statement", "").split() or [""])[1] if A.get("statement") else ""
    if edge_type in TIME_BOUND_RELATIONS and B.get("date", "") > A.get("date", ""):
        contradiction["status"] = "resolved_B"
        contradiction["resolved_by"] = "date_recency"
        contradiction["resolved_at"] = _now()
        return True, "date_recency"

    return False, None


def apply_resolution(contradiction: dict, choice: str, graph: dict) -> dict:
    """
    Apply user resolution. Choices:
      'A'        — keep existing, reject new
      'B'        — replace existing with new
      'both'     — both true (different contexts), keep both edges
      'neither'  — both wrong, drop the existing edge AND don't add the new one
      'discard'  — alias for 'A' (discard new, keep existing)
    Mutates graph and contradiction.
    """
    contradiction["resolved_at"] = _now()
    contradiction["resolved_by"] = "user_approval"

    if choice == "A":
        contradiction["status"] = "resolved_A"
    elif choice == "B":
        contradiction["status"] = "resolved_B"
        # Remove the existing edge, add the new
        sig = contradiction.get("related_edge_id", "")
        graph["edges"] = [e for e in graph.get("edges", []) if _edge_signature(e) != sig]
    elif choice == "both":
        contradiction["status"] = "superseded"
    elif choice == "neither":
        contradiction["status"] = "resolved_neither"
        # Remove the existing edge — the new one was never added in the first
        # place (contradictions are detected pre-merge), so nothing else to do.
        sig = contradiction.get("related_edge_id", "")
        graph["edges"] = [e for e in graph.get("edges", []) if _edge_signature(e) != sig]
    elif choice == "discard":
        contradiction["status"] = "resolved_A"  # discard new, keep existing
    return contradiction


# ─── Persistence ──────────────────────────────────────────────────────────
def load_contradictions(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def save_contradictions(path: Path, items: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2))


def add_contradictions(path: Path, new_items: list) -> int:
    """Merge new contradictions, dedup by signature. Returns count added."""
    existing = load_contradictions(path)
    sigs = {_dedup_sig(c) for c in existing}
    added = 0
    for c in new_items:
        if _dedup_sig(c) not in sigs:
            existing.append(c)
            sigs.add(_dedup_sig(c))
            added += 1
    save_contradictions(path, existing)
    return added


def _dedup_sig(c: dict) -> str:
    A = c.get("claim_A", {}); B = c.get("claim_B", {})
    return f"{A.get('statement','')}__vs__{B.get('statement','')}"


def list_pending_contradictions(path: Path) -> list:
    items = load_contradictions(path)
    return [c for c in items if c.get("status") == "unresolved"]
