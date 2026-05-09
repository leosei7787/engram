"""
Retrieval-Induced Strengthening (RIS).

Every time a memory object or graph edge is activated in a response,
it gets a weight boost — mirroring hippocampal reconsolidation.
"""
from .schemas import _now
from .salience import update_retrieval_modifier


RIS_NODE_BOOST    = 0.03
RIS_EDGE_BOOST    = 0.02
RIS_MAX_CUMULATIVE = 0.25


def apply_ris(
    graph: dict,
    activated_nodes: dict,   # {entity_id: activation_strength}
    activated_edge_sigs: list = None,  # ["from__type__to", ...]
    *,
    response_was_used: bool = True,
) -> dict:
    """
    Apply RIS to nodes and edges that were activated in a response.
    Mutates graph in place. Returns stats dict.
    """
    if not response_was_used:
        return {"skipped": True, "reason": "response_aborted"}

    stats = {"nodes_boosted": 0, "edges_boosted": 0}
    entities = graph.get("entities", {})
    now_iso = _now()

    # ── Nodes ────────────────────────────────────────────────────────────
    for node_id, strength in (activated_nodes or {}).items():
        ent = entities.get(node_id)
        if not ent:
            continue
        boost = RIS_NODE_BOOST * float(strength)
        prev = float(ent.get("ris_accumulated", 0.0))
        ent["ris_accumulated"] = min(RIS_MAX_CUMULATIVE, prev + boost)
        ent["activation_count"] = int(ent.get("activation_count", 0)) + 1
        ent["last_activated"] = now_iso
        # Update salience retrieved_n_times modifier
        if "salience" in ent:
            ent["salience"] = update_retrieval_modifier(
                ent["salience"], ent["activation_count"]
            )
        stats["nodes_boosted"] += 1

    # ── Edges ────────────────────────────────────────────────────────────
    if activated_edge_sigs:
        edge_sigs = set(activated_edge_sigs)
        for ed in graph.get("edges", []):
            sig = f"{ed.get('from')}__{ed.get('type')}__{ed.get('to')}"
            if sig not in edge_sigs:
                continue
            # Use both endpoint strengths (mean) as activation
            sa = float(activated_nodes.get(ed.get("from"), 0.0))
            sb = float(activated_nodes.get(ed.get("to"), 0.0))
            edge_act = max(sa, sb)
            boost = RIS_EDGE_BOOST * edge_act
            prev = float(ed.get("ris_accumulated", 0.0))
            ed["ris_accumulated"] = min(RIS_MAX_CUMULATIVE, prev + boost)
            ed["activation_count"] = int(ed.get("activation_count", 0)) + 1
            ed["last_activated"] = now_iso
            stats["edges_boosted"] += 1

    return stats


def edge_signatures_from_activated(activated_nodes: dict, graph: dict) -> list:
    """Find edges where both endpoints are activated."""
    sigs = []
    nset = set(activated_nodes.keys())
    for ed in graph.get("edges", []):
        if ed.get("from") in nset and ed.get("to") in nset:
            sigs.append(f"{ed.get('from')}__{ed.get('type')}__{ed.get('to')}")
    return sigs
