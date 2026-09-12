"""
engram.ingest.router — Route inbox files by content type to the right
MEMORY/daily/ subfolder.

Called from the dashboard's ``_ingest`` after read + cleaner + dedupe,
when deciding where in ``MEMORY/daily/`` the file should land. Prior
behaviour (everything → ``daily/emails/``) is preserved as the
fallback, so any file that doesn't declare or imply a type keeps
landing there and nothing else changes shape.

Resolution order — first match wins:

  1. YAML frontmatter ``type:`` field at the top of the file:
       - ``conversation`` → ``conversations``
       - ``document``     → ``documents``
       - ``transcript``   → ``transcripts``
       - ``email``        → ``emails``

  2. Filename prefix:
       - ``claude-session-*`` → ``conversations``
       - ``claude-doc-*``     → ``documents``

  3. Fallback → ``emails``

Downstream consumers that used to glob only ``daily/emails/*.md``
(signal extractor, tone extractor, dashboard sweep, source-credibility
classifier) should now include ``daily/conversations/`` as well —
``INGEST_CONTENT_SUBFOLDERS`` and ``ingest_scan_dirs()`` are the
canonical helpers for that.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ─── Constants ────────────────────────────────────────────────────────────────

# Well-known frontmatter ``type`` values → daily subfolder name
_TYPE_TO_SUBFOLDER: dict[str, str] = {
    "conversation": "conversations",
    "document":     "documents",
    "transcript":   "transcripts",
    "email":        "emails",
}

# Filename-prefix fallback used when there's no frontmatter (or the type
# field isn't in the known set)
_PREFIX_TO_SUBFOLDER: list[tuple[str, str]] = [
    ("claude-session-", "conversations"),
    ("claude-doc-",     "documents"),
]

# What to fall back to when neither frontmatter nor filename discriminates.
# This preserves the historical behaviour: every ingested file used to go
# to ``daily/emails/``.
_FALLBACK_SUBFOLDER = "emails"

# Subfolders under ``MEMORY/daily/`` that hold Claude-ingested first-class
# content. Consumers that used to glob ``daily/emails/*.md`` should read
# from every entry here — see ``ingest_scan_dirs()`` for the canonical
# way to obtain the concrete Path list.
#
# ``documents/`` and ``transcripts/`` are intentionally NOT in this set
# because their content shape is different (extracted-from-source and
# meeting-transcripts respectively); adding them would need a per-consumer
# call. Keep it to the two subfolders that carry user-authored / user-voiced
# first-class content: raw emails and distilled Claude conversations.
INGEST_CONTENT_SUBFOLDERS: tuple[str, ...] = ("emails", "conversations")


# ─── Frontmatter ──────────────────────────────────────────────────────────────

# Match ``type: <value>`` with optional surrounding whitespace and
# optional single- or double-quoted value. We only need one scalar
# field, not a full YAML parser — this keeps the router
# dependency-free at import time.
_TYPE_LINE_RX = re.compile(r"\s*type\s*:\s*['\"]?([\w-]+)['\"]?\s*$")


def _parse_frontmatter_type(content: str) -> Optional[str]:
    """Return the ``type`` value from a YAML frontmatter block, or None.

    Recognises the standard ``---\\n…\\n---\\n`` fence at the very top of
    the file. Only the first ~4 KB is scanned; anything larger is a
    misdeclared frontmatter block.
    """
    # Accept both LF and CRLF line endings — Windows-authored files exist.
    if not (content.startswith("---\n") or content.startswith("---\r\n")):
        return None
    head = content[:4096]
    lines = head.splitlines()
    # lines[0] is the opening ``---`` — bail if not.
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break  # closing fence — no ``type`` found before it
        m = _TYPE_LINE_RX.match(line)
        if m:
            return m.group(1).lower()
    return None


# ─── Router ───────────────────────────────────────────────────────────────────

def resolve_dest_subfolder(name: str, content: str) -> str:
    """Return the subfolder name under ``MEMORY/daily/`` for this file.

    Args:
        name:    file basename (e.g. ``claude-session-2026-09-12-foo.md``).
        content: full text of the file — we look at the top for YAML
                 frontmatter; only the ``type:`` field is consulted.

    Returns one of: ``conversations``, ``documents``, ``transcripts``,
    ``emails``. Callers should join with ``MEMORY/daily/`` themselves.
    """
    # 1) Frontmatter type wins
    t = _parse_frontmatter_type(content)
    if t and t in _TYPE_TO_SUBFOLDER:
        return _TYPE_TO_SUBFOLDER[t]

    # 2) Filename-prefix fallback
    for prefix, sub in _PREFIX_TO_SUBFOLDER:
        if name.startswith(prefix):
            return sub

    # 3) Preserve legacy behaviour — everything else → emails
    return _FALLBACK_SUBFOLDER


def ingest_scan_dirs(memory_path: Path) -> list[Path]:
    """Canonical list of first-class ingest directories under
    ``memory_path/daily/``. Callers should iterate ``.glob('*.md')`` (or
    ``.iterdir()`` and filter) across every returned Path.

    Skips subfolders that don't exist so callers don't need their own
    ``exists()`` check.
    """
    daily = memory_path / "daily"
    dirs: list[Path] = []
    for sub in INGEST_CONTENT_SUBFOLDERS:
        d = daily / sub
        if d.exists():
            dirs.append(d)
    return dirs
