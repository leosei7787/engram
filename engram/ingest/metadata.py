"""
engram.ingest.metadata — semantic filename builders for ingested content
=========================================================================

The curator's keyword scan weights filename-stem hits 3× (see
``retrieval.keyword.filename_match``). Generic source filenames like
``Recent_email_<timestamp>.md`` carry zero searchable signal, so files land in
memory but never surface in retrieval. These helpers extract real metadata
from the file body and produce a descriptive stem instead.

Used by:
  - ``engram.dashboard.server._ingest``  (live, at watcher pickup)
  - ``scripts/backfill_rename.py``        (one-shot, for already-landed files)

Both paths share this module so the live and backfilled names stay identical.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Slug helpers ─────────────────────────────────────────────────────────────

def slugify(s: str, *, max_len: int = 60) -> str:
    """Filename-safe slug — alnum + dash + underscore, capped at ``max_len``."""
    if not s:
        return ""
    s = re.sub(r"[^\w\s\-]", "", s)
    s = re.sub(r"\s+", "_", s.strip())
    return s[:max_len].strip("_")


def _date_from_mtime(mtime: Optional[float]) -> str:
    ts = mtime if mtime is not None else datetime.now().timestamp()
    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _person_name_from_email(addr: str) -> str:
    """``Foo.Bar@x.com`` → ``Foo_Bar``. Handles names with display prefix too."""
    if not addr:
        return ""
    m = re.search(r"([\w.\-]+)@", addr)
    local = (m.group(1) if m else addr).strip()
    return slugify(local.replace(".", " ").replace("-", " "), max_len=40)


# ─── Email ────────────────────────────────────────────────────────────────────
# We parse the FIRST `**Subject:**` if present; otherwise the first `## ...`
# section header that the email-cleaner writes for each thread top. Sender is
# the first `**From:**` line.

_EMAIL_SUBJECT_RX = re.compile(r"^\*\*Subject:\*\*\s*(.+?)$",      re.MULTILINE)
_EMAIL_HASH_SUBJ  = re.compile(r"^-+##\s*(.+?)$",                  re.MULTILINE)
_EMAIL_FROM_RX    = re.compile(r"^\*\*From:\*\*\s*(.+?)$",         re.MULTILINE)


def parse_email_metadata(text: str) -> dict:
    """Return {subject, from_email, from_name}. Best-effort; never raises."""
    subject = ""
    m = _EMAIL_SUBJECT_RX.search(text)
    if m:
        subject = m.group(1).strip()
    else:
        m = _EMAIL_HASH_SUBJ.search(text)
        if m:
            subject = m.group(1).strip()

    from_email = ""
    from_name  = ""
    m = _EMAIL_FROM_RX.search(text)
    if m:
        from_email = m.group(1).strip()
        from_name  = _person_name_from_email(from_email)

    return {"subject": subject, "from_email": from_email, "from_name": from_name}


def email_filename(text: str, *, source_name: str = "", mtime: Optional[float] = None) -> str:
    """Descriptive stem for an email file (no extension).

    Pattern: ``email_<YYYY-MM-DD>__<subject>__from_<sender>``
    Falls back to date-only if the body has no usable metadata.
    """
    meta = parse_email_metadata(text)
    # Prefer date embedded in source name (matches the email's send date when
    # the source is a Recent_email_<ISO> file); fall back to mtime.
    date_part = ""
    if source_name:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", source_name)
        if m:
            date_part = m.group(1)
    if not date_part:
        date_part = _date_from_mtime(mtime)

    subj   = slugify(meta["subject"], max_len=70) if meta["subject"] else "no_subject"
    sender = meta["from_name"] or "unknown"
    return f"email_{date_part}__{subj}__from_{sender}"


# ─── Slack ────────────────────────────────────────────────────────────────────
# A single slack extract usually digests many threads. We pull the top 2 H3
# topic headers and the 3 most-bolded participants (excluding the user) as the
# filename signal.

_SLACK_H3_RX     = re.compile(r"^###\s+(.+?)$",                            re.MULTILINE)
_SLACK_BOLD_NAME = re.compile(r"\*\*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\*\*")


def parse_slack_metadata(text: str, *, exclude_names: tuple[str, ...] = ("leo",)) -> dict:
    """Return {topics, participants}. ``exclude_names`` filters self-mentions."""
    topics: list[str] = []
    for m in _SLACK_H3_RX.finditer(text):
        title = m.group(1).strip()
        # Strip "— Decision reached" tails and "1. " prefixes — they're decoration
        title = re.sub(r"\s*[—\-]\s*.*$", "", title)
        title = re.sub(r"^\d+\.\s*", "", title)
        if title:
            topics.append(title)
        if len(topics) >= 2:
            break

    counter = Counter(_SLACK_BOLD_NAME.findall(text))
    excl = {n.lower() for n in exclude_names}
    participants = [
        name.split()[0]
        for name, _ in counter.most_common(8)
        if not any(e in name.lower() for e in excl) and len(name.split()) <= 3
    ][:3]
    return {"topics": topics, "participants": participants}


def slack_filename(text: str, *, source_name: str = "", mtime: Optional[float] = None,
                   exclude_names: tuple[str, ...] = ("leo",)) -> str:
    """Descriptive stem for a slack extract (no extension).

    Pattern: ``slack_<YYYY-MM-DD>__<topic1>__<topic2>__with_<people>``
    """
    meta = parse_slack_metadata(text, exclude_names=exclude_names)
    # Date: prefer source name; fall back to mtime
    date_part = ""
    if source_name:
        m = re.search(r"(\d{4}-?\d{2}-?\d{2})", source_name)
        if m:
            raw = m.group(1)
            date_part = raw if "-" in raw else f"{raw[:4]}-{raw[4:6]}-{raw[6:8]}"
    if not date_part:
        date_part = _date_from_mtime(mtime)

    topic = slugify(meta["topics"][0], max_len=50) if meta["topics"] else "digest"
    if len(meta["topics"]) > 1:
        topic = f"{topic}__{slugify(meta['topics'][1], max_len=40)}"
    people = "_".join(meta["participants"][:3]) or "general"
    return f"slack_{date_part}__{topic}__with_{people}"


# ─── Calendar events ─────────────────────────────────────────────────────────
# Each ICS event gets exploded into one markdown file under
# MEMORY/calendar/<YYYY-MM>/meeting_<date>_<time>__<summary>__with_<orgs>.md
# so the keyword scan can hit on summary, organiser, and attendee names.

def ics_event_filename(event) -> str:
    """Stem for one calendar event. ``event`` is an engram.ingest.ics.Event."""
    if not event or event.is_cancelled:
        return ""
    try:
        date_part = event.start.strftime("%Y-%m-%d")
        time_part = event.start.strftime("%H%M") if not event.all_day else "allday"
    except Exception:
        date_part = _date_from_mtime(None)
        time_part = "0000"

    summary = slugify(event.summary, max_len=60) or "untitled"
    # Pick up to 3 distinguishing names from organizer + attendees.
    names: list[str] = []
    if event.organizer:
        n = _person_name_from_email(event.organizer)
        if n: names.append(n)
    for a in (event.attendees or [])[:8]:
        n = _person_name_from_email(a)
        if n and n not in names:
            names.append(n)
        if len(names) >= 3:
            break
    with_part = "_".join(names) if names else "solo"
    return f"meeting_{date_part}_{time_part}__{summary}__with_{with_part}"


def ics_event_markdown(event) -> str:
    """Render one Event as a standalone markdown file with all useful fields.

    The keyword scanner reads the body too — putting summary, organiser, and
    every attendee name in the body lets retrieval surface this event for
    queries about any of those people, not just the event title.
    """
    if not event:
        return ""
    when_line = ""
    try:
        if event.all_day:
            when_line = event.start.strftime("All day · %A %d %b %Y")
        else:
            when_line = (event.start.strftime("%A %d %b %Y · %H:%M")
                         + event.end.strftime("–%H:%M"))
    except Exception:
        when_line = "(time unparseable)"

    lines = [
        f"# {event.summary or 'Untitled meeting'}",
        "",
        f"**When:** {when_line}",
    ]
    if event.location:
        lines.append(f"**Location:** {event.location}")
    if event.organizer:
        lines.append(f"**Organizer:** {event.organizer}")
    if event.attendees:
        lines.append(f"**Attendees:** {', '.join(event.attendees[:30])}")
    if event.status:
        lines.append(f"**Status:** {event.status}")
    if event.rrule:
        lines.append(f"**Recurrence:** {event.rrule}")
    if event.description:
        lines.append("")
        lines.append("## Description")
        lines.append(event.description.strip()[:4000])
    return "\n".join(lines) + "\n"


# ─── Shape detection ──────────────────────────────────────────────────────────
# The dashboard's _ingest needs to know which builder to use. Filename and
# the file's first few KB both contribute — slack files have a distinctive
# "# Slack Signals" / "# Slack Extract" header.

def detect_shape(source_name: str, text_head: str) -> str:
    """Return one of: 'email', 'slack', 'ics', 'unknown'."""
    n = (source_name or "").lower()
    if n.endswith(".ics"):
        return "ics"
    if "slack" in n:
        return "slack"
    if re.match(r"^\s*#\s+slack\s+(signals|extract|message|digest)",
                text_head[:500], re.IGNORECASE | re.MULTILINE):
        return "slack"
    if "**From:**" in text_head[:2000] or re.search(r"^\s*From:\s", text_head[:2000], re.MULTILINE):
        return "email"
    return "unknown"
