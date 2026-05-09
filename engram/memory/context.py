"""
v3 Context Assembly — implements §7.3 of PRD.

Replaces v2's "fetch + concatenate" with:
  - Always-load crystallised + knowledge base + thread tail + open questions
  - Spreading activation WITH priming injection
  - Optional reconstructive synthesis based on query type
  - Apply RIS post-response
"""
from pathlib import Path
from typing import Callable, Optional
from .schemas import TIER_CRYSTALLISED
from .synthesis import classify_query, needs_synthesis, synthesise, rank_by_relevance_and_salience
from .priming import get_or_create
from .open_questions import load_open_questions
from .contradictions import load_contradictions
from .communities import load_communities, expand_activation_with_communities


def list_crystallised_for_context(graph: dict, max_items: int = 20) -> list:
    """Top crystallised entities + their primary edges, formatted for prompt."""
    out = []
    crystallised_entities = [
        (eid, ent) for eid, ent in graph.get("entities", {}).items()
        if ent.get("tier") == TIER_CRYSTALLISED
    ]
    crystallised_entities.sort(
        key=lambda x: -float((x[1].get("salience") or {}).get("computed", 0.5))
    )
    for eid, ent in crystallised_entities[:max_items]:
        out.append({
            "id":     eid,
            "name":   ent.get("name"),
            "type":   ent.get("type"),
            "summary": (ent.get("description") or "")[:300],
            "sources": (ent.get("sources") or [])[:3],
        })
    return out


def format_crystallised_block(crystallised: list) -> str:
    if not crystallised:
        return ""
    lines = ["## ◆ Crystallised Memory (always-loaded, never decays)"]
    for c in crystallised:
        line = f"- ◆ **{c['name']}** ({c['type']})"
        if c.get("summary"):
            line += f": {c['summary']}"
        lines.append(line)
    return "\n".join(lines)


def format_open_questions_block(questions: list, max_items: int = 5) -> str:
    if not questions:
        return ""
    open_only = [q for q in questions if q.get("status") == "open"]
    if not open_only:
        return ""
    open_only.sort(key=lambda q: {"high": 0, "medium": 1, "low": 2}.get(q.get("priority", "medium"), 1))
    lines = ["## 🔍 Open Questions"]
    for q in open_only[:max_items]:
        prio = q.get("priority", "M")[:1].upper()
        lines.append(f"- [{prio}] {q['text']}")
    return "\n".join(lines)


def format_contradictions_block(contradictions: list, query_entity_ids: set, max_items: int = 5) -> str:
    pending = [c for c in contradictions if c.get("status") == "unresolved"]
    if not pending:
        return ""
    # Filter to those touching query entities
    relevant = [
        c for c in pending
        if (c.get("claim_A", {}).get("statement", "") + c.get("claim_B", {}).get("statement", ""))
    ]
    if not relevant:
        relevant = pending
    relevant.sort(key=lambda c: {"high": 0, "medium": 1, "low": 2}.get(c.get("severity", "low"), 2))
    lines = ["## ⚡ Active Contradictions"]
    for c in relevant[:max_items]:
        lines.append(
            f"- [{c.get('severity','?').upper()}] "
            f"A: \"{c['claim_A']['statement']}\" "
            f"vs B: \"{c['claim_B']['statement']}\""
        )
    return "\n".join(lines)


def assemble_context_v3(
    *,
    query: str,
    session_id: str,
    graph: dict,
    spreading_activation: Callable,         # (seed_ids, graph) -> {eid: strength}
    seed_finder: Callable,                  # (query, graph) -> {eid: strength}
    file_loader: Callable,                  # (relpath) -> str
    graph_context_block: Callable,          # (activated, graph, seeds) -> str
    open_questions_path: Path,
    contradictions_path: Path,
    communities_path: Path,
    ollama_complete: Optional[Callable] = None,
    synthesis_model: Optional[str] = None,
    yield_for_chat: Optional[Callable] = None,
    use_communities: bool = True,
    use_synthesis: bool = True,
) -> dict:
    """
    Returns a dict with keys: blocks, activated, query_type, files_used.
    """
    query_type = classify_query(query)

    # 1. Seed nodes from query keywords / file matches
    seeds = seed_finder(query, graph) or {}

    # 2. Priming injection
    pv = get_or_create(session_id)
    primed_seeds = pv.inject(seeds)

    # 3. Spreading activation
    activated = spreading_activation(set(primed_seeds.keys()), graph) or {}

    # 4. Community-level expansion
    if use_communities:
        try:
            communities = load_communities(communities_path)
            if communities:
                activated = expand_activation_with_communities(activated, communities)
        except Exception:
            pass

    # 5. Update priming for next query
    pv.update(activated)

    # 6. Always-loaded blocks
    blocks = []

    crystallised = list_crystallised_for_context(graph)
    cb = format_crystallised_block(crystallised)
    if cb:
        blocks.append(cb)

    open_qs = load_open_questions(open_questions_path)
    oqb = format_open_questions_block(open_qs)
    if oqb:
        blocks.append(oqb)

    contradictions = load_contradictions(contradictions_path)
    cdb = format_contradictions_block(contradictions, set(activated.keys()))
    if cdb:
        blocks.append(cdb)

    # 7. Graph context block (entity relationships)
    try:
        gcb = graph_context_block(activated, graph, set(seeds.keys()))
        if gcb:
            blocks.append(gcb)
    except Exception:
        pass

    files_used = []

    # 8. Synthesis vs direct fetch
    if use_synthesis and needs_synthesis(query_type) and ollama_complete and synthesis_model:
        ranked = rank_by_relevance_and_salience(
            activated, graph.get("entities", {}), query,
            file_loader=file_loader, max_files=8,
        )
        for r in ranked:
            files_used.append(r["source"])

        briefing = synthesise(
            query=query,
            ranked_memory_objects=ranked,
            activated_subgraph=blocks[-1] if blocks else "",
            open_questions=open_qs,
            contradictions=[c for c in contradictions if c.get("status") == "unresolved"],
            ollama_complete=ollama_complete,
            model=synthesis_model,
            yield_for_chat=yield_for_chat,
        )
        if briefing and len(briefing) > 100:
            blocks.append(f"## 🧭 Reconstructed Briefing (synthesised from {len(ranked)} sources)\n\n{briefing}")
    else:
        # Direct fetch — top files by activation × salience
        ranked = rank_by_relevance_and_salience(
            activated, graph.get("entities", {}), query,
            file_loader=None, max_files=5,
        )
        for r in ranked:
            files_used.append(r["source"])

    return {
        "blocks":     blocks,
        "activated":  activated,
        "query_type": query_type,
        "files_used": files_used,
        "primed_seeds_count": len(primed_seeds),
    }
