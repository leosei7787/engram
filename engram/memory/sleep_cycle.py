"""
5-phase nightly sleep cycle.

Replaces v2's single maintenance job. Mirrors hippocampal replay:

  Phase 1 (01:00) — Decay (deterministic, <2 min)
  Phase 2 (01:30) — Episodic Harvest (local LLM, <20 min)
  Phase 3 (02:30) — Graph Enrichment (local LLM, <30 min)
  Phase 4 (03:30) — Memory Compression (cloud, budget-gated)
  Phase 5 (05:00) — Contradiction Resolution + briefing (cloud, <10 min)
"""
import json
import time
from pathlib import Path
from typing import Callable, Optional
from . import (
    V3_GRAPH, V3_OPEN_QUESTIONS, V3_CONTRADICTIONS, V3_COMMUNITIES,
    V3_HEALTH, V3_AUDIT_LOG, V3_SLEEP_STATUS, V3_COST_LOG,
)
from .schemas import _now
from .decay import recompute_weights
from .tiers import expire_working_memory
from .compression import find_compression_candidates, select_for_tonight, compress_file
from .contradictions import (
    load_contradictions, save_contradictions, auto_resolve, list_pending_contradictions,
)
from .communities import louvain_communities, label_communities, save_communities
from .health import compute_health_snapshot, save_snapshot, append_audit
from .open_questions import mark_stale


def _load_graph(path: Path = V3_GRAPH) -> dict:
    if not path.exists():
        return {"entities": {}, "edges": []}
    return json.loads(path.read_text())


def _save_graph(g: dict, path: Path = V3_GRAPH):
    path.write_text(json.dumps(g, indent=2))


def _load_status() -> dict:
    if not V3_SLEEP_STATUS.exists():
        return {"history": []}
    try:
        return json.loads(V3_SLEEP_STATUS.read_text())
    except Exception:
        return {"history": []}


def _save_status(s: dict):
    V3_SLEEP_STATUS.parent.mkdir(parents=True, exist_ok=True)
    V3_SLEEP_STATUS.write_text(json.dumps(s, indent=2))


# ─── Live progress tracking (UI polls this) ──────────────────────────────
_live_progress: dict = {
    "running":           False,
    "started_at":        None,
    "current_phase":     None,
    "phase_started_at":  None,
    "phases_completed":  [],
    "total_phases":      5,
}


def get_live_progress() -> dict:
    """Snapshot of the in-flight sleep cycle (for the UI status endpoint)."""
    p = dict(_live_progress)
    p["phases_completed"] = list(_live_progress.get("phases_completed", []))
    if p.get("phase_started_at"):
        try:
            t0 = time.mktime(time.strptime(p["phase_started_at"][:19],
                                           "%Y-%m-%dT%H:%M:%S"))
            p["phase_elapsed_s"] = int(time.time() - t0)
        except Exception:
            pass
    if p.get("started_at"):
        try:
            t0 = time.mktime(time.strptime(p["started_at"][:19],
                                           "%Y-%m-%dT%H:%M:%S"))
            p["total_elapsed_s"] = int(time.time() - t0)
        except Exception:
            pass
    return p


def _start_phase(name: str, label: str):
    _live_progress["current_phase"]    = name
    _live_progress["current_label"]    = label
    _live_progress["phase_started_at"] = _now()
    print(f"[sleep] >>> {name}: {label}", flush=True)


def _end_phase(name: str, summary: dict):
    completed = _live_progress.get("phases_completed", [])
    completed.append({
        "phase":    name,
        "summary":  summary,
        "ended_at": _now(),
    })
    _live_progress["phases_completed"] = completed
    print(f"[sleep] <<< {name} done", flush=True)


# ─── Phase 1: Decay ───────────────────────────────────────────────────────
def phase1_decay(memory_dir: Path) -> dict:
    """Deterministic. Recompute weights, archive low-weight edges, expire working memory."""
    t0 = time.time()
    graph = _load_graph()
    decay_stats = recompute_weights(graph)
    moved = expire_working_memory(memory_dir, max_age_hours=48)

    # Cleanup orphan entities (no edges, low weight, not crystallised)
    edges = graph.get("edges", [])
    referenced = set()
    for e in edges:
        referenced.add(e.get("from"))
        referenced.add(e.get("to"))

    orphan_removed = 0
    keep = {}
    for eid, ent in graph.get("entities", {}).items():
        if ent.get("tier") == "crystallised":
            keep[eid] = ent
            continue
        if eid in referenced:
            keep[eid] = ent
            continue
        if float(ent.get("weight", 0.5)) < 0.1:
            orphan_removed += 1
            continue
        keep[eid] = ent
    graph["entities"] = keep

    _save_graph(graph)
    return {
        "phase": "decay",
        "duration_s": round(time.time() - t0, 1),
        "edges_updated":   decay_stats["edges_updated"],
        "edges_archived":  decay_stats["edges_archived"],
        "working_expired": moved,
        "orphans_removed": orphan_removed,
    }


# ─── Phase 2: Episodic Harvest ────────────────────────────────────────────
def phase2_episodic_harvest(
    *,
    consolidation_runner: Optional[Callable] = None,
    yield_for_chat: Optional[Callable] = None,
) -> dict:
    """
    Reuses v2 memory consolidation as the episodic harvest mechanism,
    plus open question extraction (handled inside consolidation runner).
    """
    t0 = time.time()
    if not consolidation_runner:
        return {"phase": "episodic_harvest", "skipped": True, "reason": "no_runner"}
    if yield_for_chat:
        yield_for_chat(max_wait=30, label="phase2")
    try:
        result = consolidation_runner()
    except Exception as e:
        return {"phase": "episodic_harvest", "error": str(e)}
    return {
        "phase": "episodic_harvest",
        "duration_s": round(time.time() - t0, 1),
        "consolidation": result,
    }


# ─── Phase 3: Graph Enrichment ────────────────────────────────────────────
def phase3_graph_enrichment(
    *,
    enrichment_runner: Optional[Callable] = None,
    synthesis_runner: Optional[Callable] = None,
    yield_for_chat: Optional[Callable] = None,
    run_communities: bool = True,
    community_labeller: Optional[Callable] = None,
) -> dict:
    t0 = time.time()
    out = {"phase": "graph_enrichment"}

    if enrichment_runner:
        if yield_for_chat:
            yield_for_chat(max_wait=30, label="phase3_enrich")
        try:
            out["enrichment"] = enrichment_runner()
        except Exception as e:
            out["enrichment_error"] = str(e)

    if synthesis_runner:
        if yield_for_chat:
            yield_for_chat(max_wait=30, label="phase3_synth")
        try:
            out["synthesis"] = synthesis_runner()
        except Exception as e:
            out["synthesis_error"] = str(e)

    # Community detection
    if run_communities:
        try:
            graph = _load_graph()
            communities = louvain_communities(graph)
            labelled = label_communities(communities, graph,
                                         **(community_labeller() if community_labeller else {}))
            save_communities(V3_COMMUNITIES, labelled)
            out["communities"] = {
                "count": len(labelled),
                "labels": [v["label"] for v in labelled.values()],
            }
        except Exception as e:
            out["communities_error"] = str(e)

    out["duration_s"] = round(time.time() - t0, 1)
    return out


# ─── Phase 4: Memory Compression ──────────────────────────────────────────
def phase4_compression(
    memory_dir: Path,
    *,
    claude_complete: Optional[Callable] = None,
    budget_check: Optional[Callable] = None,
    dry_run: bool = False,
) -> dict:
    t0 = time.time()
    if not claude_complete and not dry_run:
        return {"phase": "compression", "skipped": True, "reason": "no_cloud_client"}

    if budget_check:
        b = budget_check()
        if b.get("status") == "over":
            return {"phase": "compression", "skipped": True, "reason": "budget_exceeded", "budget": b}

    candidates = find_compression_candidates(memory_dir)
    selected = select_for_tonight(candidates)

    results = []
    for c in selected:
        if budget_check:
            b = budget_check()
            if b.get("status") == "over":
                results.append({**c, "status": "skipped", "reason": "budget_exceeded_mid"})
                continue
        r = compress_file(c, claude_complete=claude_complete, dry_run=dry_run)
        results.append(r)

    saved = sum(int(r.get("actual_savings", 0)) for r in results if r.get("status") == "ok")
    return {
        "phase": "compression",
        "duration_s": round(time.time() - t0, 1),
        "compressed": sum(1 for r in results if r.get("status") == "ok"),
        "skipped":   sum(1 for r in results if r.get("status") == "skipped"),
        "errors":    sum(1 for r in results if r.get("status") == "error"),
        "chars_saved": saved,
        "details": results,
    }


# ─── Phase 5: Contradiction Resolution ────────────────────────────────────
def phase5_contradiction_resolve(
    *,
    claude_complete: Optional[Callable] = None,
) -> dict:
    """
    Apply auto-resolution rules to low-severity contradictions.
    Higher-severity ones stay in inbox for user review.
    """
    t0 = time.time()
    items = load_contradictions(V3_CONTRADICTIONS)
    graph = _load_graph()
    auto_resolved = 0
    for c in items:
        if c.get("status") != "unresolved":
            continue
        ok, _rule = auto_resolve(c, graph)
        if ok:
            auto_resolved += 1
    save_contradictions(V3_CONTRADICTIONS, items)

    pending = sum(1 for c in items if c.get("status") == "unresolved")
    return {
        "phase": "contradiction_resolve",
        "duration_s": round(time.time() - t0, 1),
        "auto_resolved": auto_resolved,
        "pending":      pending,
    }


# ─── Phase 6: CLAUDE.md regeneration ────────────────────────────────────────

def phase6_core_synthesis(
    memory_dir: Path,
    *,
    claude_complete: Optional[Callable] = None,
    max_entities: int = 40,
) -> dict:
    """
    Regenerate CLAUDE.md from the current graph state.

    Synthesises:
      • Top entities by salience/confidence (people, accounts, decisions, projects)
      • Active open questions
      • Recent decisions log
      • Preserves the existing CLAUDE.md as a prior so stable facts aren't lost

    Writes to {memory_dir}/CLAUDE.md. Falls back gracefully if no Claude runner
    is available (leaves the existing file untouched).
    """
    t0 = time.time()
    output_path = memory_dir / "CLAUDE.md"

    if not claude_complete:
        return {"phase": "core_synthesis", "skipped": True,
                "reason": "no claude runner", "duration_s": 0}

    # ── Gather inputs ──────────────────────────────────────────────────────
    graph = _load_graph()
    entities = graph.get("entities", {})

    # Top entities sorted by salience × confidence
    def _score(e: dict) -> float:
        return float(e.get("salience", 0.5)) * float(e.get("confidence", 0.5))

    top_entities = sorted(entities.values(), key=_score, reverse=True)[:max_entities]
    entity_lines = []
    for e in top_entities:
        name  = e.get("name", "?")
        etype = e.get("type", "entity")
        desc  = (e.get("description") or "")[:120]
        conf  = int(float(e.get("confidence", 0.5)) * 100)
        entity_lines.append(f"- {name} ({etype}, {conf}% conf): {desc}")
    entities_block = "\n".join(entity_lines) if entity_lines else "(none)"

    # Open questions
    oq_block = ""
    try:
        if V3_OPEN_QUESTIONS.exists():
            qs = json.loads(V3_OPEN_QUESTIONS.read_text(errors="ignore"))
            qs = qs if isinstance(qs, list) else qs.get("questions", [])
            open_qs = [q for q in qs if q.get("status") not in ("resolved", "dismissed", "closed")]
            open_qs.sort(key=lambda q: -float(q.get("salience", 0.5)))
            oq_block = "\n".join(f"- {q.get('text', '')}" for q in open_qs[:12])
    except Exception:
        pass

    # Recent decisions (last 20 from decisions folder)
    decisions_block = ""
    try:
        dec_files = sorted((memory_dir / "decisions").glob("*.md"),
                           key=lambda f: f.stat().st_mtime, reverse=True)[:3]
        parts = []
        for df in dec_files:
            parts.append(df.read_text(errors="ignore")[:2000])
        decisions_block = "\n\n---\n".join(parts)
    except Exception:
        pass

    # Existing CLAUDE.md as prior
    prior = ""
    try:
        if output_path.exists():
            prior = output_path.read_text(errors="ignore")[:8000]
    except Exception:
        pass

    # ── Build synthesis prompt ─────────────────────────────────────────────
    prompt = f"""You are updating CLAUDE.md — the always-on core context file for an AI executive assistant.

CLAUDE.md is loaded into EVERY conversation. It must be compact (<120 lines), factual, and structured.

## Current graph — top entities by salience:
{entities_block}

## Open questions:
{oq_block or "(none)"}

## Recent decisions:
{decisions_block or "(none)"}

## Existing CLAUDE.md (your prior — keep stable facts, update what changed):
{prior or "(no prior — write from scratch)"}

---

Rewrite CLAUDE.md. Structure:
1. **Identity & Role** — who the user is, their mandate
2. **Active Workstreams** — top 5-8 with status and owner
3. **Key People** — key relationships, reporting lines
4. **Open Decisions** — unresolved choices with deadlines
5. **Risks & Open Questions** — top risks and unresolved questions

Rules:
- Under 120 lines
- Be specific (names, numbers, dates)
- Drop anything stale or low-salience
- No preamble — start directly with `# CLAUDE.md – ...`
- Use **bold** for names, statuses, deadlines
- Do NOT fabricate facts not present in the inputs above"""

    # ── Call Claude ────────────────────────────────────────────────────────
    try:
        result = claude_complete(prompt, max_tokens=2000)
        if result and len(result.strip()) > 100:
            output_path.write_text(result.strip(), encoding="utf-8")
            return {
                "phase":      "core_synthesis",
                "duration_s": round(time.time() - t0, 1),
                "chars":      len(result),
                "path":       str(output_path),
            }
        return {"phase": "core_synthesis", "duration_s": round(time.time() - t0, 1),
                "skipped": True, "reason": "empty response"}
    except Exception as e:
        return {"phase": "core_synthesis", "duration_s": round(time.time() - t0, 1),
                "error": str(e)}


# ─── Morning briefing ─────────────────────────────────────────────────────
def build_morning_briefing(cycle_results: list, memory_dir: Path) -> dict:
    """Aggregate cycle results into the morning briefing payload."""
    graph = _load_graph()
    snapshot = compute_health_snapshot(
        graph, memory_dir,
        contradictions_path=V3_CONTRADICTIONS,
        open_questions_path=V3_OPEN_QUESTIONS,
    )
    save_snapshot(snapshot, V3_HEALTH)

    # Collect headline numbers
    decay = next((r for r in cycle_results if r.get("phase") == "decay"), {})
    harvest = next((r for r in cycle_results if r.get("phase") == "episodic_harvest"), {})
    enrich = next((r for r in cycle_results if r.get("phase") == "graph_enrichment"), {})
    compress = next((r for r in cycle_results if r.get("phase") == "compression"), {})
    resolv = next((r for r in cycle_results if r.get("phase") == "contradiction_resolve"), {})

    pending = list_pending_contradictions(V3_CONTRADICTIONS)
    high = sum(1 for c in pending if c.get("severity") == "high")
    med = sum(1 for c in pending if c.get("severity") == "medium")

    new_oq = 0
    try:
        oq = json.loads(V3_OPEN_QUESTIONS.read_text()) if V3_OPEN_QUESTIONS.exists() else []
        new_oq = sum(1 for q in oq if q.get("status") == "open")
    except Exception:
        pass

    return {
        "generated_at": _now(),
        "documents_processed": (harvest.get("consolidation") or {}).get("files_scanned", 0),
        "memory": {
            "edges_archived":  decay.get("edges_archived", 0),
            "entities_total":  snapshot["graph_quality"]["entity_count"],
            "edges_total":     snapshot["graph_quality"]["edge_count"],
            "compressed":      compress.get("compressed", 0),
            "chars_saved":     compress.get("chars_saved", 0),
            "auto_resolved":   resolv.get("auto_resolved", 0),
        },
        "inbox": {
            "contradictions_high":   high,
            "contradictions_medium": med,
            "open_questions":        new_oq,
        },
        "communities": (enrich.get("communities") or {}).get("count", 0),
        "health": snapshot,
    }


# ─── Orchestrator ─────────────────────────────────────────────────────────
def run_sleep_cycle(
    memory_dir: Path,
    *,
    consolidation_runner: Optional[Callable] = None,
    enrichment_runner:    Optional[Callable] = None,
    synthesis_runner:     Optional[Callable] = None,
    claude_complete:      Optional[Callable] = None,
    budget_check:         Optional[Callable] = None,
    yield_for_chat:       Optional[Callable] = None,
    community_labeller:   Optional[Callable] = None,
    skip_compression:     bool = True,
    skip_communities:     bool = False,
) -> dict:
    """Run all 6 phases sequentially. Returns aggregated results."""
    t0 = time.time()
    results = []
    append_audit(V3_AUDIT_LOG, {"event": "sleep_cycle_start"})

    # Initialise live progress
    _live_progress.update({
        "running":           True,
        "started_at":        _now(),
        "current_phase":     None,
        "phase_started_at":  None,
        "phases_completed":  [],
        "total_phases":      6 if not skip_compression else 5,
    })

    try:
        # Phase 1
        _start_phase("phase1_decay", "Recomputing edge weights, archiving stale edges")
        r1 = phase1_decay(memory_dir)
        results.append(r1)
        _end_phase("phase1_decay", {
            "edges_archived": r1.get("edges_archived", 0),
            "duration_s":     r1.get("duration_s", 0),
        })
        append_audit(V3_AUDIT_LOG, {"event": "phase1_decay_done", **r1})

        # Phase 2
        if consolidation_runner:
            _start_phase("phase2_episodic", "Episodic harvest from new daily files")
            r2 = phase2_episodic_harvest(
                consolidation_runner=consolidation_runner,
                yield_for_chat=yield_for_chat,
            )
            results.append(r2)
            _end_phase("phase2_episodic", {
                "files_scanned": (r2.get("consolidation") or {}).get("files_scanned", 0),
                "proposals":     (r2.get("consolidation") or {}).get("proposals", 0),
                "duration_s":    r2.get("duration_s", 0),
            })
            append_audit(V3_AUDIT_LOG, {"event": "phase2_harvest_done", "duration_s": r2.get("duration_s")})

        # Phase 3
        _start_phase("phase3_enrichment",
                     "Graph enrichment + synthesis + community detection")
        r3 = phase3_graph_enrichment(
            enrichment_runner=enrichment_runner,
            synthesis_runner=synthesis_runner,
            yield_for_chat=yield_for_chat,
            run_communities=not skip_communities,
            community_labeller=community_labeller,
        )
        results.append(r3)
        _end_phase("phase3_enrichment", {
            "communities":  (r3.get("communities") or {}).get("count", 0),
            "duration_s":   r3.get("duration_s", 0),
        })
        append_audit(V3_AUDIT_LOG, {"event": "phase3_enrich_done", "duration_s": r3.get("duration_s")})

        # Phase 4
        if not skip_compression:
            _start_phase("phase4_compression", "Compressing aging memory files (cloud)")
            r4 = phase4_compression(
                memory_dir,
                claude_complete=claude_complete,
                budget_check=budget_check,
            )
            results.append(r4)
            _end_phase("phase4_compression", {
                "compressed":  r4.get("compressed", 0),
                "chars_saved": r4.get("chars_saved", 0),
                "duration_s":  r4.get("duration_s", 0),
            })
            append_audit(V3_AUDIT_LOG, {"event": "phase4_compress_done", "duration_s": r4.get("duration_s")})

        # Phase 5
        _start_phase("phase5_contradictions",
                     "Auto-resolve low-severity contradictions, mark stale open questions")
        r5 = phase5_contradiction_resolve(claude_complete=claude_complete)
        results.append(r5)
        # Mark stale open questions (>30 days)
        mark_stale(V3_OPEN_QUESTIONS, max_age_days=30)
        _end_phase("phase5_contradictions", {
            "auto_resolved": r5.get("auto_resolved", 0),
            "pending":       r5.get("pending", 0),
            "duration_s":    r5.get("duration_s", 0),
        })
        append_audit(V3_AUDIT_LOG, {"event": "phase5_resolve_done", "duration_s": r5.get("duration_s")})

        # Phase 6 — CLAUDE.md core synthesis
        _start_phase("phase6_core_synthesis",
                     "Regenerating CLAUDE.md from current graph state")
        r6 = phase6_core_synthesis(
            memory_dir,
            claude_complete=claude_complete,
        )
        results.append(r6)
        _end_phase("phase6_core_synthesis", {
            "chars":      r6.get("chars", 0),
            "skipped":    r6.get("skipped", False),
            "duration_s": r6.get("duration_s", 0),
        })
        append_audit(V3_AUDIT_LOG, {"event": "phase6_core_synthesis_done", **r6})

        # Morning briefing
        _start_phase("briefing", "Building morning briefing payload")
        briefing = build_morning_briefing(results, memory_dir)
        _end_phase("briefing", {})
    finally:
        _live_progress["running"]       = False
        _live_progress["current_phase"] = None

    summary = {
        "started_at": _now(),
        "duration_s": round(time.time() - t0, 1),
        "phases":     results,
        "briefing":   briefing,
    }

    status = _load_status()
    status["last_run"] = summary
    status.setdefault("history", []).append({
        "started_at": summary["started_at"],
        "duration_s": summary["duration_s"],
        "phases_completed": len(results),
    })
    status["history"] = status["history"][-30:]  # last 30 runs
    _save_status(status)

    append_audit(V3_AUDIT_LOG, {"event": "sleep_cycle_complete", "duration_s": summary["duration_s"]})
    return summary
