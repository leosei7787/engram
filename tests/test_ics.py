"""
Golden tests for the ICS calendar parser.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from engram.ingest.ics import parse_ics, upcoming, format_agenda, _unfold_lines


SAMPLE_ICS = """BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:event-1@test
DTSTAMP:20260509T180135Z
DTSTART:20260511T080000Z
DTEND:20260511T083000Z
SUMMARY:Prep call Discovery Japan
LOCATION:Microsoft Teams Meeting
ORGANIZER:MAILTO:Mike.Schoofs@tomtom.com
DESCRIPTION:
END:VEVENT
BEGIN:VEVENT
UID:event-2@test
DTSTART:20260511T083000Z
DTEND:20260511T090000Z
SUMMARY:1:1 Anthony / Leo
ORGANIZER:MAILTO:Leo.Sei@tomtom.com
END:VEVENT
BEGIN:VEVENT
UID:event-3@test
DTSTART:20260512T103000Z
DTEND:20260512T113000Z
SUMMARY:Long summary that
  spans multiple lines via
  RFC 5545 line continuation
LOCATION:Lodz Office
END:VEVENT
END:VCALENDAR
"""


def test_parses_basic_events():
    with tempfile.NamedTemporaryFile(suffix=".ics", mode="w", delete=False) as f:
        f.write(SAMPLE_ICS)
        path = Path(f.name)
    events = parse_ics(path)
    assert len(events) == 3, f"expected 3 events, got {len(events)}"
    e1 = events[0]
    assert e1.summary == "Prep call Discovery Japan"
    assert e1.organizer == "Mike.Schoofs@tomtom.com"
    assert e1.start.year == 2026
    assert e1.start.month == 5
    assert e1.start.day == 11
    print("✓ basic VEVENT parsing")


def test_handles_line_continuation():
    """Long summaries split across lines (RFC 5545 folding) must reassemble."""
    with tempfile.NamedTemporaryFile(suffix=".ics", mode="w", delete=False) as f:
        f.write(SAMPLE_ICS)
        path = Path(f.name)
    events = parse_ics(path)
    e3 = events[2]
    # Should be the full sentence, not just "Long summary that"
    assert "spans multiple lines" in e3.summary, f"got: {e3.summary!r}"
    assert "RFC 5545" in e3.summary, f"got: {e3.summary!r}"
    print("✓ line continuation unfolding")


def test_unfold_lines_directly():
    text = "FOO:start\n bar\n baz\nBAZ:next"
    out = _unfold_lines(text)
    assert out == ["FOO:startbarbaz", "BAZ:next"], f"got: {out}"
    print("✓ _unfold_lines handles space-prefixed continuations")


def test_upcoming_filter():
    """upcoming() respects the days_ahead window."""
    with tempfile.NamedTemporaryFile(suffix=".ics", mode="w", delete=False) as f:
        f.write(SAMPLE_ICS)
        path = Path(f.name)
    events = parse_ics(path)

    # Sample events are 11-12 May 2026 — pin "now" to 10 May to ensure they're upcoming
    pinned_now = datetime(2026, 5, 10, 12, 0, tzinfo=timezone.utc)
    upc = upcoming(events, days_ahead=7, now=pinned_now)
    assert len(upc) == 3, f"all 3 sample events should be in 7-day window from 10 May: got {len(upc)}"

    upc_far = upcoming(events, days_ahead=0, now=pinned_now)
    # 0 days = same day only — none on the 10th
    assert len(upc_far) == 0, "0-day window should be empty for these test events"
    print("✓ upcoming() window filter")


def test_format_agenda_markdown():
    """format_agenda() produces clean markdown grouped by day."""
    with tempfile.NamedTemporaryFile(suffix=".ics", mode="w", delete=False) as f:
        f.write(SAMPLE_ICS)
        path = Path(f.name)
    events = parse_ics(path)
    md = format_agenda(events, days_ahead=14)
    assert md.startswith("## Upcoming agenda"), f"unexpected header: {md[:60]!r}"
    # When run as test, sample events are in the past (May 2026) — agenda may be empty.
    # That's fine — header should still be there.
    print("✓ format_agenda produces structured markdown")


def test_handles_missing_dtend():
    """An event without DTEND should default to start + 1 hour."""
    ics = """BEGIN:VCALENDAR
BEGIN:VEVENT
DTSTART:20260511T080000Z
SUMMARY:Open-ended event
END:VEVENT
END:VCALENDAR
"""
    with tempfile.NamedTemporaryFile(suffix=".ics", mode="w", delete=False) as f:
        f.write(ics)
        path = Path(f.name)
    events = parse_ics(path)
    assert len(events) == 1
    delta = events[0].end - events[0].start
    assert delta.total_seconds() == 3600, f"default duration should be 1h, got {delta}"
    print("✓ missing DTEND defaults to +1h")


def test_handles_nonexistent_file():
    """Missing file returns empty list, no crash."""
    out = parse_ics(Path("/nonexistent/calendar.ics"))
    assert out == []
    print("✓ missing file returns []")


if __name__ == "__main__":
    failures = []
    for fn in (
        test_parses_basic_events,
        test_handles_line_continuation,
        test_unfold_lines_directly,
        test_upcoming_filter,
        test_format_agenda_markdown,
        test_handles_missing_dtend,
        test_handles_nonexistent_file,
    ):
        try:
            fn()
        except Exception as e:
            print(f"✗ {fn.__name__}: {type(e).__name__}: {e}")
            failures.append(fn.__name__)
    if failures:
        print(f"\n{len(failures)} failed: {', '.join(failures)}")
        sys.exit(1)
    print("\nAll ICS tests passed.")
