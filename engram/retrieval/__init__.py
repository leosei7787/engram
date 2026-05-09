"""
engram.retrieval — Zero-latency multi-stage context assembly.

Public API:

    from engram.retrieval import memory_scan, load_config

    cfg    = load_config()                          # reads ~/.engram/config.yaml
    result = memory_scan("VW counter-offer status", cfg)

    result["direct"]        # list of memory file paths (keyword + bundle scan)
    result["graph"]         # list of additional paths from graph activation
    result["wiki"]          # list of absolute wiki page paths
    result["graph_context"] # markdown block for LLM system prompt injection
    result["suggestions"]   # high-score files that didn't fit the budget

Pipeline stages:
  1. Keyword scan   — fast BM25-style scoring (engram.retrieval.keyword)
  2. Domain bundles — force-load canonical files per query domain
  3. Graph spread   — spreading activation over entity graph
  4. Wiki scan      — index-based wiki page retrieval
"""

from .config import load_config, get_config, EngramConfig
from .pipeline import memory_scan, scan_with_config_file
from .tokenizer import query_tokens, proper_nouns, is_meeting_query
from .keyword import fast_file_score
from .graph import (
    load_graph, spreading_activation, entity_seed_ids,
    graph_context_block, activated_to_files,
)
from .wiki import wiki_scan, invalidate_cache as invalidate_wiki_cache
from .domain_bundles import match_domain_bundles, bundles_from_config

__all__ = [
    # Config
    "load_config", "get_config", "EngramConfig",
    # Pipeline
    "memory_scan", "scan_with_config_file",
    # Tokenizer
    "query_tokens", "proper_nouns", "is_meeting_query",
    # Keyword
    "fast_file_score",
    # Graph
    "load_graph", "spreading_activation", "entity_seed_ids",
    "graph_context_block", "activated_to_files",
    # Wiki
    "wiki_scan", "invalidate_wiki_cache",
    # Domain bundles
    "match_domain_bundles", "bundles_from_config",
]
