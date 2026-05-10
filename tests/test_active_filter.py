"""
Regression test for the 'active contradictions' filter in the dashboard API.

Bug: when a user marked a contradiction as `both_true` or `both_false`, the
status was correctly saved to disk, but the same item kept reappearing in
the modal because the active filter only excluded
`resolved_A`/`resolved_B`/`dismissed`/`superseded` — not `both_*`.

This test pins the full set of "non-active" statuses so future regressions
fail loudly.
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


# All resolution statuses that mean "user has dealt with this — do NOT show again"
RESOLVED_STATUSES = (
    "resolved_A", "resolved_B",
    "both_true", "both_false",
    "dismissed", "superseded",
)


def test_filter_excludes_all_resolved_statuses():
    """
    Inspect server.py to ensure the active-contradiction filter excludes
    every resolution status, not just resolved_A/resolved_B.
    """
    server_py = (Path(__file__).parent.parent / "engram" / "dashboard" / "server.py").read_text()

    # Find lines that filter contradictions for "active" / "pending"
    filter_lines = [
        line for line in server_py.splitlines()
        if "status" in line and ("not in" in line) and ("resolved_A" in line)
    ]
    assert filter_lines, "no active-contradiction filter found in server.py"

    for line in filter_lines:
        for status in RESOLVED_STATUSES:
            assert f'"{status}"' in line, \
                f"filter line missing exclusion of {status!r}:\n  {line.strip()}"
    print(f"✓ all {len(filter_lines)} active filters exclude {len(RESOLVED_STATUSES)} resolved statuses")


def test_resolution_set_complete():
    """
    The /api/resolve-contradiction endpoint must accept every user-action
    status. (The cascade gate only allows A/B — it's intentionally narrower
    because cascading dismiss/both_* doesn't make sense.)
    """
    server_py = (Path(__file__).parent.parent / "engram" / "dashboard" / "server.py").read_text()
    # Find the validation line that mentions 'invalid resolution'
    lines = server_py.splitlines()
    for i, line in enumerate(lines):
        if 'invalid resolution' in line:
            # Validation line is one of the previous 3 lines
            for j in range(max(0, i-3), i):
                if 'resolution not in' in lines[j] and 'both_true' in lines[j]:
                    for status in ("resolved_A", "resolved_B", "both_true", "both_false", "dismissed"):
                        assert f'"{status}"' in lines[j], \
                            f"endpoint validation missing {status!r}:\n  {lines[j].strip()}"
                    print(f"✓ resolve endpoint accepts all 5 user-action statuses")
                    return
    raise AssertionError("could not find /api/resolve-contradiction validation line")


def test_record_resolution_writes_to_disk():
    """
    record_resolution() must persist to disk so future sleep cycles see it.
    Reproduces the user-reported 'nothing was saved' scenario end-to-end.
    """
    from engram.memory.contradictions import record_resolution, load_rejected_registry

    with tempfile.TemporaryDirectory() as tmp:
        reg_path = Path(tmp) / ".rejected_claims.json"
        c = {
            "id": "c_test_persist",
            "claim_A": {"statement": "Bob Smith reports_to Some Project",
                        "source": "MEMORY/daily/emails/junk.md", "weight": 0.7},
            "claim_B": {"statement": "Bob Smith reports_to Alice Chen",
                        "source": "MEMORY/CLAUDE.md", "weight": 0.95},
        }
        # User picks B
        added = record_resolution(reg_path, c, "resolved_B")
        assert added > 0, "record_resolution should report at least 1 addition"
        assert reg_path.exists(), "registry file MUST exist on disk after resolution"

        # Reload and verify persistence — fresh process would see this
        reloaded = load_rejected_registry(reg_path)
        assert len(reloaded["ground_truths"]) == 1
        assert reloaded["ground_truths"][0]["object"] == "Alice Chen"
        assert len(reloaded["rejected"]) == 1
        assert reloaded["rejected"][0]["object"] == "Some Project"

        # both_false also writes
        c2 = {
            "id": "c_test_both_false",
            "claim_A": {"statement": "X reports_to Y"},
            "claim_B": {"statement": "X reports_to Z"},
        }
        record_resolution(reg_path, c2, "both_false")
        reloaded = load_rejected_registry(reg_path)
        assert len(reloaded["rejected"]) == 3, \
            f"both_false should add 2 more rejections; got {len(reloaded['rejected'])}"

    print("✓ record_resolution persists across reload — 'nothing saved' bug fixed")


if __name__ == "__main__":
    failures = []
    for fn in (
        test_filter_excludes_all_resolved_statuses,
        test_resolution_set_complete,
        test_record_resolution_writes_to_disk,
    ):
        try:
            fn()
        except AssertionError as e:
            print(f"✗ {fn.__name__}: {e}")
            failures.append(fn.__name__)
        except Exception as e:
            print(f"✗ {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        sys.exit(1)
    print("\nAll active-filter tests passed.")
