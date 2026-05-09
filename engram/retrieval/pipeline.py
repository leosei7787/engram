"""
engram.retrieval.pipeline — Memory scan orchestrator
=====================================================

The main retrieval pipeline. Orchestrates four stages into a single call:

  Stage 1 — Keyword scan
    Fast BM25-style scoring across all memory files. Returns top-N ranked files.

  Stage 2 — Domain bundles
    Force-load canonical files for recognised query domains (financials, accounts,
    org chart, etc.). Driven by trigger word matching — no score required.

  Stage 3 — Graph spreading activation
    Seed the entity graph from both (a) files found in Stage 1 and (b) entity
    names that match query tokens. Spread activation N hops to surface related
    files and entities the keyword scan missed.

  Stage 4 — Wiki scan
    Search compiled wiki topic indexes for relevant pages. Returns absolute paths.

Returns a dict:
  {
    "direct":        [str],   # relative paths from keyword + bundle scan
    "graph":         [str],   # relative paths from graph spreading activation
    "wiki":          [str],   # absolute paths of matching wiki pages
    "graph_context": str,     # structured markdown block for LLM injection
    "suggestions":   [str],   # high-scoring files that didn't make the cut
  }

Usage:
    from engram.retrieval.pipeline import memory_scan
    from engram.retrieval.config import load_config

    cfg = load_config()
    result = memory_scan("What's the status of the VW counter-offer?", cfg)
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from .config import EngramConfig, load_config
from .tokenizer import query_tokens
from .keyword import fast_file_score
from .graph import load_graph, spreading_activation, entity_seed_ids, graph_context_block, activated_to_files
from .wiki import wiki_scan
from .domain_bundles import match_domain_bundles, bundles_from_config


# ─── Main pipeline ────────────────────────────────────────────────────────────

def memory_scan(
    query: str,
    cfg: Optional[EngramConfig] = None,
    *,
    max_files: int = 8,
) -> dict:
    """
    Run the full four-stage retrieval pipeline.

    Args:
        query:     Natural language user query.
        cfg:       EngramConfig instance. Loaded from default config file if None.
        max_files: Base maximum number of direct files to return.

    Returns:
        dict with keys: direct, graph, wiki, graph_context, suggestions.
        All file paths are relative to cfg.base_path unless wiki (absolute).
    """
    if cfg is None:
        cfg = load_config()

    memory_path  = cfg.memory_path
    wiki_path    = cfg.wiki_path
    base_path    = cfg.base_path
    ret_cfg      = cfg.retrieval
    kw_cfg       = ret_cfg.keyword
    gr_cfg       = ret_cfg.graph
    wi_cfg       = ret_cfg.wiki
    bundles      = bundles_from_config(cfg.domain_bundles)

    t0 = time.time()
    synonyms = getattr(ret_cfg, "synonyms", {}) or {}

    # ── Stage 1: keyword scan ─────────────────────────────────────────────────
    FETCH_CAP = max(max_files * 2, 16)
    people_file = memory_path / "context" / "people.md"

    ranked = fast_file_score(
        query, memory_path, base_path,
        max_files        = FETCH_CAP,
        body_read_chars  = kw_cfg.body_read_chars,
        path_boosts      = kw_cfg.path_boosts,
        filename_match   = kw_cfg.filename_match,
        score_components = kw_cfg.score_components,
        dynamic_caps     = kw_cfg.dynamic_caps,
        meeting_from_person = kw_cfg.meeting_from_person,
        scan_exclude     = kw_cfg.scan_exclude,
        people_file      = people_file if people_file.exists() else None,
        synonyms         = synonyms,
    )
    all_scored  = [rel for rel, _score in ranked]
    direct      = all_scored[:max_files]
    suggestions = all_scored[max_files:]

    ms1 = (time.time() - t0) * 1000
    print(f"[memory_scan] stage1 keyword: {len(direct)} files in {ms1:.0f}ms", flush=True)

    # ── Stage 2: domain bundles ────────────────────────────────────────────────
    if bundles:
        bundle_files = match_domain_bundles(query, memory_path, base_path, bundles)
        added = 0
        for bf in bundle_files:
            if bf not in direct:
                direct.append(bf)
                added += 1
        if added:
            print(f"[memory_scan] stage2 bundles: +{added} files", flush=True)

    # ── Stage 3: graph spreading activation ───────────────────────────────────
    graph_extra   = []
    _graph_context = ""
    graph_file    = memory_path / "graph.json"

    try:
        g        = load_graph(graph_file)
        entities = g.get("entities", {})

        if entities:
            seed_set = set(direct)

            # Seed from files found by keyword scan
            file_seed_ids = {
                eid for eid, ent in entities.items()
                if any(src in seed_set for src in (ent.get("sources") or []))
            }

            # Seed from entity names matching query tokens
            q_toks = query_tokens(query)
            name_seed_ids = entity_seed_ids(q_toks, entities)

            all_seed_ids = file_seed_ids | name_seed_ids
            print(f"[memory_scan] stage3 graph seeds: "
                  f"{len(file_seed_ids)} files + {len(name_seed_ids)} names = {len(all_seed_ids)}",
                  flush=True)

            activated = (
                spreading_activation(
                    all_seed_ids, g,
                    depth     = gr_cfg.depth,
                    hop_decay = gr_cfg.hop_decay,
                    threshold = gr_cfg.threshold,
                )
                if all_seed_ids else {}
            )

            graph_extra = activated_to_files(
                activated, entities, seed_set, base_path,
                max_extra = gr_cfg.max_graph_extra_files,
            )

            _graph_context = graph_context_block(
                activated, g, all_seed_ids,
                high_threshold    = gr_cfg.high_activation_threshold,
                related_threshold = gr_cfg.related_activation_threshold,
                max_high          = ret_cfg.context_budget.graph_block_max_high,
                max_related       = ret_cfg.context_budget.graph_block_max_related,
            )

            if _graph_context:
                print(f"[memory_scan] stage3 graph context: {len(_graph_context)} chars, "
                      f"{len(activated)} activated entities", flush=True)

    except Exception as e:
        print(f"[memory_scan] stage3 graph error: {e}", flush=True)

    # ── Stage 4: wiki scan ────────────────────────────────────────────────────
    wiki_files: list[str] = []
    try:
        wiki_files = wiki_scan(
            query, wiki_path,
            max_pages           = wi_cfg.max_pages,
            proper_noun_boost   = wi_cfg.proper_noun_boost,
            max_count_per_token = wi_cfg.max_count_per_token,
            topics              = cfg.wiki.topics,
            synonyms            = synonyms,
            use_qmd             = getattr(wi_cfg, "use_qmd", True),
            qmd_collection      = getattr(wi_cfg, "qmd_collection", "wiki"),
        )
        if wiki_files:
            print(f"[memory_scan] stage4 wiki: {len(wiki_files)} pages — "
                  f"{', '.join(Path(p).stem[:30] for p in wiki_files)}", flush=True)
    except Exception as e:
        print(f"[memory_scan] stage4 wiki error: {e}", flush=True)

    # ── Finalise ──────────────────────────────────────────────────────────────
    loaded_set  = set(direct) | set(graph_extra)
    final_suggestions = [f for f in suggestions if f not in loaded_set][:6]

    total_ms = (time.time() - t0) * 1000
    print(
        f"[memory_scan] done in {total_ms:.0f}ms — "
        f"direct={len(direct)}, graph={len(graph_extra)}, "
        f"wiki={len(wiki_files)}, suggestions={len(final_suggestions)}",
        flush=True,
    )

    return {
        "direct":        direct,
        "graph":         graph_extra,
        "wiki":          wiki_files,
        "graph_context": _graph_context,
        "suggestions":   final_suggestions,
    }


# ─── Convenience wrappers ─────────────────────────────────────────────────────

def scan_with_config_file(
    query: str,
    config_file: Optional[str] = None,
    **kwargs,
) -> dict:
    """
    Run memory_scan loading config from a specific file path.

    Args:
        query:       User query.
        config_file: Path to engram_config.yaml. Defaults to ~/.engram/config.yaml.
        **kwargs:    Passed through to memory_scan().
    """
    cfg = load_config(config_file, reload=True)
    return memory_scan(query, cfg, **kwargs)
