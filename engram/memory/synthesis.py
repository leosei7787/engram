"""
Reconstructive Synthesis — retrieval that generates a coherent briefing
instead of concatenating raw fragments.

Mirrors the brain's reconstructive memory: when you "remember" something,
you don't replay it — you reconstruct a plausible narrative from fragments.
"""
from typing import Callable, Optional


# Query types and their retrieval modes
SYNTHESIS_QUERY_TYPES = {"decision", "strategic", "brainstorm"}
DIRECT_FETCH_QUERY_TYPES = {"quick_answer", "provenance", "writing", "data"}


SYNTHESIS_PROMPT = """You are a briefing synthesiser.

Given memory fragments and a query, produce a concise, coherent briefing —
not a concatenation of fragments, but a reconstructed account of what is known.

Query: {query}

Memory fragments:
{ranked_memory_objects}

Graph context (entity relationships):
{activated_subgraph}

Open questions linked to query entities:
{open_questions}

Known contradictions involving query entities:
{contradictions}

Rules:
- Synthesise, don't concatenate. Write as a briefing, not assembled text.
- Resolve minor inconsistencies using the more recent or higher-confidence source.
- Flag unresolved contradictions explicitly: "CONFLICT: ..."
- Surface open questions: "OPEN: ..."
- Preserve provenance: every non-trivial fact must end with (→ source_filename)
- Target length: 400-800 tokens.
- Structure: [What we know] / [Key relationships] / [Conflicts or gaps] / [Open questions]

Output: structured markdown briefing, ready for injection into the primary context."""


# ─── Query classification ─────────────────────────────────────────────────
def classify_query(query: str) -> str:
    """
    Heuristic query type classifier — fast, no LLM.
    """
    q = (query or "").lower().strip()
    if not q:
        return "quick_answer"

    # Provenance / source-finding
    if any(p in q for p in ("source", "where did", "who said", "which file", "citation")):
        return "provenance"

    # Writing/drafting
    if any(p in q for p in ("draft", "write a", "compose", "rewrite", "edit this", "respond to")):
        return "writing"

    # Data analysis
    if any(p in q for p in ("compute", "calculate", "how many", "what's the rate",
                            "spreadsheet", "table", "compare numbers")):
        return "data"

    # Strategic
    if any(p in q for p in ("strategy", "should we", "trade-off", "trade off", "compare",
                            "vs", "options", "implications", "framework", "positioning",
                            "approach", "investment", "100m arr filter")):
        return "strategic"

    # Decision
    if any(p in q for p in ("decide", "decision", "choose between", "go or no-go",
                            "recommend", "what should i", "which one")):
        return "decision"

    # Brainstorm
    if any(p in q for p in ("brainstorm", "ideas", "what if", "could we", "explore",
                            "thinking about", "open question")):
        return "brainstorm"

    return "quick_answer"


def needs_synthesis(query_type: str) -> bool:
    return query_type in SYNTHESIS_QUERY_TYPES


# ─── Synthesis builder ────────────────────────────────────────────────────
def build_synthesis_prompt(
    query: str,
    ranked_memory_objects: list,
    activated_subgraph: str,
    open_questions: list,
    contradictions: list,
) -> str:
    fragments_text = "\n\n".join(
        f"--- {m.get('source','?')} (salience={m.get('salience',0.5):.2f}) ---\n{m.get('content','')[:2000]}"
        for m in ranked_memory_objects[:8]
    )
    oq_text = "\n".join(f"- [{q.get('priority','M')}] {q['text']}" for q in open_questions[:5]) or "(none)"
    cd_text = "\n".join(
        f"- {c.get('severity','?').upper()}: A=\"{c['claim_A']['statement']}\" vs B=\"{c['claim_B']['statement']}\""
        for c in contradictions[:5]
    ) or "(none)"

    return SYNTHESIS_PROMPT.format(
        query=query,
        ranked_memory_objects=fragments_text or "(none)",
        activated_subgraph=activated_subgraph or "(none)",
        open_questions=oq_text,
        contradictions=cd_text,
    )


def synthesise(
    query: str,
    ranked_memory_objects: list,
    activated_subgraph: str,
    open_questions: list,
    contradictions: list,
    *,
    ollama_complete: Callable,
    model: str,
    yield_for_chat: Optional[Callable] = None,
) -> str:
    """
    Run synthesis via local model. Returns the briefing markdown.
    Falls back to empty string on failure.
    """
    prompt = build_synthesis_prompt(
        query, ranked_memory_objects, activated_subgraph, open_questions, contradictions
    )
    try:
        if yield_for_chat:
            yield_for_chat(max_wait=10, label="synthesis")
        return ollama_complete(model, prompt, json_mode=False)
    except Exception as e:
        return f"_(synthesis failed: {e})_"


def rank_by_relevance_and_salience(
    activated_nodes: dict,
    entities: dict,
    query: str,
    *,
    file_loader: Optional[Callable] = None,
    max_files: int = 8,
) -> list:
    """
    Rank memory objects (files referenced by activated entities) by
    relevance × salience, returning a ranked list of dicts.
    """
    file_score: dict = {}
    file_meta: dict = {}

    for node_id, activation in activated_nodes.items():
        ent = entities.get(node_id, {})
        sources = ent.get("sources") or []
        salience = float((ent.get("salience") or {}).get("computed", 0.5))
        score_per_source = float(activation) * (0.5 + 0.5 * salience)
        for src in sources:
            file_score[src] = file_score.get(src, 0) + score_per_source
            file_meta.setdefault(src, {"sources": src, "salience": salience})

    ranked = sorted(file_score.items(), key=lambda x: -x[1])[:max_files]
    out = []
    for src, score in ranked:
        meta = file_meta[src]
        meta["score"] = round(score, 3)
        if file_loader:
            try:
                meta["content"] = file_loader(src)
            except Exception:
                meta["content"] = ""
        out.append({"source": src, **meta})
    return out
