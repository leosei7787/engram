"""
engram.retrieval.graph — Graph spreading activation
====================================================

Implements spreading activation over a weighted entity graph to surface
semantically related files and entities beyond what keyword search finds.

Algorithm:
  1. Seed nodes: entities from files found by keyword scan PLUS entities
     whose names match query tokens directly.
  2. Spread activation N hops (default 2), decaying by hop_decay per hop.
  3. Nodes below threshold are dropped.
  4. Source files of activated nodes are ranked by summed activation.
  5. A structured markdown context block is assembled for LLM injection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

from .tokenizer import query_tokens


# ─── Graph loading ────────────────────────────────────────────────────────────

def load_graph(graph_file: Path) -> dict:
    """
    Load graph.json from disk. Returns empty dict on failure.
    Expected format: {"entities": {eid: {...}}, "edges": [{...}]}
    """
    if not graph_file.exists():
        return {}
    try:
        import json
        return json.loads(graph_file.read_text(errors="ignore")) or {}
    except Exception as e:
        print(f"[graph] failed to load {graph_file}: {e}", flush=True)
        return {}


# ─── Spreading activation ─────────────────────────────────────────────────────

def spreading_activation(
    seed_entity_ids: set,
    g: dict,
    *,
    depth: int = 2,
    hop_decay: float = 0.6,
    threshold: float = 0.12,
) -> dict:
    """
    Spreading activation from seed nodes through a weighted graph.

    Start with activation=1.0 at each seed. At each hop, propagate to
    neighbours weighted by edge.weight × hop_decay. Accumulate the maximum
    activation each node receives across all paths.

    Args:
        seed_entity_ids: Set of entity IDs to start from.
        g:               Graph dict with 'entities' and 'edges'.
        depth:           Number of hops to spread.
        hop_decay:       Activation multiplier per hop (0.0–1.0).
        threshold:       Minimum activation to retain a node.

    Returns:
        {entity_id: activation_strength} for non-seed nodes above threshold.
    """
    entities: dict = g.get("entities", {})
    edges: list    = g.get("edges", [])

    # Build weighted adjacency (bidirectional traversal)
    adj: dict = {}
    for e in edges:
        frm  = e.get("from")
        to   = e.get("to")
        w    = e.get("weight", 0.6)
        if frm and to:
            adj.setdefault(frm, []).append((to,  w))
            adj.setdefault(to,  []).append((frm, w))

    activated: dict = {eid: 1.0 for eid in seed_entity_ids if eid in entities}

    for _ in range(depth):
        new_act: dict = {}
        for node_id, strength in activated.items():
            for neighbour_id, edge_weight in adj.get(node_id, []):
                if neighbour_id not in entities:
                    continue
                propagated = strength * edge_weight * hop_decay
                if propagated > threshold:
                    new_act[neighbour_id] = max(new_act.get(neighbour_id, 0), propagated)
        for nid, s in new_act.items():
            if nid not in activated or activated[nid] < s:
                activated[nid] = s

    return {eid: s for eid, s in activated.items() if eid not in seed_entity_ids}


# ─── Entity seed resolution ───────────────────────────────────────────────────

def entity_seed_ids(q_toks: set, entities: dict) -> set:
    """
    Match query tokens against entity names in the graph.

    Returns entity IDs where the token overlap is significant:
      - ≥2 query tokens in the entity name, OR
      - ≥30% of query tokens in the entity name (for short queries)

    This seeds activation from topically relevant nodes even when the right
    file isn't found by keyword scan alone.
    """
    seeds: set = set()
    for eid, ent in entities.items():
        name_toks = set(re.split(r'\W+', ent.get("name", "").lower()))
        overlap_count = len(q_toks & name_toks)
        if overlap_count >= 2:
            seeds.add(eid)
        elif overlap_count >= 1 and overlap_count / max(len(q_toks), 1) >= 0.3:
            seeds.add(eid)
    return seeds


# ─── Context block builder ────────────────────────────────────────────────────

def graph_context_block(
    activated: dict,
    g: dict,
    seed_entity_ids: set,
    *,
    high_threshold: float = 0.50,
    related_threshold: float = 0.25,
    max_high: int = 6,
    max_related: int = 4,
) -> str:
    """
    Format the spreading-activation result as a structured markdown block
    for injection into the LLM system prompt alongside memory files.

    Groups nodes into High (≥high_threshold) and Related (related_threshold–high),
    showing key edges and any insight annotations.

    Returns empty string if there's nothing to show.
    """
    entities: dict = g.get("entities", {})
    edges: list    = g.get("edges",    [])

    if not activated:
        return ""

    # Pre-index edges per entity
    out_idx: dict = {}
    in_idx:  dict = {}
    for e in edges:
        out_idx.setdefault(e.get("from"), []).append(e)
        in_idx.setdefault( e.get("to"),   []).append(e)

    def _fmt(eid: str, _strength: float) -> str:
        ent = entities.get(eid)
        if not ent:
            return ""
        name  = ent.get("name", eid)
        etype = ent.get("type", "")
        props = ent.get("props", {})

        edge_parts = []
        for e in (out_idx.get(eid, []) + in_idx.get(eid, []))[:6]:
            other_id  = e.get("to") if e.get("from") == eid else e.get("from")
            other     = entities.get(other_id, {})
            if not other:
                continue
            etype_e = e.get("type", "")
            arrow   = "→" if e.get("from") == eid else "←"
            insight = (e.get("props") or {}).get("insight", "")
            part    = f"{etype_e} {arrow} {other.get('name', '')}"
            if insight:
                part += f" [{insight}]"
            edge_parts.append(part)
            if len(edge_parts) >= 4:
                break

        props_str = " | ".join(f"{k}: {v}" for k, v in props.items() if v)[:80]
        line = f"**{name}** [{etype}]"
        if props_str:
            line += f" — {props_str}"
        if edge_parts:
            line += f"\n    {' | '.join(edge_parts)}"
        return line

    high    = sorted([(eid, s) for eid, s in activated.items() if s >= high_threshold],
                     key=lambda x: -x[1])[:max_high]
    related = sorted([(eid, s) for eid, s in activated.items()
                      if related_threshold <= s < high_threshold],
                     key=lambda x: -x[1])[:max_related]

    if not high and not related:
        return ""

    lines = ["\n## Knowledge Graph Context"]
    if high:
        lines.append("**High relevance:**")
        for eid, s in high:
            fmt = _fmt(eid, s)
            if fmt:
                lines.append(f"- {fmt}")
    if related:
        lines.append("**Also related:**")
        for eid, s in related:
            fmt = _fmt(eid, s)
            if fmt:
                lines.append(f"- {fmt}")

    return "\n".join(lines)


# ─── Source file resolution ───────────────────────────────────────────────────

def activated_to_files(
    activated: dict,
    entities: dict,
    seed_set: set,
    base_path: Path,
    *,
    max_extra: int = 3,
) -> list[str]:
    """
    Resolve activated entity nodes to source files not already in seed_set.

    Args:
        activated:  {entity_id: activation_strength} from spreading_activation()
        entities:   Graph entities dict.
        seed_set:   Set of already-loaded file paths (to avoid duplicates).
        base_path:  Base directory for resolving relative paths.
        max_extra:  Maximum number of additional files to return.

    Returns:
        List of relative file paths, ranked by summed activation.
    """
    file_score: dict = {}
    for eid, strength in activated.items():
        for src in (entities.get(eid, {}).get("sources") or []):
            if src and src not in seed_set and (base_path / src).exists():
                file_score[src] = max(file_score.get(src, 0), strength)

    return sorted(file_score, key=lambda f: -file_score[f])[:max_extra]
