"""
engram.retrieval.wiki — Wiki index-based retrieval
===================================================

Searches compiled wiki topic indexes for pages relevant to a query.

The wiki is organized as:
  {wiki_path}/wiki/{topic}/_index.md   -- list of [[wikilinks]] with descriptions
  {wiki_path}/wiki/{topic}/{page}.md   -- compiled knowledge page

_index.md format (Obsidian-compatible wikilinks):
  - [[wiki/competition/Waymo]] -- autonomous vehicle company, robotaxi, Alphabet
  - [[wiki/people/Mike_Schoofs|Alice Chen]] -- CEO of AcmeCorp

This module parses those indexes, scores each entry against query tokens
(with a proper-noun boost), and returns the top-N page paths.

Index entries are cached per topic and invalidated when _index.md changes.

Resilience features (v0.2):
- Stemming:       morphological variants match (meetings/meeting, hired/hiring)
- Synonym expansion: config-driven equivalent terms passed from pipeline
- Fuzzy PN match: difflib catches typos in page names (Wayom->Waymo)
"""

from __future__ import annotations

import difflib
import re
from pathlib import Path
from typing import Optional

from .tokenizer import query_tokens, proper_nouns, stem_token, stemmed_tokens, expand_with_synonyms


# ─── Cache ────────────────────────────────────────────────────────────────────

_wiki_index_cache: dict[str, list] = {}   # topic -> [(page_ref, description), ...]
_wiki_index_mtime: dict[str, float] = {}  # topic -> _index.md mtime


def _parse_index(index_file: Path) -> list[tuple[str, str]]:
    """
    Parse a _index.md file into (page_ref, description) tuples.

    Accepts lines like:
      - [[wiki/competition/Waymo]] -- description text
      - * [[wiki/people/Mike_Schoofs|Alice Chen]] -- CEO
      - [[Page Name]] - short description
    """
    entries: list[tuple[str, str]] = []
    try:
        for line in index_file.read_text(errors="ignore").splitlines():
            m = re.match(
                r'\s*[-*]\s+\[\[([^\]|]+?)(?:\|[^\]]*)?]]\s*(?:[--]\s*(.*))?',
                line,
            )
            if m:
                entries.append((m.group(1).strip(), (m.group(2) or "").strip()))
    except Exception:
        pass
    return entries


def _get_cached(topic: str, index_file: Path) -> list[tuple[str, str]]:
    """Return cached entries for a topic, re-parsing if _index.md has changed."""
    try:
        mtime = index_file.stat().st_mtime
    except Exception:
        return []

    if _wiki_index_mtime.get(topic) != mtime:
        _wiki_index_cache[topic] = _parse_index(index_file)
        _wiki_index_mtime[topic] = mtime

    return _wiki_index_cache.get(topic, [])


# ─── Main scan ────────────────────────────────────────────────────────────────

def wiki_scan(
    query: str,
    wiki_path: Path,
    *,
    max_pages: int = 4,
    proper_noun_boost: float = 3.0,
    max_count_per_token: int = 5,
    topics: Optional[list] = None,
    synonyms: Optional[dict] = None,
) -> list[str]:
    """
    Search wiki topic indexes for pages relevant to the query.

    Scoring layers (applied in order, accumulated):
      1. Exact token match against page name + description
      2. Stem match for tokens that didn't exact-match (0.6x weight)
      3. Fuzzy match of long proper nouns against page name tokens (0.8x weight)

    Returns a list of absolute paths to the best matching wiki pages,
    up to max_pages. Returns [] if the wiki directory doesn't exist or
    the query has no signal tokens.

    Args:
        query:              Natural language user query.
        wiki_path:          Root of the knowledge-base-wiki directory.
        max_pages:          Maximum number of wiki pages to return.
        proper_noun_boost:  Score multiplier for proper-noun tokens.
        max_count_per_token: Cap token count per page (avoids over-scoring).
        topics:             If set, only scan these topic directories.
                            Defaults to all topic directories found.
        synonyms:           Dict of {term: [equivalent, ...]} for query expansion.
                            Same format as keyword.fast_file_score().
    """
    wiki_pages_dir = wiki_path / "wiki"
    if not wiki_pages_dir.exists():
        return []

    q_toks = query_tokens(query)

    # Synonym expansion (bidirectional)
    if synonyms:
        q_toks = expand_with_synonyms(q_toks, synonyms)

    if not q_toks:
        return []

    pn      = proper_nouns(query, q_toks)
    pn_long = {p for p in pn if len(p) >= 5}   # long proper nouns for fuzzy match
    q_stems = stemmed_tokens(q_toks)             # pre-compute query stems

    scored: list[tuple[float, str]] = []

    # Determine which topic dirs to scan
    if topics:
        index_files = [
            wiki_pages_dir / t / "_index.md"
            for t in topics
            if (wiki_pages_dir / t / "_index.md").exists()
        ]
    else:
        index_files = sorted(wiki_pages_dir.glob("*/_index.md"))

    for index_file in index_files:
        topic = index_file.parent.name
        entries = _get_cached(topic, index_file)

        for page_ref, description in entries:
            page_name   = page_ref.split("/")[-1]
            page_stem   = page_name.replace("_", " ").replace("-", " ").lower()
            search_text = page_stem + " " + description.lower()

            # Pre-compute token sets for this entry (wiki corpus is small, so OK)
            entry_toks  = set(re.split(r"\W+", search_text))
            entry_stems = stemmed_tokens({t for t in entry_toks if len(t) >= 3})

            score = 0.0

            # ── Layer 1: Exact token match ─────────────────────────────────────
            exact_matched: set = set()
            for tok in q_toks:
                if tok in search_text:
                    count       = min(search_text.count(tok), max_count_per_token)
                    boost_f     = proper_noun_boost if tok in pn else 1.0
                    score      += boost_f * count
                    exact_matched.add(tok)

            # ── Layer 2: Stem match (morphological variants) ───────────────────
            # Only for tokens that didn't already exact-match.
            for tok in q_toks - exact_matched:
                if len(tok) < 3:
                    continue
                tok_s = stem_token(tok)
                if len(tok_s) >= 3 and tok_s in entry_stems:
                    boost_f = (proper_noun_boost * 0.6) if tok in pn else 0.6
                    score  += boost_f   # count as 1 occurrence at reduced weight

            # ── Layer 3: Fuzzy page-name match for proper nouns ────────────────
            # Catches typos like "Wayom" -> "Waymo" or "Microsft" -> "Microsoft".
            # Applied only against page-name tokens (not description) for precision.
            page_name_toks = set(re.split(r"[\W_]+", page_stem))
            for pn_tok in pn_long:
                if pn_tok not in page_name_toks:    # skip already-exact-matched
                    if difflib.get_close_matches(pn_tok, page_name_toks, n=1, cutoff=0.82):
                        score += proper_noun_boost * 0.8

            if score > 0:
                page_path = wiki_pages_dir / topic / (page_name + ".md")
                if page_path.exists():
                    scored.append((score, str(page_path)))

    scored.sort(key=lambda x: -x[0])

    seen: set = set()
    result: list[str] = []
    for _, path in scored:
        if path not in seen and len(result) < max_pages:
            seen.add(path)
            result.append(path)

    return result


def invalidate_cache(topic: Optional[str] = None) -> None:
    """
    Clear the wiki index cache for a topic (or all topics).
    Call this after writing new wiki pages.
    """
    if topic:
        _wiki_index_cache.pop(topic, None)
        _wiki_index_mtime.pop(topic, None)
    else:
        _wiki_index_cache.clear()
        _wiki_index_mtime.clear()
