"""
Memory Health — visibility metrics on graph quality, coverage, age.
"""
import json
import time
from pathlib import Path
from .schemas import _now, TIER_CRYSTALLISED, TIER_SEMANTIC, TIER_EPISODIC


def compute_age_distribution(memory_dir: Path) -> dict:
    """% of files in each age bucket."""
    if not memory_dir.exists():
        return {"fresh": 0, "aging": 0, "mature": 0, "ancient": 0, "total": 0}
    now_t = time.time()
    buckets = {"fresh": 0, "aging": 0, "mature": 0, "ancient": 0}
    total = 0
    for f in memory_dir.rglob("*.md"):
        try:
            age_days = (now_t - f.stat().st_mtime) / 86400
        except Exception:
            continue
        total += 1
        if age_days < 30:    buckets["fresh"]   += 1
        elif age_days < 90:  buckets["aging"]   += 1
        elif age_days < 365: buckets["mature"]  += 1
        else:                buckets["ancient"] += 1

    if total > 0:
        for k in buckets:
            buckets[k] = round(100 * buckets[k] / total)
    buckets["total"] = total
    return buckets


def compute_graph_quality(graph: dict) -> dict:
    edges = graph.get("edges", [])
    if not edges:
        return {
            "avg_edge_weight": 0.0,
            "avg_confidence":  0.0,
            "contradiction_rate": 0.0,
            "edge_count":      0,
            "entity_count":    len(graph.get("entities", {})),
        }
    weights = [float(e.get("weight", e.get("base_strength", 0.5))) for e in edges]
    confidences = [float(e.get("confidence", 0.7)) for e in edges]
    return {
        "avg_edge_weight": round(sum(weights) / len(weights), 3),
        "avg_confidence":  round(sum(confidences) / len(confidences), 3),
        "edge_count":      len(edges),
        "entity_count":    len(graph.get("entities", {})),
    }


def compute_tier_distribution(graph: dict) -> dict:
    """Count of entities per tier."""
    out = {"working": 0, "episodic": 0, "semantic": 0, "crystallised": 0}
    for ent in graph.get("entities", {}).values():
        t = ent.get("tier", "semantic")
        out[t] = out.get(t, 0) + 1
    return out


def compute_coverage_score(graph: dict, memory_dir: Path) -> dict:
    """
    Heuristic coverage: how well is the graph rooted in the file tree?

    Folders excluded from coverage scoring (intentionally not indexed):
      • archive/                 — stale content kept for audit only
      • _raw/                    — pre-digest backups
      • _pre_compression_backups — pre-compression backups
      • sessions/                — chat session transcripts
      • proposals/               — proposal index data
      • priming/                 — priming snapshots
      • health/                  — health snapshots
    """
    entities = graph.get("entities", {})
    if not entities:
        return {"score": 0, "well_covered": [], "thin": [], "blind_spots": [],
                "intentionally_excluded": []}

    EXCLUDED_DIRS = {
        "archive", "_raw", "_pre_compression_backups",
        "sessions", "proposals", "priming", "health",
    }
    def _is_excluded(rel_path: str) -> bool:
        parts = rel_path.split("/")
        # Match either MEMORY/<excluded>/... or any nested */<excluded>/*
        return any(seg in EXCLUDED_DIRS for seg in parts)

    # Files referenced by any entity source
    referenced_files = set()
    for ent in entities.values():
        for s in (ent.get("sources") or []):
            referenced_files.add(s)

    # Files in memory dir, minus excluded folders
    all_files = {
        str(f.relative_to(memory_dir.parent))
        for f in memory_dir.rglob("*.md")
        if not _is_excluded(str(f.relative_to(memory_dir.parent)))
    }

    coverage_pct = (len(referenced_files & all_files) / max(len(all_files), 1)) * 100

    # Simple well-covered / thin classification by directory
    by_dir: dict = {}
    for s in referenced_files & all_files:
        parts = s.split("/")
        if len(parts) >= 2:
            d = parts[1]
            by_dir[d] = by_dir.get(d, 0) + 1

    by_all_dir: dict = {}
    for s in all_files:
        parts = s.split("/")
        if len(parts) >= 2:
            d = parts[1]
            by_all_dir[d] = by_all_dir.get(d, 0) + 1

    well = []; thin = []; blind = []
    for d, total in by_all_dir.items():
        ref = by_dir.get(d, 0)
        ratio = ref / max(total, 1)
        if ratio >= 0.7:    well.append(d)
        elif ratio >= 0.3:  thin.append(d)
        else:               blind.append(d)

    return {
        "score":                  round(coverage_pct),
        "well_covered":           sorted(well),
        "thin":                   sorted(thin),
        "blind_spots":            sorted(blind),
        "intentionally_excluded": sorted(EXCLUDED_DIRS),
    }


def compute_volume_stats(memory_dir: Path) -> dict:
    """
    Throughput metrics — files + tokens, by age window and folder.

    'tokens' is approximated as chars/4 (Anthropic-rough rule of thumb).
    Folder breakdown lets the user see where their content is flowing.
    Skips audit/backup folders.
    """
    if not memory_dir.exists():
        return {}

    SKIP = {"sessions", "_raw", "_pre_compression_backups", "priming"}

    now_t = time.time()
    day_cutoff  = now_t - 86400
    week_cutoff = now_t - 7 * 86400

    total     = {"files": 0, "chars": 0}
    last_24h  = {"files": 0, "chars": 0}
    last_7d   = {"files": 0, "chars": 0}
    by_folder: dict = {}

    for f in memory_dir.rglob("*.md"):
        if any(seg in SKIP for seg in f.parts):
            continue
        try:
            st = f.stat()
            chars = st.st_size
            mt = st.st_mtime
        except Exception:
            continue

        total["files"] += 1
        total["chars"] += chars
        if mt > day_cutoff:
            last_24h["files"] += 1
            last_24h["chars"] += chars
        if mt > week_cutoff:
            last_7d["files"] += 1
            last_7d["chars"] += chars

        # Per-folder breakdown — group by top two path segments under MEMORY
        try:
            rel_parts = f.relative_to(memory_dir).parts
        except Exception:
            rel_parts = ()
        if len(rel_parts) == 1:
            folder = "(root)"
        elif len(rel_parts) == 2:
            folder = rel_parts[0]
        else:
            folder = f"{rel_parts[0]}/{rel_parts[1]}"
        bucket = by_folder.setdefault(folder, {
            "files": 0, "chars": 0,
            "added_24h": 0, "added_7d": 0,
        })
        bucket["files"] += 1
        bucket["chars"] += chars
        if mt > day_cutoff:  bucket["added_24h"] += 1
        if mt > week_cutoff: bucket["added_7d"]  += 1

    # Approximate tokens: chars / 4
    for d in (total, last_24h, last_7d):
        d["tokens"] = d["chars"] // 4
    for v in by_folder.values():
        v["tokens"] = v["chars"] // 4

    # Sort folders by total chars desc — biggest first
    by_folder_sorted = dict(sorted(by_folder.items(), key=lambda kv: -kv[1]["chars"]))

    return {
        "total":     total,
        "last_24h":  last_24h,
        "last_7d":   last_7d,
        "by_folder": by_folder_sorted,
    }


def compute_health_snapshot(graph: dict, memory_dir: Path,
                            contradictions_path: Path = None,
                            open_questions_path: Path = None) -> dict:
    """Aggregate snapshot — written to health/health_snapshot.json."""
    snapshot = {
        "generated_at":     _now(),
        "graph_quality":    compute_graph_quality(graph),
        "tier_distribution": compute_tier_distribution(graph),
        "age_distribution": compute_age_distribution(memory_dir),
        "coverage":         compute_coverage_score(graph, memory_dir),
        "volume":           compute_volume_stats(memory_dir),
    }

    # Contradiction rate
    if contradictions_path and contradictions_path.exists():
        try:
            cs = json.loads(contradictions_path.read_text())
            edges = max(snapshot["graph_quality"]["edge_count"], 1)
            unresolved = sum(1 for c in cs if c.get("status") == "unresolved")
            snapshot["graph_quality"]["contradiction_rate"] = round(100 * unresolved / edges, 2)
            snapshot["contradictions_unresolved"] = unresolved
            snapshot["contradictions_total"] = len(cs)
        except Exception:
            pass

    # Open questions
    if open_questions_path and open_questions_path.exists():
        try:
            qs = json.loads(open_questions_path.read_text())
            snapshot["open_questions"] = {
                "open":     sum(1 for q in qs if q.get("status") == "open"),
                "answered": sum(1 for q in qs if q.get("status") == "answered"),
                "stale":    sum(1 for q in qs if q.get("status") == "stale"),
            }
        except Exception:
            pass

    return snapshot


def save_snapshot(snapshot: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2))


def append_audit(audit_path: Path, event: dict):
    """Append a JSON line to the audit log."""
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    event["ts"] = _now()
    with audit_path.open("a") as f:
        f.write(json.dumps(event) + "\n")
