#!/usr/bin/env python3
"""
Prune stale source references from MEMORY/graph.json.

After file moves / email cleanups, graph.json can hold pointers to files
that no longer exist on disk (e.g. cleaned-up email digests). Those pointers
break "open file" actions in the UI. This script:

  1) Loads MEMORY/graph.json
  2) Drops any source path (in entity sources or edge sources) that doesn't
     resolve to an existing file under base_path.
  3) Removes entities/edges that end up with zero remaining sources.
  4) Writes a backup to graph.json.bak.<timestamp> before saving.

Defaults to dry-run; pass --apply to mutate.

Usage:
  python scripts/prune-stale-graph.py [--apply] [--config ~/.engram/config.yaml]
"""
import argparse
import json
import sys
import time
from pathlib import Path


def load_config(config_file: str | None) -> dict:
    import yaml  # type: ignore
    cfg_path = Path(config_file) if config_file else Path.home() / ".engram" / "config.yaml"
    if not cfg_path.exists():
        sys.exit(f"config not found: {cfg_path}")
    return yaml.safe_load(cfg_path.read_text()) or {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write changes")
    ap.add_argument("--config", help="path to engram config.yaml")
    args = ap.parse_args()

    cfg = load_config(args.config)
    paths = cfg.get("paths", {})
    memory_path = Path(paths.get("memory_path", "")).expanduser()
    if not memory_path.exists():
        sys.exit(f"memory_path not found: {memory_path}")
    base_path = memory_path.parent

    graph_file = memory_path / "graph.json"
    if not graph_file.exists():
        sys.exit(f"graph.json not found at {graph_file}")

    g = json.loads(graph_file.read_text())
    entities = g.get("entities", {}) or {}
    edges    = g.get("edges", []) or []

    def src_exists(s: str) -> bool:
        return (base_path / s).exists()

    # Pass 1: prune stale sources
    stale_sources: set[str] = set()
    for ent in entities.values():
        srcs = ent.get("sources") or []
        kept = [s for s in srcs if src_exists(s)]
        for s in srcs:
            if s not in kept:
                stale_sources.add(s)
        ent["sources"] = kept

    edge_stale = 0
    edge_kept_full = 0
    for e in edges:
        srcs = e.get("sources") or []
        kept = [s for s in srcs if src_exists(s)]
        edge_stale += len(srcs) - len(kept)
        if len(srcs) and len(kept):
            edge_kept_full += 1
        e["sources"] = kept

    # Pass 2: drop entities/edges that lost all sources
    drop_entities = [eid for eid, ent in entities.items() if not (ent.get("sources") or [])]
    keep_edges = [e for e in edges if (e.get("sources") or [])]
    dropped_edges = len(edges) - len(keep_edges)

    print(f"Source paths checked: {sum(len(ent.get('sources', []) or []) + len(stale_sources) for ent in entities.values()):,}")
    print(f"Stale entity sources removed: {len(stale_sources)} unique")
    print(f"Stale edge sources removed: {edge_stale}")
    print(f"Entities with zero remaining sources: {len(drop_entities)} (will be dropped)")
    print(f"Edges with zero remaining sources: {dropped_edges} (will be dropped)")
    print(f"Total entities: {len(entities)} → {len(entities) - len(drop_entities)}")
    print(f"Total edges:    {len(edges)} → {len(keep_edges)}")

    if not args.apply:
        print("\n(dry-run — pass --apply to write)")
        if stale_sources:
            print("\nSample stale sources (first 10):")
            for s in list(stale_sources)[:10]:
                print(f"  {s}")
        return 0

    for eid in drop_entities:
        del entities[eid]
    g["entities"] = entities
    g["edges"]    = keep_edges
    g["updated"]  = time.strftime("%Y-%m-%dT%H:%M:%S")

    backup = graph_file.with_suffix(f".json.bak.{int(time.time())}")
    backup.write_text(graph_file.read_text())
    graph_file.write_text(json.dumps(g, indent=2))

    print(f"\nbackup: {backup}")
    print(f"wrote:  {graph_file}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
