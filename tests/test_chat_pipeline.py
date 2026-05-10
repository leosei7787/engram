"""
Golden test: end-to-end chat pipeline must reach 'ready' state.

Run with:
    python3 tests/test_chat_pipeline.py

This test calls the full /api/chat generator path with a realistic query
("what's coming up this week") and asserts:
  1. The pipeline emits a 'scanning' event
  2. The pipeline emits a 'ready' event with non-zero candidates within 3s
  3. No AttributeError or other exception kills the generator

It does NOT spin up the full Flask server — it imports and exercises the
generator function directly so failures show clean stack traces.

Add new assertions here when fixing chat regressions so they don't reoccur.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))


def _drive_generator(query: str, timeout_s: float = 3.0) -> tuple[list, float]:
    """
    Invoke the chat generator with a fake Flask request context.
    Returns (events, elapsed_seconds_to_ready).
    Stops after 'ready' phase to avoid waiting for the LLM stream.
    """
    from engram.dashboard import server as srv

    # Build a fake request body
    fake_body = {"messages": [{"role": "user", "content": query}]}

    with srv.app.test_request_context(
        "/api/chat", method="POST",
        json=fake_body,
    ):
        # Pull the generate() generator out of the chat() route
        # by calling chat() and inspecting the response wrapper
        resp = srv.chat()
        events: list[dict] = []
        t0 = time.time()
        elapsed_to_ready = None

        for raw in resp.response:
            text = raw.decode() if isinstance(raw, (bytes, bytearray)) else str(raw)
            for line in text.splitlines():
                if not line.startswith("data: "):
                    continue
                try:
                    payload = json.loads(line[6:])
                except Exception:
                    continue
                events.append(payload)

                phase = (payload.get("context") or {}).get("phase")
                if phase == "ready" and elapsed_to_ready is None:
                    elapsed_to_ready = time.time() - t0
                    return events, elapsed_to_ready

                if (time.time() - t0) > timeout_s:
                    raise TimeoutError(f"Chat pipeline took >{timeout_s}s without 'ready'")

        raise RuntimeError(f"Chat pipeline ended without 'ready'. Events: {events}")


def test_chat_reaches_ready_under_3s():
    events, dt = _drive_generator("what's coming up this week", timeout_s=3.0)

    phases = [(e.get("context") or {}).get("phase") for e in events if "context" in e]
    assert "scanning" in phases, f"missing 'scanning' phase. phases={phases}"
    assert "ready"    in phases, f"missing 'ready' phase. phases={phases}"

    ready_event = next(e for e in events if (e.get("context") or {}).get("phase") == "ready")
    n = ready_event["context"].get("candidates_total", 0)
    selected = ready_event["context"].get("selected", [])

    assert n > 0,                f"candidates_total should be > 0 (got {n})"
    assert len(selected) > 0,    f"selected should not be empty"
    assert dt < 3.0,             f"ready took {dt:.2f}s (target <3s)"

    print(f"✓ ready in {dt*1000:.0f}ms — {n} candidates, {len(selected)} selected")


def test_chat_handles_calendar_query():
    """Calendar queries trigger calendar_globs auto-load — make sure that path works too."""
    events, dt = _drive_generator("what meetings do I have tomorrow", timeout_s=3.0)
    phases = [(e.get("context") or {}).get("phase") for e in events]
    assert "ready" in phases, "calendar query must reach ready"
    print(f"✓ calendar query ready in {dt*1000:.0f}ms")


def test_no_attribute_errors():
    """Regression: cfg.curator was accessed without guard, killing the generator."""
    # If this raises AttributeError or KeyError, the generator died.
    events, _ = _drive_generator("amx acmetech status", timeout_s=3.0)
    assert any((e.get("context") or {}).get("phase") == "ready" for e in events), \
        "generator died before reaching 'ready'"
    print("✓ no AttributeError in pipeline")


if __name__ == "__main__":
    failures = []
    for fn in (test_chat_reaches_ready_under_3s, test_chat_handles_calendar_query, test_no_attribute_errors):
        try:
            fn()
        except Exception as e:
            print(f"✗ {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)

    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        sys.exit(1)
    print("\nAll golden tests passed.")
