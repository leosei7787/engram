#!/usr/bin/env python3
"""
Run AI-driven deadline extraction over recent emails and write the result to
MEMORY/signals/deadlines.json (read by the Top of Mind dashboard tab).

Defaults to scanning the last 14 days. Pass --days N to widen / narrow.

Usage:
  python scripts/extract-deadlines.py [--days N] [--limit M]

Reads ~/.engram/config.yaml for memory_path and user_name.
"""
import argparse
import os
import sys
from pathlib import Path

# Allow running as a script without install
HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engram.retrieval.config import load_config       # noqa: E402
from engram.memory.signal_extractor import (          # noqa: E402
    extract_recent_email_signals,
    save_signals,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days",  type=int, default=14, help="days back to scan")
    ap.add_argument("--limit", type=int, default=80, help="max emails to LLM-process")
    args = ap.parse_args()

    cfg = load_config()
    user = cfg.identity.user_name or ""
    print(f"Extracting deadlines for {user or '<no user_name set>'}…")
    print(f"Memory: {cfg.memory_path}")

    sigs = extract_recent_email_signals(
        memory_path = cfg.memory_path,
        user_name   = user,
        cfg         = cfg,
        days_back   = args.days,
        limit       = args.limit,
    )
    out = save_signals(sigs, cfg.memory_path)

    print(f"\nScanned {sigs['scanned']} emails, found {sigs['with_action']} with actions.")
    print(f"  Filtered out: {sigs['filtered_out']['past']} past · "
          f"{sigs['filtered_out']['already_responded']} already-responded")
    print(f"  Surfaced:     {len(sigs['deadlines'])}")
    print(f"\nWrote {out}")
    if sigs["deadlines"][:5]:
        print("\nTop 5:")
        for d in sigs["deadlines"][:5]:
            print(f"  [{d['urgency']:6}] {d['deadline'] or '—'} · {d['subject'][:50]}")
            print(f"           → {d['action']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
