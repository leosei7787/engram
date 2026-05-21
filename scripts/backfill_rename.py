#!/usr/bin/env python3
"""
backfill_rename.py — one-shot rename of past-ingested files into descriptive names.

The live ingest path (``engram.dashboard.server._ingest``) writes new emails
and Slack extracts with a descriptive filename derived by
``engram.ingest.metadata`` — but files that landed BEFORE that fix still have
opaque names like ``Recent_email.mdrecent_email_<timestamp>.md``. The
curator's keyword scan weights filename-stem hits 3×, so those legacy files
land in memory but never surface in retrieval.

This script walks ``MEMORY/daily/emails/`` for files modified within
``--days`` and writes a SIBLING file with a descriptive name. The original
is preserved (with a back-pointer comment in the new file) so nothing that
references the old path breaks.

When ``--calendar`` is passed, also runs the per-event ICS explosion via
``engram.ingest.ics.explode_to_files`` so meetings become findable too.

Usage::

    python -m scripts.backfill_rename --days 5
    python -m scripts.backfill_rename --days 7 --calendar
    python -m scripts.backfill_rename --dry-run
"""
from __future__ import annotations

import argparse
import hashlib
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Make the package importable when run as a script
_repo_root = Path(__file__).resolve().parent.parent
if str(_repo_root) not in sys.path:
    sys.path.insert(0, str(_repo_root))

from engram.ingest.metadata import (   # noqa: E402
    detect_shape,
    email_filename,
    slack_filename,
)


def _resolve_memory_path() -> Path:
    """Find the memory_path from the engram config — same source the live
    server uses, so backfill always writes to the right place."""
    from engram.retrieval.config import load_config  # noqa: E402
    cfg = load_config()
    return Path(cfg.memory_path)


def backfill_emails_and_slack(*, memory_path: Path, days: int, dry_run: bool) -> dict:
    emails_dir = memory_path / "daily" / "emails"
    if not emails_dir.exists():
        print(f"  ! emails dir not found: {emails_dir}", file=sys.stderr)
        return {"emails_renamed": 0, "slack_renamed": 0, "skipped_existing": 0,
                "skipped_no_signal": 0, "samples": []}

    cutoff = (datetime.now() - timedelta(days=days)).timestamp()
    stats = {"emails_renamed": 0, "slack_renamed": 0, "skipped_existing": 0,
             "skipped_no_signal": 0, "samples": []}

    for fp in sorted(emails_dir.glob("*.md")):
        if not fp.is_file():
            continue
        try:
            if fp.stat().st_mtime < cutoff:
                continue
        except Exception:
            continue
        # Already descriptive — skip
        if fp.name.startswith("email_") or fp.name.startswith("slack_"):
            continue

        text = fp.read_text(errors="ignore")[:30000]
        shape = detect_shape(fp.name, text[:3000])
        if shape == "email":
            stem = email_filename(text, source_name=fp.name, mtime=fp.stat().st_mtime)
            kind = "email"
        elif shape == "slack":
            stem = slack_filename(text, source_name=fp.name, mtime=fp.stat().st_mtime)
            kind = "slack"
        else:
            continue

        if not stem:
            stats["skipped_no_signal"] += 1
            continue

        new_fp = fp.with_name(stem + ".md")
        if new_fp.exists() and new_fp != fp:
            h = hashlib.md5(fp.name.encode()).hexdigest()[:6]
            new_fp = fp.with_name(f"{stem}__{h}.md")
        if new_fp.exists():
            stats["skipped_existing"] += 1
            continue

        if not dry_run:
            note = (f"<!-- backfilled rename: original sibling preserved at "
                    f"`{fp.name}` -->\n\n")
            full = fp.read_text(errors="ignore")
            new_fp.write_text(note + full, encoding="utf-8")
            # Preserve the source mtime so recent-activity sorts by when the
            # email actually arrived, not by backfill time.
            try:
                src_mtime = fp.stat().st_mtime
                os.utime(new_fp, (src_mtime, src_mtime))
            except Exception:
                pass

        if kind == "email":
            stats["emails_renamed"] += 1
        else:
            stats["slack_renamed"] += 1
        if len(stats["samples"]) < 5:
            stats["samples"].append(f"  {fp.name}\n    → {new_fp.name}")

    return stats


def backfill_calendar(*, memory_path: Path, dry_run: bool) -> dict:
    """Locate the most-recent ICS in inbox or memory and explode its events."""
    from engram.memory.calendar_extractor import find_latest_ics  # noqa: E402
    from engram.retrieval.config import load_config                # noqa: E402
    from engram.ingest.ics import explode_to_files                 # noqa: E402

    cfg = load_config()
    inbox = getattr(getattr(cfg, "paths", None), "inbox_src", None)
    ics = find_latest_ics(
        inbox_src   = Path(inbox) if inbox else None,
        memory_path = memory_path,
    )
    if not ics:
        return {"found": False, "written": 0}

    if dry_run:
        print(f"  (dry run — would explode {ics})")
        return {"found": True, "ics": str(ics), "written": 0, "dry": True}

    return {"found": True, "ics": str(ics),
            **explode_to_files(ics, memory_path=memory_path, days_back=7, days_ahead=30)}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument("--days", type=int, default=5,
                    help="rename emails/slack files modified within N days (default 5)")
    ap.add_argument("--calendar", action="store_true",
                    help="also explode the current calendar.ics into per-event files")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    memory_path = _resolve_memory_path()
    print(f"=== backfill_rename ({'DRY RUN' if args.dry_run else 'LIVE'}) ===")
    print(f"memory_path: {memory_path}")
    print(f"window     : last {args.days} days")
    print()

    s = backfill_emails_and_slack(memory_path=memory_path, days=args.days, dry_run=args.dry_run)
    print(f"emails renamed     : {s['emails_renamed']}")
    print(f"slack renamed      : {s['slack_renamed']}")
    print(f"skipped (exists)   : {s['skipped_existing']}")
    print(f"skipped (no signal): {s['skipped_no_signal']}")
    if s["samples"]:
        print("\nsample renames:")
        for line in s["samples"]:
            print(line)

    if args.calendar:
        print("\n=== calendar explosion ===")
        c = backfill_calendar(memory_path=memory_path, dry_run=args.dry_run)
        if not c.get("found"):
            print("  no ICS found in inbox or memory — skipped")
        else:
            print(f"  ics              : {c.get('ics')}")
            if not c.get("dry"):
                print(f"  events written   : {c.get('written', 0)}")
                print(f"  skipped cancelled: {c.get('skipped_cancelled', 0)}")
                print(f"  out of window    : {c.get('skipped_out_of_window', 0)}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
