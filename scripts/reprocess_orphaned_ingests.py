#!/usr/bin/env python3
"""
reprocess_orphaned_ingests.py — recover files where the watcher raced with
Power Automate / OneDrive and archived them with empty content.

Root cause (fixed in watcher.py): the watcher used to poll a file that was
still being written, get empty content back with no error, mark it "done"
with the empty-content hash, and archive to _processed/. When PA/OneDrive
finished writing seconds later, the file had real content but was already
out of the watcher's scope.

This script:
  1. Reads the watcher's seen-registry (.watcher_seen.json).
  2. Finds entries whose recorded content_hash matches known "empty" hashes.
  3. For each, checks if a same-named file lives in _processed/ NOW with
     real content.
  4. Moves the orphaned file back to the inbox root and removes its stale
     seen-registry entry, so the next watcher cycle picks it up cleanly.

Dry-run by default. Pass --apply to actually move files.

Usage:
  python scripts/reprocess_orphaned_ingests.py          # dry-run
  python scripts/reprocess_orphaned_ingests.py --apply  # do it
  python scripts/reprocess_orphaned_ingests.py --apply --limit 100
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))
from engram.retrieval.config import load_config

MIN_REAL_CONTENT_BYTES = 200

EMPTY_HASHES = {
    hashlib.sha1(b"").hexdigest()[:16],       # "" — write race
    hashlib.sha1(b"----").hexdigest()[:16],   # "----" — PA "no email" marker
    hashlib.sha1(b"----\n").hexdigest()[:16],
    hashlib.sha1(b"--\n").hexdigest()[:16],
    hashlib.sha1(b"-\n").hexdigest()[:16],
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="Actually move files (default: dry-run)")
    ap.add_argument("--limit", type=int, default=0, help="Cap files reprocessed (0 = no cap)")
    args = ap.parse_args()

    cfg = load_config()
    memory_path = cfg.memory_path
    inbox_root = Path(cfg.paths.inbox_src)
    processed_root = inbox_root / "_processed"
    seen_path = memory_path / ".watcher_seen.json"

    if not inbox_root.exists():
        print(f"inbox missing: {inbox_root}", file=sys.stderr)
        return 1
    if not seen_path.exists():
        print(f"seen-registry missing: {seen_path}", file=sys.stderr)
        return 1

    seen: dict[str, str] = json.loads(seen_path.read_text())

    empty_recorded = [(p, h) for p, h in seen.items() if h in EMPTY_HASHES]
    print(f"Seen registry: {len(seen)} entries total, "
          f"{len(empty_recorded)} recorded with empty content")

    candidates: list[tuple[str, str, Path]] = []
    for orig_path, recorded_hash in empty_recorded:
        name = os.path.basename(orig_path)
        processed_file = processed_root / name
        if not processed_file.exists():
            continue
        try:
            st = processed_file.stat()
        except OSError:
            continue
        if st.st_size < MIN_REAL_CONTENT_BYTES:
            continue
        candidates.append((orig_path, recorded_hash, processed_file))

    print(f"Candidates (in _processed with >= {MIN_REAL_CONTENT_BYTES} bytes): {len(candidates)}")

    if args.limit:
        candidates = candidates[: args.limit]
        print(f"Capped by --limit to {len(candidates)}")

    if not candidates:
        return 0

    print()
    print(f"{'ACTION':10s} {'SIZE':>8s}  NAME")
    moved = 0
    for orig_path, _recorded, processed_file in candidates:
        name = processed_file.name
        dest = inbox_root / name
        size = processed_file.stat().st_size
        if dest.exists():
            print(f"{'skip-collide':10s} {size:>8d}  {name}  (file with same name already in inbox root)")
            continue
        action = "MOVE" if args.apply else "would-move"
        print(f"{action:10s} {size:>8d}  {name}")
        if args.apply:
            processed_file.rename(dest)
            seen.pop(orig_path, None)
            moved += 1

    if args.apply:
        seen_path.write_text(json.dumps(seen, indent=2))
        print(f"\nMoved {moved} files back to inbox root.")
        print(f"Cleared {moved} stale seen-registry entries.")
        print("Watcher will pick these up on its next scan cycle.")
    else:
        print(f"\nDry-run — pass --apply to actually move {len(candidates)} files.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
