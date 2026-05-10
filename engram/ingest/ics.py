"""
engram.ingest.ics — Zero-dependency ICS calendar parser

Parses a .ics file (RFC 5545 subset — VEVENTs only) into a clean list of
upcoming events. Designed for prompt injection: produces a compact
human-readable agenda block that fits in the system prompt without burning
context on raw ICS noise.

API:
    from engram.ingest.ics import parse_ics, format_agenda

    events = parse_ics(Path('calendar.ics'))
    # → [{'start': datetime, 'end': datetime, 'summary': str,
    #     'location': str, 'organizer': str, 'all_day': bool}, ...]

    md = format_agenda(events, days_ahead=14)
    # → markdown block: '## Upcoming agenda (next 14 days)\\n...'

Notes:
- Recurring events (RRULE) are NOT expanded — only the original DTSTART
  occurrence is returned. Most AcmeCorp-style calendars carry the actual
  occurrences as separate VEVENTs anyway.
- All datetimes are returned in the local timezone for display.
- Line continuations (space-prefixed) are handled per RFC 5545.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass
class Event:
    start:     datetime
    end:       datetime
    summary:   str             = ""
    location:  str             = ""
    organizer: str             = ""
    all_day:   bool            = False
    uid:       str             = ""
    status:    str             = ""           # CONFIRMED | TENTATIVE | CANCELLED | ""
    rrule:     str             = ""           # raw RRULE string ("" = one-off)
    attendees: list            = field(default_factory=list)   # list of email/name strings
    description: str           = ""
    raw:       dict            = field(default_factory=dict)

    @property
    def is_recurring(self) -> bool:
        return bool(self.rrule)

    @property
    def is_cancelled(self) -> bool:
        return (self.status or "").upper() == "CANCELLED"


# ─── ICS unfolding ────────────────────────────────────────────────────────────
def _unfold_lines(text: str) -> list[str]:
    """RFC 5545: a leading space/tab on a line continues the previous line."""
    out: list[str] = []
    for line in text.splitlines():
        if line.startswith((" ", "\t")) and out:
            out[-1] += line[1:]
        else:
            out.append(line.rstrip("\r"))
    return out


# ─── Datetime parsing ─────────────────────────────────────────────────────────
_DT_RE = re.compile(r"^(\d{8})(?:T(\d{6})Z?)?$")


def _parse_dt(value: str, params: dict) -> tuple[datetime, bool]:
    """
    Parse an ICS DTSTART/DTEND value. Returns (datetime, is_all_day).
    Honors VALUE=DATE for all-day, otherwise treats Z as UTC and naive as local.
    """
    is_all_day = params.get("VALUE") == "DATE" or len(value) == 8
    m = _DT_RE.match(value)
    if not m:
        # Try with TZID=...:YYYYMMDDTHHMMSS
        try:
            dt = datetime.strptime(value, "%Y%m%dT%H%M%S")
            return dt.replace(tzinfo=timezone.utc), False
        except Exception:
            return datetime.now(timezone.utc), False
    date_part, time_part = m.groups()
    if is_all_day:
        dt = datetime.strptime(date_part, "%Y%m%d")
        return dt.replace(tzinfo=timezone.utc), True
    fmt = "%Y%m%dT%H%M%S"
    dt = datetime.strptime(date_part + "T" + time_part, fmt)
    if value.endswith("Z"):
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        # No Z, no TZID — treat as UTC for safety
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, False


def _split_property(line: str) -> tuple[str, dict, str]:
    """Split 'NAME;PARAM=VAL:VALUE' into (name, params, value)."""
    if ":" not in line:
        return ("", {}, "")
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v
    return (name, params, value.strip())


# ─── Main parser ──────────────────────────────────────────────────────────────
def parse_ics(path: Path | str) -> list[Event]:
    """Parse all VEVENTs in an ICS file. Returns a list of Event dataclasses."""
    p = Path(path)
    if not p.exists():
        return []
    text = p.read_text(errors="ignore")
    lines = _unfold_lines(text)

    events: list[Event] = []
    in_event = False
    current: dict = {}

    for line in lines:
        if line == "BEGIN:VEVENT":
            in_event = True
            current = {}
            continue
        if line == "END:VEVENT":
            if "DTSTART" in current and "SUMMARY" in current:
                start, all_day = current["DTSTART"]
                end, _ = current.get("DTEND", (start + timedelta(hours=1), all_day))
                events.append(Event(
                    start=start, end=end,
                    summary=current.get("SUMMARY", ""),
                    location=current.get("LOCATION", ""),
                    organizer=current.get("ORGANIZER", ""),
                    all_day=all_day,
                    uid=current.get("UID", ""),
                    status=current.get("STATUS", ""),
                    rrule=current.get("RRULE", ""),
                    attendees=list(current.get("__attendees__", [])),
                    description=current.get("DESCRIPTION", ""),
                    raw=current,
                ))
            in_event = False
            continue
        if not in_event:
            continue
        name, params, value = _split_property(line)
        if name in ("DTSTART", "DTEND"):
            current[name] = _parse_dt(value, params)
        elif name in ("SUMMARY", "LOCATION", "DESCRIPTION", "UID", "STATUS", "RRULE"):
            # Unescape ICS-encoded chars
            v = value.replace("\\,", ",").replace("\\;", ";")
            v = v.replace("\\n", "\n").replace("\\N", "\n")
            current[name] = v.strip()
        elif name == "ORGANIZER":
            # ORGANIZER values look like "MAILTO:foo@bar.com"
            v = value.split(":", 1)[-1]
            current[name] = v.strip()
        elif name == "ATTENDEE":
            # ATTENDEE;CN=Alice Chen;ROLE=REQ-PARTICIPANT:MAILTO:alice@example.com
            # Prefer the CN (display name) when present, else the email.
            cn = (params.get("CN") or "").strip().strip('"')
            email = value.split(":", 1)[-1].strip()
            display = cn or email
            if display:
                current.setdefault("__attendees__", []).append(display)

    return events


# ─── Agenda formatter ─────────────────────────────────────────────────────────
def upcoming(events: list[Event], days_ahead: int = 14, *, now: datetime | None = None) -> list[Event]:
    """Filter to events starting within [now, now+days_ahead]. Sorted by start."""
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=days_ahead)
    # Include events that started today (in case it's still "today")
    floor = now - timedelta(hours=12)
    out = [e for e in events if floor <= e.start <= cutoff]
    out.sort(key=lambda e: e.start)
    return out


def format_agenda(events: list[Event], *, days_ahead: int = 14, max_events: int = 60) -> str:
    """
    Render upcoming events as a compact markdown agenda grouped by date.
    Designed for direct injection into the system prompt.
    """
    upc = upcoming(events, days_ahead=days_ahead)[:max_events]
    if not upc:
        return f"## Upcoming agenda (next {days_ahead} days)\n\n_(no events found)_"

    # Group by date
    from collections import OrderedDict
    by_day: OrderedDict[str, list[Event]] = OrderedDict()
    for e in upc:
        # Use local time for grouping/display
        local = e.start.astimezone()
        key = local.strftime("%a %d %b %Y")  # "Mon 11 May 2026"
        by_day.setdefault(key, []).append(e)

    lines = [f"## Upcoming agenda (next {days_ahead} days, source: ICS)"]
    for day, evs in by_day.items():
        lines.append(f"\n### {day}")
        for e in evs:
            local = e.start.astimezone()
            local_end = e.end.astimezone()
            if e.all_day:
                tline = "all-day"
            else:
                tline = local.strftime("%H:%M") + "–" + local_end.strftime("%H:%M")
            bits = [f"- **{tline}** {e.summary}"]
            if e.location and "Microsoft Teams" not in e.location and "Skype" not in e.location:
                bits.append(f"_({e.location[:50]})_")
            if e.organizer and "@" in e.organizer:
                org = e.organizer.split("@")[0]
                bits.append(f"· organiser: {org}")
            lines.append(" ".join(bits))
    return "\n".join(lines)


# ─── CLI for testing ─────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m engram.ingest.ics <calendar.ics> [days_ahead]")
        sys.exit(1)
    days = int(sys.argv[2]) if len(sys.argv) > 2 else 14
    events = parse_ics(sys.argv[1])
    print(f"Parsed {len(events)} events from {sys.argv[1]}\n")
    print(format_agenda(events, days_ahead=days))
