"""
Migrate v2 graph.json (and existing memory files) to v3 schema.

Idempotent: running multiple times is safe — already-migrated nodes
are not modified.
"""
import json
import time
from pathlib import Path
from .schemas import (
    default_entity_v3, default_edge_v3, _now,
    SOURCE_CREDIBILITY,
)
from .tiers import classify_tier_for_entity, classify_tier_for_edge
from .salience import infer_entity_salience
from .source_credibility import credibility_for_sources


SCHEMA_VERSION = "v3.0"


def migrate_graph(graph: dict) -> tuple[dict, dict]:
    """
    Upgrade an existing graph to v3 schema.
    Returns (new_graph, migration_stats).
    """
    stats = {
        "entities_upgraded": 0,
        "edges_upgraded":    0,
        "entities_skipped":  0,
        "edges_skipped":     0,
        "tier_assignments":  {"working": 0, "episodic": 0, "semantic": 0, "crystallised": 0},
    }

    if "schema_version" not in graph or graph.get("schema_version") != SCHEMA_VERSION:
        graph["schema_version"] = SCHEMA_VERSION

    # ── entities ─────────────────────────────────────────────────────────────
    for eid, ent in graph.get("entities", {}).items():
        already_v3 = "tier" in ent and "salience" in ent
        if already_v3:
            stats["entities_skipped"] += 1
            stats["tier_assignments"][ent["tier"]] = \
                stats["tier_assignments"].get(ent["tier"], 0) + 1
            continue

        # Apply v3 defaults
        for k, v in default_entity_v3().items():
            ent.setdefault(k, v)

        # Reclassify tier from sources
        primary_source = (ent.get("sources") or [""])[0]
        ent["tier"] = classify_tier_for_entity(ent, primary_source)
        stats["tier_assignments"][ent["tier"]] = \
            stats["tier_assignments"].get(ent["tier"], 0) + 1

        # Infer salience
        sal = infer_entity_salience(ent, graph)
        ent["salience"] = sal.to_dict()

        stats["entities_upgraded"] += 1

    # ── edges ────────────────────────────────────────────────────────────────
    entities = graph.get("entities", {})
    for ed in graph.get("edges", []):
        already_v3 = "tier" in ed and "source_credibility" in ed
        if already_v3:
            stats["edges_skipped"] += 1
            continue

        for k, v in default_edge_v3().items():
            ed.setdefault(k, v)

        # Edge tier from endpoints
        from_ent = entities.get(ed.get("from"), {})
        to_ent = entities.get(ed.get("to"), {})
        ed["tier"] = classify_tier_for_edge(
            from_ent.get("tier", "semantic"),
            to_ent.get("tier", "semantic"),
            ed.get("type", "")
        )

        # Source credibility
        ed["source_credibility"] = credibility_for_sources(ed.get("sources") or [])
        stats["edges_upgraded"] += 1

    return graph, stats


def run_migration(graph_path: Path) -> dict:
    """Load graph.json, migrate in place, save back."""
    if not graph_path.exists():
        return {"error": "graph.json not found"}

    g = json.loads(graph_path.read_text())
    g, stats = migrate_graph(g)

    # Backup before writing
    backup = graph_path.with_suffix(".pre_v3.json.bak")
    if not backup.exists():
        backup.write_text(json.dumps(json.loads(graph_path.read_text()), indent=2))
        stats["backup_created"] = str(backup.name)

    graph_path.write_text(json.dumps(g, indent=2))
    stats["migrated_at"] = _now()
    stats["schema_version"] = SCHEMA_VERSION
    return stats
