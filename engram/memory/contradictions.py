"""
Contradiction engine.

Detects when new triples conflict with existing graph edges, classifies
severity, creates Contradiction nodes, and surfaces them in an inbox.

Detection rule:
  - Same (subject, relation) but different object → factual_conflict
  - Same (subject, object) but incompatible relation → role_conflict

Rejected-claims registry:
  Once a user marks a contradiction (or its claims) as wrong, the
  (subject, relation, object) tuple is stored in MEMORY/.rejected_claims.json
  so future extractions don't re-create it. Ground truths recorded here also
  let the system reject any future triple that contradicts an established fact
  for a single-valued relation.
"""
import json
import re
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


# ─── Source-quality filter ────────────────────────────────────────────────────
# Org-structure relations should only be trusted from these source patterns.
# Triples extracted from email signatures, airline notifications, marketing
# copy, etc. were generating "Bob Smith reports_to Some Project" garbage.
AUTHORITATIVE_ORG_SOURCES = (
    "/CLAUDE.md",
    "/context/people",
    "/context/org_",
    "/context/responsibility_",
    "/context/bob_smith",
    "/context/acmecorp_product_org",
    "/decisions/",
)

ORG_RELATIONS = {
    "reports_to", "current_manager", "current_role", "current_title",
    "current_employer", "currently_works_at", "employed_by",
    "manages", "leads", "owns",
    "ceo_of", "cto_of", "cfo_of", "cpo_of", "coo_of",
}


def _is_authoritative_source(source: str) -> bool:
    if not source:
        return False
    return any(pat in source for pat in AUTHORITATIVE_ORG_SOURCES)


def _norm_obj(s: str) -> str:
    """Normalize a name/identifier for fuzzy matching against the rejection registry."""
    if not s:
        return ""
    return re.sub(r"[\s\-_.,:]+", " ", s.lower()).strip()


def find_contradictions(
    new_triples: list,
    graph: dict,
    confidence_threshold: float = 0.7,
    rejected_registry: dict | None = None,
) -> list:
    """
    new_triples: list of dicts {from, to, type, confidence, source, props?}
    rejected_registry: optional dict from .rejected_claims.json — used to skip
        already-rejected (subject, relation, object) tuples and to enforce
        ground truths (any new triple contradicting a ground truth is dropped).
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

    rejected = _index_rejections(rejected_registry) if rejected_registry else None

    for tr in new_triples:
        if tr.get("confidence", 1.0) < confidence_threshold:
            continue
        f = tr.get("from"); t = tr.get("to"); et = tr.get("type")
        if not (f and t and et):
            continue

        # ── Filter: untrusted sources for org relations ──────────────────────
        # Don't extract reports_to/manages/etc. from random emails or transcripts.
        if et in ORG_RELATIONS and not _is_authoritative_source(tr.get("source", "")):
            continue

        # ── Filter: registry rejection ───────────────────────────────────────
        if rejected is not None:
            f_name = entities.get(f, {}).get("name", f)
            t_name = entities.get(t, {}).get("name", t)
            if _is_rejected(rejected, f_name, et, t_name):
                continue
            # Ground-truth violation for single-valued relations: silently drop.
            gt_obj = _ground_truth_object(rejected, f_name, et)
            if gt_obj and _norm_obj(gt_obj) != _norm_obj(t_name):
                continue

        # Same (from, relation) different to → factual conflict
        if et in SINGLE_VALUED_RELATIONS:
            for ed in by_from_rel.get((f, et), []):
                if ed.get("to") != t:
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


# ─── Rejected-claims registry ────────────────────────────────────────────────
# File: {memory_path}/.rejected_claims.json
# Schema:
#   {
#     "rejected": [
#       {"subject": str, "relation": str, "object": str,
#        "rejected_at": iso, "source_contradiction": id}
#     ],
#     "ground_truths": [
#       {"subject": str, "relation": str, "object": str,
#        "established_at": iso, "source_contradiction": id}
#     ]
#   }
# Ground truths are only stored for SINGLE_VALUED_RELATIONS — for those,
# any future triple with same (subject, relation) but different object is
# automatically rejected.

def load_rejected_registry(path: Path) -> dict:
    if not path.exists():
        return {"rejected": [], "ground_truths": []}
    try:
        data = json.loads(path.read_text())
        data.setdefault("rejected", [])
        data.setdefault("ground_truths", [])
        return data
    except Exception:
        return {"rejected": [], "ground_truths": []}


def save_rejected_registry(path: Path, registry: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(registry, indent=2, ensure_ascii=False))


def _index_rejections(registry: dict) -> dict:
    """Build a fast-lookup index from a registry dict."""
    rejected_set: set = set()
    truths: dict = {}  # (norm_subject, relation) -> norm_object
    for r in registry.get("rejected", []):
        key = (_norm_obj(r.get("subject","")), r.get("relation",""), _norm_obj(r.get("object","")))
        rejected_set.add(key)
    for g in registry.get("ground_truths", []):
        if g.get("relation") in SINGLE_VALUED_RELATIONS:
            truths[(_norm_obj(g.get("subject","")), g.get("relation",""))] = g.get("object","")
    return {"rejected": rejected_set, "truths": truths}


def _is_rejected(idx: dict, subject: str, relation: str, obj: str) -> bool:
    return (_norm_obj(subject), relation, _norm_obj(obj)) in idx.get("rejected", set())


def _ground_truth_object(idx: dict, subject: str, relation: str) -> str | None:
    return idx.get("truths", {}).get((_norm_obj(subject), relation))


def _parse_statement(stmt: str) -> tuple[str, str, str] | None:
    """Mirror of dashboard parser — splits 'X relation Y' on known relation tokens."""
    if not stmt:
        return None
    s = stmt.strip()
    # Try longest relations first
    all_rels = SINGLE_VALUED_RELATIONS | ORG_RELATIONS | {"manages", "leads", "owns", "knows", "advises", "is_a", "part_of"}
    for rel in sorted(all_rels, key=len, reverse=True):
        for token in (f" {rel} ", f" {rel.replace('_', ' ')} "):
            idx = s.lower().find(token.lower())
            if idx > 0:
                subject = s[:idx].strip()
                obj     = s[idx + len(token):].strip()
                return (subject, rel.replace(" ", "_"), obj)
    return None


def record_resolution(
    registry_path: Path,
    contradiction: dict,
    resolution: str,
) -> int:
    """
    Update the rejected-claims registry based on a user resolution.

    Returns count of new entries added.

    resolution:
      resolved_A   — claim A correct → reject claim B; claim A is ground truth
      resolved_B   — claim B correct → reject claim A; claim B is ground truth
      both_true    — no rejection, no truth (ambiguous, both valid)
      both_false   — reject both A and B; no truth
      dismissed    — no registry change (user just doesn't want to see it)
    """
    if resolution == "dismissed" or resolution == "both_true":
        return 0

    registry = load_rejected_registry(registry_path)
    rejected_keys = {
        (_norm_obj(r.get("subject","")), r.get("relation",""), _norm_obj(r.get("object","")))
        for r in registry["rejected"]
    }
    truth_keys = {
        (_norm_obj(g.get("subject","")), g.get("relation",""))
        for g in registry["ground_truths"]
    }

    now = _now()
    cid = contradiction.get("id", "")
    added = 0

    def _add_rejection(stmt: str):
        nonlocal added
        parsed = _parse_statement(stmt)
        if not parsed:
            return
        subj, rel, obj = parsed
        key = (_norm_obj(subj), rel, _norm_obj(obj))
        if key in rejected_keys:
            return
        registry["rejected"].append({
            "subject": subj, "relation": rel, "object": obj,
            "rejected_at": now,
            "source_contradiction": cid,
        })
        rejected_keys.add(key)
        added += 1

    def _add_truth(stmt: str):
        nonlocal added
        parsed = _parse_statement(stmt)
        if not parsed:
            return
        subj, rel, obj = parsed
        if rel not in SINGLE_VALUED_RELATIONS:
            return
        key = (_norm_obj(subj), rel)
        if key in truth_keys:
            return
        registry["ground_truths"].append({
            "subject": subj, "relation": rel, "object": obj,
            "established_at": now,
            "source_contradiction": cid,
        })
        truth_keys.add(key)
        added += 1

    A_stmt = (contradiction.get("claim_A") or {}).get("statement", "")
    B_stmt = (contradiction.get("claim_B") or {}).get("statement", "")

    if resolution == "resolved_A":
        _add_truth(A_stmt)
        _add_rejection(B_stmt)
    elif resolution == "resolved_B":
        _add_truth(B_stmt)
        _add_rejection(A_stmt)
    elif resolution == "both_false":
        _add_rejection(A_stmt)
        _add_rejection(B_stmt)

    if added:
        save_rejected_registry(registry_path, registry)
    return added


def purge_rejected_edges_from_graph(graph: dict, registry: dict) -> int:
    """
    Remove edges from the graph that match rejected claims. Returns count removed.
    Mutates `graph` in place.
    """
    idx = _index_rejections(registry)
    if not idx["rejected"] and not idx["truths"]:
        return 0

    entities = graph.get("entities", {})
    edges    = graph.get("edges", [])
    keep: list = []
    removed = 0

    for ed in edges:
        f_name = entities.get(ed.get("from"), {}).get("name", ed.get("from", ""))
        t_name = entities.get(ed.get("to"),   {}).get("name", ed.get("to",   ""))
        rel    = ed.get("type", "")

        if _is_rejected(idx, f_name, rel, t_name):
            removed += 1
            continue

        # Ground truth violation: drop edges that contradict an established truth
        gt_obj = _ground_truth_object(idx, f_name, rel)
        if gt_obj and _norm_obj(gt_obj) != _norm_obj(t_name):
            removed += 1
            continue

        keep.append(ed)

    graph["edges"] = keep
    return removed
