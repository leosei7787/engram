#!/usr/bin/env python3
"""
Run the engram dream cycle (sleep cycle), wired with the session-file
consolidation runner so today's chat sessions feed into Phase 2's episodic
harvest.

This is the script you'd schedule via launchd / cron to run nightly. By
default it runs the full 6-phase cycle, but skips compression (heavy and
optional). See `engram/memory/sleep_cycle.py` for what each phase does.

Usage:
  python scripts/run-dream-cycle.py [--max-age-hours 48] [--no-skip-compression]

Reads ~/.engram/config.yaml for memory_path and user_name.
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engram.retrieval.config import load_config                         # noqa: E402
from engram.memory.sleep_cycle import run_sleep_cycle                   # noqa: E402
from engram.memory.session_harvester import (                           # noqa: E402
    make_session_consolidation_runner,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--max-age-hours", type=int, default=48,
                    help="how far back to scan session files (default: 48h)")
    ap.add_argument("--no-skip-compression", action="store_true",
                    help="run the heavy compression phase too (default: skipped)")
    args = ap.parse_args()

    cfg  = load_config()
    user = cfg.identity.user_name or ""

    print(f"engram dream cycle — memory: {cfg.memory_path}")
    print(f"  user:           {user or '<unset>'}")
    print(f"  session window: last {args.max_age_hours}h")
    print(f"  compression:    {'on' if args.no_skip_compression else 'skipped'}")
    print()

    runner = make_session_consolidation_runner(
        memory_path   = cfg.memory_path,
        user_name     = user,
        cfg           = cfg,
        max_age_hours = args.max_age_hours,
    )

    summary = run_sleep_cycle(
        cfg.memory_path,
        consolidation_runner = runner,
        skip_compression     = not args.no_skip_compression,
    )

    print("\nSummary:")
    for phase in summary.get("phases", []):
        name = phase.get("phase", "?")
        dur  = phase.get("duration_s", "—")
        print(f"  {name:30}  {dur}s")

    cons = summary.get("consolidation", {}) or {}
    if cons:
        print(f"\nSession harvest:")
        print(f"  files scanned: {cons.get('files_scanned', 0)}")
        print(f"  proposals:     {cons.get('proposals', 0)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
