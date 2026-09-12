#!/usr/bin/env python3
"""
migrate_claude_ingest_by_type.py — one-shot migration for existing
Claude-Cowork ingests that landed in ``MEMORY/daily/emails/`` before the
type-aware router (``engram.ingest.router``) shipped.

Uses the exact same routing rules the live watcher will use going
forward. Files that route back to ``emails/`` (the fallback) are left
alone — nothing else changes shape.

Dry-run by default. Pass ``--apply`` to actually move files.

Does NOT touch ``$MEMORY/.watcher_seen.json`` — its keys are absolute
ClaudeContext source paths, unrelated to where the ingested copy lives.

Usage:
  python scripts/migrate_claude_ingest_by_type.py                # dry-run
  python scripts/migrate_claude_ingest_by_type.py --apply        # do it
  python scripts/migrate_claude_ingest_by_type.py --sample 8     # dry-run, only 8 files
  python scripts/migrate_claude_ingest_by_type.py --pattern 'claude-*.md'
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from engram.retrieval.config import load_config       # noqa: E402
from engram.ingest.router import resolve_dest_subfolder  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="Actually move files. Default is a dry-run.")
    ap.add_argument("--pattern", default="claude-*.md",
                    help="Glob under daily/emails/ to consider (default: claude-*.md)")
    ap.add_argument("--sample", type=int, default=0,
                    help="Only print/act on the first N matches (0 = all)")
    args = ap.parse_args()

    cfg = load_config()
    daily = cfg.memory_path / "daily"
    src_dir = daily / "emails"
    if not src_dir.exists():
        print(f"source missing: {src_dir}", file=sys.stderr)
        return 1

    files = sorted(src_dir.glob(args.pattern))
    print(f"Source: {src_dir}")
    print(f"Pattern: {args.pattern!r} → {len(files)} candidate(s)")
    if args.sample and len(files) > args.sample:
        files = files[: args.sample]
        print(f"Sampled to first {len(files)} for this run.")
    if not files:
        print("nothing to do.")
        return 0

    # Precompute where things would go, and by which rule.
    # Read only the file HEAD so we don't slurp giant transcripts into memory.
    plans: list[tuple[Path, str, str]] = []  # (src, dest_subfolder, rule)
    for f in files:
        try:
            head = f.read_text(errors="ignore")[:4096]
        except OSError:
            print(f"  ! could not read {f.name}", file=sys.stderr)
            continue
        sub = resolve_dest_subfolder(f.name, head)
        # Reverse-engineer which rule fired (for the report)
        if head.startswith(("---\n", "---\r\n")) and sub != "emails":
            rule = "frontmatter"
        elif f.name.startswith("claude-session-") and sub == "conversations":
            rule = "prefix"
        elif f.name.startswith("claude-doc-") and sub == "documents":
            rule = "prefix"
        else:
            rule = "fallback"
        plans.append((f, sub, rule))

    # Report
    print()
    print(f"{'RULE':>11s}  {'DEST':>13s}  NAME")
    print(f"{'-' * 11}  {'-' * 13}  {'-' * 40}")
    counts: dict[str, int] = {}
    moves: list[tuple[Path, Path]] = []
    for src, sub, rule in plans:
        counts[sub] = counts.get(sub, 0) + 1
        dest = daily / sub / src.name
        marker = "→" if sub != "emails" else "="
        print(f"{rule:>11s}  {sub:>13s}  {marker} {src.name}")
        if sub != "emails":
            moves.append((src, dest))

    print()
    print("Summary by destination:")
    for k in sorted(counts):
        print(f"  {counts[k]:>4d}  daily/{k}/")
    print(f"  {len(moves)} of {len(plans)} would be moved (rest already in emails/).")

    if not args.apply:
        print()
        print("Dry-run — no files touched. Pass --apply to actually move.")
        return 0

    # Apply: create destination dirs if missing, then rename.
    print()
    print("Applying moves…")
    ok = 0
    skipped = 0
    for src, dest in moves:
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                print(f"  ! skip-collide {dest.relative_to(cfg.memory_path)}")
                skipped += 1
                continue
            # shutil.move copes with cross-device — src and dest are on the
            # same filesystem here (both under memory_path) so it degrades
            # to a fast rename.
            shutil.move(str(src), str(dest))
            ok += 1
        except OSError as e:
            print(f"  ! move failed for {src.name}: {e}", file=sys.stderr)

    print(f"Moved {ok} file(s). Skipped {skipped} name-collision(s).")
    print("Note: .watcher_seen.json intentionally untouched — its keys are")
    print("      ClaudeContext source paths, still valid regardless of dest.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
