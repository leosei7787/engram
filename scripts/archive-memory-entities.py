#!/usr/bin/env python3
"""
Archive legacy entity-record folders out of MEMORY/.

Background
----------
The repo split has settled on:

  - **wiki/**   = canonical entity records (people, decisions, projects,
                  concepts, competition, systems, problems). Maintained
                  manually + by the wiki ingestion scripts. Title Case
                  filenames matched on canonical name from graph.json.
  - **MEMORY/** = inputs (daily/, sessions/, weekly/) and runtime state
                  (signals/, proposals/, graph.json, pinned/, priming/, …).

Before this consolidation the harvester also wrote auto-generated entity
stubs into MEMORY/accounts/ and MEMORY/decisions/. Those folders have
been superseded by the wiki and should not be picked up by the curator
any more.

What this does
--------------
Moves the listed legacy folders into MEMORY/_archive/<timestamp>/. The
curator already skips /_archive/ via ``scan_exclude``, so archived files
no longer show up in retrieval but remain on disk for manual review.

Defaults are conservative:
  * Dry-run unless ``--apply`` is passed.
  * Only the two folders the harvester historically wrote to
    (``accounts/``, ``decisions/``) are archived. Everything else
    stays where it is — pass ``--include <folder>`` to add more.
  * Wiki is never read or written.

Usage
-----
  python scripts/archive-memory-entities.py                    # dry-run
  python scripts/archive-memory-entities.py --apply
  python scripts/archive-memory-entities.py --include context  # add another
"""
from __future__ import annotations

import argparse
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

from engram.retrieval.config import load_config                                  # noqa: E402


DEFAULT_FOLDERS = ["accounts", "decisions"]


def _file_count(p: Path) -> int:
    if not p.exists():
        return 0
    return sum(1 for _ in p.rglob("*") if _.is_file())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply",   action="store_true",
                    help="Actually move folders. Without this, prints what would happen.")
    ap.add_argument("--include", action="append", default=[],
                    help="Additional folder name (relative to MEMORY/) to archive. "
                         "Repeat for multiple. e.g. --include context --include weekly.")
    args = ap.parse_args()

    cfg         = load_config()
    memory_path = cfg.memory_path
    if not memory_path.exists():
        sys.exit(f"memory_path does not exist: {memory_path}")

    folders = list(DEFAULT_FOLDERS) + list(args.include)
    seen: set[str] = set()
    folders = [f for f in folders if not (f in seen or seen.add(f))]   # dedup, preserve order

    ts          = time.strftime("%Y%m%d-%H%M%S")
    archive_dir = memory_path / "_archive" / ts

    print(f"memory_path = {memory_path}")
    print(f"archive_dir = {archive_dir}\n")

    moves: list[tuple[Path, Path, int]] = []
    for name in folders:
        src = memory_path / name
        if not src.exists():
            print(f"  skip   {name:<20} (does not exist)")
            continue
        if not src.is_dir():
            print(f"  skip   {name:<20} (not a directory)")
            continue
        dst = archive_dir / name
        n   = _file_count(src)
        moves.append((src, dst, n))
        print(f"  ready  {name:<20} ({n} files) -> {dst.relative_to(memory_path)}")

    if not moves:
        print("\nNothing to archive.")
        return 0

    if not args.apply:
        print("\nDRY RUN — pass --apply to actually move these folders.")
        return 0

    archive_dir.mkdir(parents=True, exist_ok=True)
    for src, dst, _n in moves:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        print(f"  moved  {src.relative_to(memory_path)} -> {dst.relative_to(memory_path)}")

    print(f"\nDone. Archived {len(moves)} folder(s) under {archive_dir.relative_to(memory_path)}/")
    print("The curator already skips /_archive/ — these files no longer affect retrieval.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
