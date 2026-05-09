"""
engram.retrieval.domain_bundles — Query-domain forced file loading
=================================================================

Domain bundles map query trigger words to file path patterns that should
always be loaded when those triggers appear — regardless of keyword score.

This is the "breadcrumb" mechanism: if the user asks about "budget" or
"VW", we force-load the relevant canonical files (financials.md,
accounts/) even if they don't score highly on token overlap alone.

Bundles are configured in engram_config.yaml under `domain_bundles:`.
They can also be passed directly to match_domain_bundles() for testing.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ─── Default bundles (used if no config is provided) ─────────────────────────

DEFAULT_BUNDLES: list[dict] = [
    {
        "name":     "org_team",
        "triggers": {"org", "team", "people", "headcount", "fte", "hire", "hiring",
                     "vp", "director", "manager"},
        "patterns": ["context/org_", "context/people", "context/responsibility_"],
    },
    {
        "name":     "financials",
        "triggers": {"budget", "revenue", "arr", "cost", "margin", "financial",
                     "financials", "bonus", "compensation"},
        "patterns": ["context/financials"],
    },
    {
        "name":     "accounts",
        "triggers": {"account", "customer", "deal", "sourcing", "contract", "pipeline"},
        "patterns": ["accounts/"],
    },
]


# ─── Bundle matcher ───────────────────────────────────────────────────────────

def match_domain_bundles(
    query: str,
    memory_path: Path,
    base_path: Optional[Path] = None,
    bundles: Optional[list] = None,
) -> list[str]:
    """
    Return additional memory file paths to force-load based on query domain.

    Args:
        query:       Natural language user query.
        memory_path: Root of the memory file corpus.
        base_path:   Parent of memory_path (for computing relative paths).
                     Defaults to memory_path.parent.
        bundles:     List of bundle dicts (from config). Each bundle has:
                       name (str), triggers (list|set), patterns (list[str])
                     Defaults to DEFAULT_BUNDLES.

    Returns:
        List of relative file paths (relative to base_path) that match at
        least one active bundle pattern, deduped and sorted.
    """
    _bundles = bundles if bundles is not None else DEFAULT_BUNDLES
    _base    = base_path or memory_path.parent

    q_lower = query.lower()
    q_words = set(re.findall(r"\b\w+\b", q_lower))

    extras: list[str] = []
    for bundle in _bundles:
        triggers = set(bundle.get("triggers") or [])
        patterns = list(bundle.get("patterns") or [])

        if not (q_words & triggers):
            continue

        for pattern in patterns:
            for f in sorted(memory_path.rglob("*.md")):
                try:
                    rel = str(f.relative_to(_base)).replace("\\", "/")
                except ValueError:
                    rel = str(f)
                if pattern in rel and rel not in extras:
                    extras.append(rel)

    return extras


def bundles_from_config(config_bundles: list) -> list[dict]:
    """
    Convert config bundle dicts (from EngramConfig.domain_bundles) to the
    internal format expected by match_domain_bundles().

    Config format: [{name, description, triggers: [...], patterns: [...]}]
    Internal format: [{name, triggers: set, patterns: list}]
    """
    result: list[dict] = []
    for b in config_bundles:
        # Accept both dict (legacy) and DomainBundle dataclass
        if isinstance(b, dict):
            triggers = b.get("triggers") or []
            patterns = b.get("patterns") or []
            name     = b.get("name", "")
        else:
            triggers = getattr(b, "triggers", None) or []
            patterns = getattr(b, "patterns", None) or []
            name     = getattr(b, "name", "")
        if not patterns:
            continue
        result.append({
            "name":     name,
            "triggers": set(str(t).lower() for t in triggers),
            "patterns": [str(p) for p in patterns],
        })
    return result
