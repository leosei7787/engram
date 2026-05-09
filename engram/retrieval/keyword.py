"""
engram.retrieval.keyword — Fast keyword-based file scoring
==========================================================

Zero-latency BM25-style scoring across a memory file corpus.

Key design choices:
- Reads full file body (capped at body_read_chars) for better recall
- Proper-noun boost: capitalized query terms score N× a generic word match
- Term-frequency component: frequent mentions score higher (log-scaled + capped)
- Dynamic max_files: proper-noun or meeting queries return more files
- Meeting-from-person boost: surfaces emails FROM a named person for 1:1 prep
- All tuning values come from EngramConfig (or a passed config dict)

Resilience features (v0.2):
- Stemming:          morphological variants match (meetings/meeting, hired/hiring)
- Synonym expansion: config-driven equivalent terms (vw/acmemotors, car/automotive)
- Fuzzy PN match:    difflib catches typos in proper nouns (BetaMotorss->BetaMotors)
"""

from __future__ import annotations

import difflib
import math
import re
import time
from pathlib import Path
from typing import Optional

from .tokenizer import (
    query_tokens, proper_nouns, is_meeting_query, query_person_names,
    stem_token, stemmed_tokens, expand_with_synonyms,
)


# ─── Tunable config cache ─────────────────────────────────────────────────────
# Reads MEMORY/v5/retrieval_config.json so users can hot-tune weights without
# restarting the server. Falls back to EngramConfig values.

_RC_CACHE: dict = {"data": None, "ts": 0.0}
_RC_TTL = 30  # seconds


def _load_runtime_config(memory_path: Path, defaults: dict) -> dict:
    """
    Load tunable retrieval weights from {memory_path}/v5/retrieval_config.json.
    Cached for _RC_TTL seconds. Falls back to defaults if missing/malformed.
    """
    now = time.time()
    if _RC_CACHE["data"] is not None and (now - _RC_CACHE["ts"]) < _RC_TTL:
        return _RC_CACHE["data"]

    cfg = dict(defaults)
    cfg_path = memory_path / "v5" / "retrieval_config.json"
    if cfg_path.exists():
        try:
            import json
            user_cfg = json.loads(cfg_path.read_text())
            for k, v in user_cfg.items():
                if k.startswith("_"):
                    continue
                if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                    cfg[k].update(v)
                else:
                    cfg[k] = v
        except Exception as e:
            print(f"[keyword] retrieval_config.json error: {e}", flush=True)

    _RC_CACHE.update({"data": cfg, "ts": now})
    return cfg


# ─── Main scorer ─────────────────────────────────────────────────────────────

def fast_file_score(
    query: str,
    memory_path: Path,
    base_path: Optional[Path] = None,
    *,
    max_files: int = 8,
    body_read_chars: int = 8000,
    path_boosts: Optional[dict] = None,
    filename_match: Optional[dict] = None,
    score_components: Optional[dict] = None,
    dynamic_caps: Optional[dict] = None,
    meeting_from_person: Optional[dict] = None,
    scan_exclude: Optional[list] = None,
    people_file: Optional[Path] = None,
    claim_boosts: Optional[dict] = None,
    synonyms: Optional[dict] = None,
) -> list[tuple[str, float]]:
    """
    Score all .md files under memory_path for relevance to query.

    Returns:
        List of (relative_path, score) tuples, sorted descending by score.
        Relative paths are relative to base_path (or memory_path if not given).

    Args:
        query:        Natural language user query.
        memory_path:  Root of the memory file corpus.
        base_path:    Parent of memory_path (for relative paths). Defaults to
                      memory_path.parent.
        max_files:    Base maximum results (may be raised for proper-noun queries).
        body_read_chars: Characters of file body to read per file.
        path_boosts:  Dict of path fragment to score multiplier.
        filename_match: Dict with 'stem_hit_multiplier' and 'path_hit_multiplier'.
        score_components: Dict with 'overlap_log_factor', 'proper_noun_factor',
                          'tf_log_factor'.
        dynamic_caps: Dict with 'proper_noun_query_cap', 'meeting_query_cap'.
        meeting_from_person: Dict with thresholds for the from-person boost.
        scan_exclude: List of path fragments to skip.
        people_file:  Optional path to people.md for person-name extraction.
        claim_boosts: Pre-computed {rel_path: multiplicative_boost} dict.
        synonyms:     Dict of {term: [equivalent, ...]} for query expansion.
                      Bidirectional -- matched on any group member.
    """
    if not memory_path.exists():
        return []

    base = base_path or memory_path.parent
    _scan_exclude = scan_exclude or ["/sessions/", "/_raw/", "/_pre_compression_backups/",
                                     "/proposals/", "/priming/", "/health/"]
    _path_boosts = path_boosts or {
        "/accounts/": 2.5, "/decisions/": 1.8, "/weekly/": 1.6,
        "/context/": 1.4, "/saved/": 1.4, "/research/": 1.3,
        "/daily/emails/": 1.0, "/daily/": 0.9, "/episodic/": 1.1, "/archive/": 0.6,
    }
    _fn  = filename_match  or {"stem_hit_multiplier": 3.0, "path_hit_multiplier": 1.5}
    _sc  = score_components or {"overlap_log_factor": 1.0, "proper_noun_factor": 3.0, "tf_log_factor": 0.4}
    _dc  = dynamic_caps    or {"proper_noun_query_cap": 12, "meeting_query_cap": 15}
    _mtg = meeting_from_person or {
        "base_multiplier": 2.0, "short_email_threshold_bytes": 1500,
        "short_email_multiplier": 6.0, "recent_days_strong": 7,
        "recent_strong_multiplier": 4.0, "recent_days_weak": 14,
        "recent_weak_multiplier": 2.0,
    }
    _claim_boosts = claim_boosts or {}

    # ── Tokenize + expand ─────────────────────────────────────────────────────
    q_lower  = query.lower()
    recency  = any(w in q_lower for w in ("latest", "recent", "update", "current", "now", "today"))
    q_toks   = query_tokens(query)

    # Synonym expansion: bidirectional -- "vw" pulls in "acmemotors" and vice-versa
    if synonyms:
        q_toks = expand_with_synonyms(q_toks, synonyms)

    if not q_toks:
        return []

    pn          = proper_nouns(query, q_toks)
    pn_long     = {p for p in pn if len(p) >= 5}   # long proper nouns for fuzzy match
    q_stems     = stemmed_tokens(q_toks)             # pre-compute stems once
    has_pn      = len(pn) > 0
    is_mtg      = is_meeting_query(q_toks)
    mtg_persons = (query_person_names(query, people_file) if is_mtg else set())
    mtg_patterns = {p.lower() for p in mtg_persons}

    scored: list[tuple[str, float]] = []

    for f in sorted(memory_path.rglob("*.md")):
        try:
            sz = f.stat().st_size
        except Exception:
            continue
        if sz < 50:
            continue

        try:
            rel = str(f.relative_to(base))
        except ValueError:
            rel = str(f)

        # Skip excluded paths
        if any(exc in rel for exc in _scan_exclude):
            continue
        if f.name.startswith(".") or f.name.endswith(".digest.md"):
            continue

        # Read body
        try:
            body = f.read_text(errors="ignore")[:body_read_chars].lower()
        except Exception:
            body = ""

        haystack_path = rel.lower().replace("_", " ").replace("/", " ")
        haystack = haystack_path + " " + body
        hay_toks  = set(re.split(r"\W+", haystack))

        # ── Exact token overlap ───────────────────────────────────────────────
        overlap = len(q_toks & hay_toks)

        # ── Stem overlap (morphological variants) ─────────────────────────────
        # Catches: meetings/meeting, decided/deciding/decide, hired/hiring/hire.
        # Only stem tokens of reasonable length; short tokens are too ambiguous.
        hay_stems = stemmed_tokens({t for t in hay_toks if len(t) >= 3})
        # Subtract overlap to avoid double-counting already-exact-matched tokens.
        stem_hits = max(0, len(q_stems & hay_stems) - overlap)

        # ── Fuzzy proper-noun matching (typo resilience) ──────────────────────
        # Only on long proper nouns (>=5 chars) to keep false-positive rate low.
        # difflib cutoff=0.85 is strict: "BetaMotorss" matches "BetaMotors",
        # but "cat" does not match "car".
        fuzzy_pn_hits = 0
        if pn_long:
            for pn_tok in pn_long:
                if pn_tok not in hay_toks:      # skip already-exact-matched
                    if difflib.get_close_matches(pn_tok, hay_toks, n=1, cutoff=0.85):
                        fuzzy_pn_hits += 1

        # ── Combined signal ───────────────────────────────────────────────────
        # Stem hits: 0.6x (less certain than exact match).
        # Fuzzy PN hits: 1.5x (strong signal -- a fuzzy match on a proper noun
        # in a domain-specific corpus almost always means an intentional typo).
        effective_overlap = overlap + stem_hits * 0.6 + fuzzy_pn_hits * 1.5
        if effective_overlap == 0:
            continue

        # Proper noun hits: exact occurrences + confirmed fuzzy hits
        proper_hits = sum(1 for p in pn if p in haystack) + fuzzy_pn_hits
        tf = sum(min(haystack.count(t), 12) for t in q_toks)

        # Path boost
        boost = 1.0
        for pfrag, b in _path_boosts.items():
            if pfrag in rel:
                boost = float(b)
                break

        # Filename match
        stem_toks = set(re.split(r"[\W_]+", f.stem.lower()))
        if any(t in stem_toks for t in q_toks):
            boost *= float(_fn.get("stem_hit_multiplier", 3.0))
        elif any(t in rel.lower() for t in q_toks):
            boost *= float(_fn.get("path_hit_multiplier", 1.5))

        # Meeting-from-person boost
        if mtg_patterns and "/daily/emails/" in rel:
            head = body[:1500]
            for fname in mtg_patterns:
                if f"from: {fname}" in head or f"<{fname}." in head or f"<{fname}@" in head:
                    boost *= float(_mtg.get("base_multiplier", 2.0))
                    if sz < int(_mtg.get("short_email_threshold_bytes", 1500)):
                        boost *= float(_mtg.get("short_email_multiplier", 6.0))
                    try:
                        age = (time.time() - f.stat().st_mtime) / 86400
                        if age < int(_mtg.get("recent_days_strong", 7)):
                            boost *= float(_mtg.get("recent_strong_multiplier", 4.0))
                        elif age < int(_mtg.get("recent_days_weak", 14)):
                            boost *= float(_mtg.get("recent_weak_multiplier", 2.0))
                    except Exception:
                        pass
                    break

        # Claim-source boost (from atomic fact extraction)
        if rel in _claim_boosts:
            boost *= _claim_boosts[rel]

        if recency and "/weekly/" in rel:
            boost *= 1.5

        score = (
            float(_sc.get("overlap_log_factor", 1.0)) * effective_overlap * boost * math.log(1 + effective_overlap)
            + float(_sc.get("proper_noun_factor", 3.0)) * proper_hits * boost
            + float(_sc.get("tf_log_factor", 0.4)) * math.log(1 + tf) * boost
        )
        scored.append((rel, score))

    scored.sort(key=lambda x: -x[1])

    # Dynamic cap
    if has_pn and len(scored) > max_files * 2:
        max_files = min(int(_dc.get("proper_noun_query_cap", 12)), max_files * 3)
    if mtg_patterns:
        max_files = max(max_files, int(_dc.get("meeting_query_cap", 15)))

    return scored[:max_files]
