"""
Golden tests for the rejected-claims registry and source filtering.

The user's manual resolutions of contradictions must persist across sleep
cycles. Future extractions from non-authoritative sources (random emails,
transcripts) must NOT be allowed to overwrite that ground truth with
nonsense like "Bob Smith reports_to Some Project".
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engram.memory.contradictions import (
    record_resolution,
    load_rejected_registry,
    purge_rejected_edges_from_graph,
    find_contradictions,
    _parse_statement,
    _is_authoritative_source,
)


def _make_contradiction(a_stmt: str, b_stmt: str) -> dict:
    return {
        "id": "test_c1",
        "type": "factual_conflict",
        "severity": "high",
        "claim_A": {"statement": a_stmt, "source": "MEMORY/test_a.md", "weight": 0.7},
        "claim_B": {"statement": b_stmt, "source": "MEMORY/test_b.md", "weight": 0.8},
    }


def test_resolved_B_records_truth_and_rejects_A():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "rejected.json"
        c = _make_contradiction("Bob Smith reports_to Some Project",
                                "Bob Smith reports_to Alice Chen")
        added = record_resolution(reg, c, "resolved_B")
        assert added == 2  # 1 truth + 1 rejection
        r = load_rejected_registry(reg)
        assert len(r["ground_truths"]) == 1
        assert r["ground_truths"][0]["object"] == "Alice Chen"
        assert len(r["rejected"]) == 1
        assert r["rejected"][0]["object"] == "Some Project"
        print("✓ resolved_B records truth + rejects losing claim")


def test_both_false_rejects_both():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "rejected.json"
        c = _make_contradiction("Bob Smith reports_to Iris Cole",
                                "Bob Smith reports_to Jay Knox")
        record_resolution(reg, c, "both_false")
        r = load_rejected_registry(reg)
        assert len(r["rejected"]) == 2
        assert len(r["ground_truths"]) == 0
        print("✓ both_false rejects both, no truth recorded")


def test_dismissed_no_change():
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / "rejected.json"
        c = _make_contradiction("X reports_to Y", "X reports_to Z")
        added = record_resolution(reg, c, "dismissed")
        assert added == 0
        print("✓ dismissed makes no registry change")


def test_purge_removes_rejected_edges():
    """An established truth should cause violating edges to be purged."""
    graph = {
        "entities": {
            "person:bob_smith":      {"name": "Bob Smith"},
            "person:alice_chen": {"name": "Alice Chen"},
            "company:lot":         {"name": "Some Project"},
            "person:tara":         {"name": "Iris Cole"},
        },
        "edges": [
            {"from": "person:bob_smith", "to": "person:alice_chen", "type": "reports_to"},
            {"from": "person:bob_smith", "to": "company:lot",         "type": "reports_to"},
            {"from": "person:bob_smith", "to": "person:tara",         "type": "reports_to"},
        ],
    }
    registry = {
        "rejected": [{"subject": "Bob Smith", "relation": "reports_to", "object": "Some Project"}],
        "ground_truths": [{"subject": "Bob Smith", "relation": "reports_to", "object": "Alice Chen"}],
    }
    purged = purge_rejected_edges_from_graph(graph, registry)
    assert purged == 2  # Some Project (rejected) + Tara (ground-truth violation)
    assert len(graph["edges"]) == 1
    assert graph["edges"][0]["to"] == "person:alice_chen"
    print(f"✓ purge removed {purged} bad edges, kept the established truth")


def test_find_contradictions_skips_non_authoritative_sources():
    """An email-sourced reports_to triple must NOT generate a contradiction."""
    graph = {"entities": {}, "edges": []}
    new_triples = [
        {"from": "Bob Smith", "to": "Some Project", "type": "reports_to",
         "confidence": 0.9, "source": "MEMORY/daily/emails/lot_polish_airlines.md"},
        {"from": "Bob Smith", "to": "Alice Chen", "type": "reports_to",
         "confidence": 0.9, "source": "MEMORY/context/people.md"},
    ]
    out = find_contradictions(new_triples, graph)
    # Both should be skipped at extraction stage by the source filter (no existing edges)
    # The first because email is non-authoritative; the second because there's nothing to conflict with.
    statements = [c.get("claim_A",{}).get("statement","") + c.get("claim_B",{}).get("statement","") for c in out]
    assert not any("LOT" in s for s in statements), \
        f"email-sourced Some Project should be filtered out. Got: {statements}"
    print("✓ source filter blocks org-relation extraction from emails")


def test_find_contradictions_respects_registry():
    """A previously rejected triple should not generate a new contradiction."""
    # Existing edge: Bob reports to Alice (the truth)
    graph = {
        "entities": {
            "leo": {"name": "Bob Smith"},
            "mike": {"name": "Alice Chen"},
            "tara": {"name": "Tara"},
        },
        "edges": [{"from": "leo", "to": "mike", "type": "reports_to"}],
    }
    # New triple says Bob reports to Tara — this would normally create a contradiction
    new_triples = [{"from": "leo", "to": "tara", "type": "reports_to",
                    "confidence": 0.9, "source": "MEMORY/context/people.md"}]

    # Without registry: produces a contradiction
    out_unfiltered = find_contradictions(new_triples, graph)
    assert len(out_unfiltered) == 1, "without registry should detect conflict"

    # With registry containing the ground truth: silently drops the new triple
    registry = {
        "ground_truths": [{"subject": "Bob Smith", "relation": "reports_to", "object": "Alice Chen"}],
        "rejected": [],
    }
    out_filtered = find_contradictions(new_triples, graph, rejected_registry=registry)
    assert len(out_filtered) == 0, "ground truth should suppress conflicting new triple"
    print("✓ ground truth suppresses contradicting future triples")


def test_authoritative_source_check():
    assert _is_authoritative_source("MEMORY/CLAUDE.md")
    assert _is_authoritative_source("MEMORY/context/people.md")
    assert _is_authoritative_source("MEMORY/decisions/2026-Q2.md")
    assert not _is_authoritative_source("MEMORY/daily/emails/lot.md")
    assert not _is_authoritative_source("MEMORY/episodic/reconsolidation_log.md")
    assert not _is_authoritative_source("")
    print("✓ source classifier identifies authoritative org files")


def test_parse_statement_handles_real_data():
    cases = [
        ("Bob Smith reports_to Alice Chen",     ("Bob Smith", "reports_to", "Alice Chen")),
        ("Karl Brown reports_to Carol Davis",
         ("Karl Brown", "reports_to", "Carol Davis")),
        ("Bob Smith manages AcmeCorp",              ("Bob Smith", "manages", "AcmeCorp")),
        ("Bob Smith reports to Some Project",      ("Bob Smith", "reports_to", "Some Project")),
    ]
    for stmt, expected in cases:
        got = _parse_statement(stmt)
        assert got == expected, f"parse({stmt!r}) → {got}, expected {expected}"
    print(f"✓ parser handles {len(cases)} real-world statements")


if __name__ == "__main__":
    failures = []
    for fn in (
        test_resolved_B_records_truth_and_rejects_A,
        test_both_false_rejects_both,
        test_dismissed_no_change,
        test_purge_removes_rejected_edges,
        test_find_contradictions_skips_non_authoritative_sources,
        test_find_contradictions_respects_registry,
        test_authoritative_source_check,
        test_parse_statement_handles_real_data,
    ):
        try:
            fn()
        except Exception as e:
            print(f"✗ {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        sys.exit(1)
    print("\nAll registry tests passed.")
