#!/usr/bin/env python3
"""
Backfill MEMORY/context/tone_of_voice.md from existing outgoing emails.

Run once after upgrading to seed the observed-patterns section with whatever
has already been ingested. The watcher takes over after that — every new
email from the user kicks off a refresh automatically.

Usage:
  python scripts/bootstrap-tone-of-voice.py [--days 90] [--max-emails 50]
"""
import argparse
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engram.retrieval.config import load_config                         # noqa: E402
from engram.memory.tone_extractor import (                              # noqa: E402
    refresh_tone_observations,
    ensure_tone_file,
    tone_file_path,
)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--days",       type=int, default=90, help="lookback window")
    ap.add_argument("--max-emails", type=int, default=50, help="cap on samples sent to Haiku")
    args = ap.parse_args()

    cfg  = load_config()
    user = cfg.identity.user_name  or ""
    mail = cfg.identity.user_email or ""

    if not user and not mail:
        print("✗ No user_name / user_email in config — can't identify your emails.", file=sys.stderr)
        return 2

    print(f"User:    {user!r}  email: {mail!r}")
    print(f"Memory:  {cfg.memory_path}")
    print(f"Window:  last {args.days} days · up to {args.max_emails} emails")
    print()

    fp = ensure_tone_file(cfg.memory_path)
    print(f"Tone file: {fp}")
    print()

    res = refresh_tone_observations(
        memory_path = cfg.memory_path,
        user_name   = user,
        user_email  = mail,
        cfg         = cfg,
        days_back   = args.days,
        max_emails  = args.max_emails,
    )

    if res.get("updated"):
        print(f"✓ Refreshed from {res['scanned']} emails")
        print(f"  style observations: {res['style_observations']}")
        print(f"  phrases used:       {res['phrases_used']}")
        print(f"  phrases avoided:    {res['phrases_avoided']}")
        print(f"\nReview / tighten by editing {fp} (or use the Browse tab).")
    else:
        print(f"✗ Did not update — reason: {res.get('reason')}")
        if res.get("reason") == "no_user_emails":
            print(f"  Your email cleaner has no emails matching From: {user!r} or {mail!r}.")
            print(f"  Check MEMORY/daily/emails/ — if your outgoing folder isn't being ingested,")
            print(f"  add it to your inbox source path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
