#!/usr/bin/env python3
"""
Clean up MEMORY/proposals/index.json:

  1. **Canonicalize person paths** — re-resolve subject names through the
     entity graph so e.g. context/people/dave.md and
     context/people/dave_johnson.md collapse to one canonical slug.

  2. **Semantic dedup** — within each (path, kind) group, mark older
     pending proposals as 'superseded' when their `reason` text overlaps
     a newer one above a Jaccard token threshold (default 0.55).

Dry-run by default. Pass --apply to write changes (auto-backup is taken).

Usage:
  python scripts/dedup-proposals.py [--apply] [--threshold 0.55]
"""
import argparse
import json
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engram.retrieval.config import load_config                                  # noqa: E402
from engram.memory.session_harvester import (                                    # noqa: E402
    _build_name_index,
    _resolve_canonical_name,
    _slug,
)


# ─── Tokenisation + similarity ────────────────────────────────────────────────

_WORD_RX = re.compile(r"[a-z][a-z0-9\-']+")
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "have", "been",
    "will", "than", "when", "what", "their", "they", "them", "would", "could",
    "should", "about", "after", "before", "your", "ours", "very",
}


def _tokens(text: str) -> set[str]:
    return {w for w in _WORD_RX.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2}


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ─── Path canonicalisation ────────────────────────────────────────────────────

def canonicalize_path(path: str, name_idx: dict[str, str]) -> str:
    """Rewrite context/people/<slug>.md to the canonical-name slug."""
    if not path or not path.startswith("context/people/"):
        return path
    stem = Path(path).stem.replace("_", " ")
    canonical = _resolve_canonical_name(stem, name_idx)
    if not canonical:
        return path
    new_slug = _slug(canonical)
    return f"context/people/{new_slug}.md"


# ─── Main pass ────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply",     action="store_true", help="actually mutate")
    ap.add_argument("--threshold", type=float, default=0.55,
                    help="Jaccard threshold above which two reasons collapse")
    ap.add_argument("--source-prefix", default="chat_session",
                    help="only touch proposals whose source starts with this (safety)")
    args = ap.parse_args()

    cfg = load_config()
    idx_path = cfg.memory_path / "proposals" / "index.json"
    if not idx_path.exists():
        sys.exit(f"proposals index not found: {idx_path}")

    name_idx = _build_name_index(cfg.memory_path)
    print(f"Loaded {len(name_idx)} name index entries from graph.json")

    proposals = json.loads(idx_path.read_text())
    print(f"Loaded {len(proposals)} proposals from {idx_path}")

    # Phase 1: canonicalise paths on PENDING items in scope
    rewrites: list[tuple[str, str]] = []
    for p in proposals:
        if p.get("status") != "pending":
            continue
        if not str(p.get("source", "")).startswith(args.source_prefix):
            continue
        old = p.get("path") or ""
        new = canonicalize_path(old, name_idx)
        if new != old:
            rewrites.append((old, new))
            p["path"] = new

    print(f"\nPath rewrites: {len(rewrites)}")
    seen_pairs = set()
    for old, new in rewrites:
        key = (old, new)
        if key in seen_pairs:
            continue
        seen_pairs.add(key)
        if len(seen_pairs) <= 12:
            print(f"  {old}  →  {new}")

    # Phase 2: group + supersede semantic duplicates within each path
    # Only consider chat_session-source pending items; group by path; within
    # each group, walk newest → oldest, mark earlier items whose tokens
    # overlap above threshold.
    in_scope = [
        p for p in proposals
        if p.get("status") == "pending"
        and str(p.get("source", "")).startswith(args.source_prefix)
    ]
    by_path: dict[str, list[dict]] = {}
    for p in in_scope:
        by_path.setdefault(p.get("path", ""), []).append(p)

    superseded_count = 0
    examples: list[tuple[str, str]] = []

    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    for path, group in by_path.items():
        if len(group) < 2:
            continue
        # Newest first by ts
        group.sort(key=lambda p: p.get("ts", ""), reverse=True)
        # Pre-tokenise once
        with_tokens = [(p, _tokens(p.get("reason", ""))) for p in group]
        # Walk, marking later (older) items if they overlap an earlier (newer) one
        kept: list[tuple[dict, set]] = []
        for p, toks in with_tokens:
            collapsed = False
            for k_p, k_toks in kept:
                sim = _jaccard(toks, k_toks)
                if sim >= args.threshold:
                    p["status"]         = "superseded"
                    p["superseded_at"]  = now
                    p["superseded_by"]  = k_p.get("uid")
                    p["dedup_jaccard"]  = round(sim, 2)
                    superseded_count   += 1
                    if len(examples) < 8:
                        examples.append((
                            (k_p.get("reason") or "")[:90],
                            (p.get("reason") or "")[:90],
                        ))
                    collapsed = True
                    break
            if not collapsed:
                kept.append((p, toks))

    print(f"\nSemantic dedup (Jaccard ≥ {args.threshold}):")
    print(f"  superseded: {superseded_count} pending proposal(s)")
    if examples:
        print("  examples (kept ⇆ superseded):")
        for k, s in examples:
            print(f"    KEPT:  {k}")
            print(f"    SUPER: {s}")
            print()

    if not args.apply:
        print("(dry-run — pass --apply to write)")
        return 0

    # Backup + write
    backup = idx_path.with_suffix(f".json.bak.{int(time.time())}")
    backup.write_text(json.dumps(json.loads(idx_path.read_text()), indent=2))
    idx_path.write_text(json.dumps(proposals, indent=2))
    print(f"\nbackup: {backup}")
    print(f"wrote:  {idx_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
