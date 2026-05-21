"""
engram.ingest.dedup — email-content fingerprinting for cross-batch deduplication
=================================================================================

The user's Power Automate flow uses an overlapping time window (e.g. fires
every 15 min, filters "received in the last 30 min") to ensure no email is
missed when a single run fails or a batch comes in slightly late. The cost
is that almost every email lands in OneDrive TWICE — once in the run right
after it arrives, once in the next run before the window slides past it.

We dedupe on the INGEST side instead of trying to tune the PA window:

  1. For each ingested file (a PA batch file), compute a *content* fingerprint
     for every embedded email block (inner From + Subject + body-slice).
  2. Maintain a small JSON state file at MEMORY/.ingested_email_fingerprints.json
     mapping fingerprint → first-seen metadata.
  3. When a new file lands, count how many of its email blocks are duplicates.
     If ALL blocks are dupes → skip the file entirely (still archive the
     source so PA's overlap doesn't backlog).
     If SOME blocks are new → write a stripped version with only the new
     blocks (preserves the audit chain without inflating MEMORY).

The fingerprint deliberately ignores:
  - The run-time timestamp at the top of the file (changes per PA run)
  - The cleaner's backfill-rename comment (only on backfilled files)
  - Whitespace differences

so the same email through two consecutive PA runs always hashes the same.
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── State file ───────────────────────────────────────────────────────────────

_STATE_REL = ".ingested_email_fingerprints.json"
_state_lock = threading.RLock()


def _state_path(memory_path: Path) -> Path:
    return Path(memory_path) / _STATE_REL


def load_fingerprints(memory_path: Path) -> dict:
    """Return the fingerprint state dict. Empty dict on first run or read error."""
    p = _state_path(memory_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def save_fingerprints(memory_path: Path, state: dict) -> None:
    """Atomic write — tmp + rename so a concurrent reader never sees a partial
    file (same pattern as the pinned/index.json save)."""
    p = _state_path(memory_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _state_lock:
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2))
        tmp.replace(p)


# ─── Fingerprint ──────────────────────────────────────────────────────────────
# We hash the email's *inner* From + Subject + a normalised body slice.
# That way two PA runs that capture the same email produce the same hash,
# even though the outer file's name/timestamp/backfill-comment differ.

_FROM_RX    = re.compile(r"^\*\*From:\*\*\s*(.+?)$",  re.MULTILINE)
_SUBJ_RX    = re.compile(r"^-+##\s*(.+?)$",            re.MULTILINE)
_BACKFILL_RX = re.compile(r"<!-- backfilled rename:.*?-->\s*", re.DOTALL)
_WS_RX       = re.compile(r"\s+")


def _normalise(s: str) -> str:
    """Collapse whitespace + lowercase. Deterministic and robust to cleaner
    differences between two PA runs of the same email."""
    return _WS_RX.sub(" ", s.strip().lower())


def fingerprint(content: str) -> str:
    """Compute the canonical fingerprint for an email-shaped content blob.

    Uses inner From + Subject + first 1500 chars of body (whitespace-normalised
    and lowercased) hashed with SHA-256, truncated to 20 hex chars. Same email
    via two PA runs → same fingerprint.

    Returns "" when the content doesn't look like an email at all (no From: or
    Subject: detected) — caller should treat that as "can't dedupe, write
    normally."
    """
    # Strip the backfill-rename HTML comment if present — that's PA/backfill
    # metadata, not part of the email itself.
    text = _BACKFILL_RX.sub("", content)

    from_m = _FROM_RX.search(text)
    subj_m = _SUBJ_RX.search(text)
    if not from_m and not subj_m:
        return ""

    from_v = _normalise(from_m.group(1)) if from_m else ""
    subj_v = _normalise(subj_m.group(1)) if subj_m else ""

    # Body slice: skip past the headers we already captured, take a stable
    # window. Truncating at 1500 chars dodges body variability from email
    # signatures, trackers, and the cleaner's randomly-ordered metadata.
    body_start = max(
        from_m.end() if from_m else 0,
        subj_m.end() if subj_m else 0,
    )
    body = _normalise(text[body_start:body_start + 1500])

    key = f"{from_v}|{subj_v}|{body[:1000]}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20]


# ─── Block-level split (for multi-email PA files) ───────────────────────────
# When PA's Apply-to-each + Append-to-string-variable produces a file with
# MULTIPLE outlook emails concatenated, we need block-level dedup. A new email
# starts when we see "**From:** <addr>\n**To:** <addr>\n[blank]\n----## subject"
# at column 0. (A quoted-thread email *inside* a single message starts with
# "---## subject" — note the leading dashes differ — and isn't a fresh email.)

_EMAIL_HEAD_RX = re.compile(
    r"(?m)^\*\*From:\*\*\s+\S.*?\n\*\*To:\*\*\s+\S.*?\n\s*\n----##\s+",
    re.DOTALL,
)


def split_email_blocks(content: str) -> list[str]:
    """Split a PA-output file into per-email blocks. Returns the original
    content as a single-element list if no clear multi-email boundary is found.
    """
    matches = list(_EMAIL_HEAD_RX.finditer(content))
    if len(matches) <= 1:
        return [content.strip()] if content.strip() else []
    out: list[str] = []
    for i, m in enumerate(matches):
        start = m.start()
        end   = matches[i + 1].start() if i + 1 < len(matches) else len(content)
        block = content[start:end].strip()
        if block:
            out.append(block)
    return out


# ─── High-level filter ────────────────────────────────────────────────────────

def filter_new_content(
    *,
    content:     str,
    source_name: str,
    memory_path: Path,
) -> tuple[str, dict]:
    """Return (deduped_content, info).

    ``info`` is ``{"blocks_total": int, "blocks_new": int, "blocks_dupe": int,
                   "all_duplicate": bool, "fingerprints_added": [str, ...]}``.

    Caller behaviour:
      - If ``info["all_duplicate"]`` is True: skip writing to MEMORY (still
        archive the source — PA's overlap means it'll keep arriving).
      - Otherwise: write ``deduped_content`` to MEMORY. The returned content
        contains only the new email blocks (with original framing preserved).

    Side effect: state file is updated with the new fingerprints. Caller does
    NOT need to call save_fingerprints separately.
    """
    blocks = split_email_blocks(content)
    state  = load_fingerprints(memory_path)

    kept_blocks: list[str] = []
    new_fps: list[str] = []
    dupe_count = 0
    now_iso = datetime.now().isoformat(timespec="seconds")

    for block in blocks:
        fp = fingerprint(block)
        if not fp:
            # Couldn't fingerprint (no From/Subject detected) — keep as-is,
            # better to ingest something we can't dedupe than to lose it.
            kept_blocks.append(block)
            continue
        if fp in state:
            dupe_count += 1
            continue
        kept_blocks.append(block)
        state[fp] = {"first_seen": now_iso, "source": source_name}
        new_fps.append(fp)

    if new_fps:
        # Persist only when we actually added something — avoids writing on
        # every all-dupe ingest, which would be 50%+ of runs with 30-min overlap.
        with _state_lock:
            save_fingerprints(memory_path, state)

    deduped = "\n\n".join(kept_blocks)
    info = {
        "blocks_total":       len(blocks),
        "blocks_new":         len(kept_blocks) - sum(1 for b in kept_blocks
                                                     if fingerprint(b) and fingerprint(b) not in new_fps),
        "blocks_dupe":        dupe_count,
        "all_duplicate":      (dupe_count > 0 and not new_fps),
        "fingerprints_added": new_fps,
    }
    # Refine blocks_new — the loop above double-counts; simpler:
    info["blocks_new"] = len(new_fps) + sum(1 for b in kept_blocks if not fingerprint(b))
    return deduped, info


# ─── Compaction helper ───────────────────────────────────────────────────────

def compact_state(memory_path: Path, *, max_entries: int = 5000) -> int:
    """Trim the fingerprint state to the most recent N entries to keep the
    file small. Returns the number of entries dropped. Safe to call on every
    ingest cycle — does nothing when state is below the cap."""
    state = load_fingerprints(memory_path)
    if len(state) <= max_entries:
        return 0
    # Sort by first_seen descending, keep top N
    sorted_items = sorted(
        state.items(),
        key=lambda kv: kv[1].get("first_seen", ""),
        reverse=True,
    )
    kept = dict(sorted_items[:max_entries])
    dropped = len(state) - len(kept)
    save_fingerprints(memory_path, kept)
    return dropped
