"""
engram.memory.signal_extractor — AI-driven extraction of actionable signals
==========================================================================

Replaces the regex-based deadline panel on the Top of Mind tab. Walks recent
email files, calls Haiku with the user's name + today's date for grounding,
and emits structured records like:

  {
    "deadline":         "2026-05-15",   # absolute ISO date the LLM resolved
    "action":           "Review draft …",
    "subject":          "Re: design doc",
    "urgency":          "high|medium|low",
    "already_responded":false,
    "is_past":          false,
    "source_path":      "MEMORY/daily/emails/...",
    "extracted_at":     "2026-05-09T...",
  }

Anything `is_past` or `already_responded` is dropped before persisting, so the
Top of Mind panel only sees live, actionable items.

Output: MEMORY/signals/deadlines.json (read by /api/top-of-mind).

Why this beats the regex pass:
  * Resolves "tomorrow" / "next Friday" against the email's actual send date,
    not request time, so a stale email's "tomorrow" never resurfaces today.
  * Reads the reply chain. If the user has already responded, the deadline
    is dropped — no more "still need to send by Monday" when you sent it
    Tuesday.
  * Skips deadlines that are already in the past (no surprise resurrections).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Email metadata helpers ───────────────────────────────────────────────────

def _parse_email_send_date(content: str, mtime_fallback: float) -> str:
    """Extract the email's send date as ISO string. Falls back to file mtime."""
    # Look for frontmatter or markdown headers like:
    #   **Date**: 2026-04-29
    #   Date: 2026-04-29
    #   Sent: ...
    patterns = [
        r"\*\*Date\*\*\s*:\s*(\d{4}-\d{2}-\d{2})",
        r"^Date\s*:\s*(\d{4}-\d{2}-\d{2})",
        r"^Sent\s*:\s*(\d{4}-\d{2}-\d{2})",
        r"\*\*Sent\*\*\s*:\s*(\d{4}-\d{2}-\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, content[:1500], re.MULTILINE)
        if m:
            return m.group(1)
    # Fallback: file mtime
    return datetime.fromtimestamp(mtime_fallback).strftime("%Y-%m-%d")


def _email_subject(filename: str, content: str) -> str:
    """Pull a clean subject string from frontmatter or filename."""
    m = re.search(r"\*\*Subject\*\*\s*:\s*(.+)$", content[:1500], re.MULTILINE)
    if m:
        return m.group(1).strip().strip('"').strip("'")[:120]
    m = re.search(r"^Subject\s*:\s*(.+)$", content[:1500], re.MULTILINE)
    if m:
        return m.group(1).strip().strip('"').strip("'")[:120]
    stem = Path(filename).stem.replace("email_", "").replace("_", " ")
    if stem.endswith(".digest"):
        stem = stem[:-len(".digest")]
    return stem[:120]


# ─── Prompt ───────────────────────────────────────────────────────────────────

_EXTRACTOR_PROMPT = """You are extracting actionable deadlines for {user_name}.

Today's date: {today}
This email was sent on: {email_date}
Email subject: {subject}

Read the email below. Decide:

1. Does this email impose a deadline or commitment that {user_name} still needs to act on? (Inbound asks, things {user_name} agreed to deliver, etc.)
2. Has {user_name} already responded in the visible thread? (Look for {user_name}'s name as sender, "I replied", "On <date> {user_name} wrote:", or a reply that closes the loop.)
3. If yes-action and not-yet-responded: when is the deadline as an ABSOLUTE ISO date (YYYY-MM-DD)? Resolve relative phrases ("tomorrow", "next Friday", "by EOD") against the EMAIL'S send date ({email_date}) — not today.
4. What is the action {user_name} needs to take, in 12 words or fewer.
5. Urgency: "high" if past-due or within 2 days, "medium" within a week, "low" further out.

Output STRICT JSON ONLY (no prose, no markdown fences):
{{
  "has_actionable_deadline": true|false,
  "deadline":           "YYYY-MM-DD" or null,
  "action":             "<short imperative>" or "",
  "already_responded":  true|false,
  "urgency":            "high"|"medium"|"low" or "",
  "confidence":         0.0-1.0
}}

Email content:
<<<
{content}
>>>

JSON:"""


# ─── Single-email extraction ──────────────────────────────────────────────────

def extract_signal_from_email(
    *,
    content:   str,
    subject:   str,
    email_date: str,
    today:      str,
    user_name:  str,
    cfg,
) -> Optional[dict]:
    """Run Haiku over one email. Returns dict or None on no-action / error.

    Trims content aggressively — Haiku doesn't need the full email, just enough
    to detect the deadline and reply state.
    """
    prompt = _EXTRACTOR_PROMPT.format(
        user_name=user_name or "the user",
        today=today,
        email_date=email_date,
        subject=subject[:140],
        content=content[:5000],
    )

    text = ""
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model      = cfg.models.haiku,
                max_tokens = 200,
                messages   = [{"role": "user", "content": prompt}],
            )
            for block in (resp.content or []):
                if getattr(block, "type", "") == "text":
                    text += getattr(block, "text", "")
        else:
            # CLI fallback (slower)
            import shutil as _shutil
            cli_bin = (getattr(getattr(cfg, "chat", None), "cli_bin", None) or None) \
                      or _shutil.which("claude")
            if not cli_bin:
                return None
            proc = subprocess.run(
                [cli_bin, "-p", prompt, "--output-format", "text", "--model", cfg.models.haiku],
                capture_output=True, text=True, timeout=20,
            )
            text = (proc.stdout or "").strip()
    except Exception:
        # Log server-side, never propagate raw exception details.
        print("[signal_extractor] LLM error:\n" + traceback.format_exc(), flush=True)
        return None

    # Tolerant JSON extraction
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return None

    if not parsed.get("has_actionable_deadline"):
        return None

    return parsed


# ─── Batch extraction over a memory tree ──────────────────────────────────────

def _is_past(date_str: str, today: str) -> bool:
    try:
        return date_str < today
    except Exception:
        return False


def extract_recent_email_signals(
    *,
    memory_path: Path,
    user_name:   str,
    cfg,
    days_back: int = 14,
    limit:     int = 80,
    today_iso: Optional[str] = None,
) -> dict:
    """Walk recent emails under memory_path/daily/emails/, extract signals.

    Returns:
      {
        "extracted_at": ISO-now,
        "today":        today_iso,
        "scanned":      <int>,
        "with_action":  <int>,
        "filtered_out": {"past": N, "already_responded": N},
        "deadlines":    [...filtered, sorted by urgency+date...],
      }

    Only emails newer than (today - days_back) are scanned, capped at `limit`
    to keep the pass under a few minutes on large inboxes.
    """
    today_iso = today_iso or datetime.now().strftime("%Y-%m-%d")
    emails_dir = memory_path / "daily" / "emails"
    if not emails_dir.exists():
        return {
            "extracted_at": datetime.now().isoformat(timespec="seconds"),
            "today":        today_iso,
            "scanned":      0,
            "with_action":  0,
            "filtered_out": {"past": 0, "already_responded": 0},
            "deadlines":    [],
        }

    cutoff = time.time() - days_back * 86400
    files: list[Path] = []
    for f in emails_dir.glob("*.md"):
        try:
            if f.stat().st_mtime >= cutoff:
                files.append(f)
        except Exception:
            continue
    files.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    files = files[:limit]

    deadlines: list[dict] = []
    n_past = 0
    n_responded = 0
    n_with_action = 0

    for f in files:
        try:
            content = f.read_text(errors="ignore")[:8000]
        except Exception:
            continue

        email_date = _parse_email_send_date(content, f.stat().st_mtime)
        subject    = _email_subject(f.name, content)

        sig = extract_signal_from_email(
            content=content,
            subject=subject,
            email_date=email_date,
            today=today_iso,
            user_name=user_name,
            cfg=cfg,
        )
        if not sig:
            continue

        n_with_action += 1

        # Filter: drop if user already responded, or deadline is in the past
        if sig.get("already_responded"):
            n_responded += 1
            continue
        deadline = sig.get("deadline")
        if deadline and _is_past(deadline, today_iso):
            n_past += 1
            continue

        deadlines.append({
            "deadline":  deadline or "",
            "subject":   subject,
            "action":    (sig.get("action") or "")[:160],
            "urgency":   sig.get("urgency") or "medium",
            "confidence": float(sig.get("confidence") or 0.0),
            "source_rel": str(f.relative_to(memory_path)),
            "email_date": email_date,
        })

    # Sort: undated last, otherwise by urgency tier then deadline asc
    urg_rank = {"high": 0, "medium": 1, "low": 2}
    deadlines.sort(key=lambda d: (
        0 if d["deadline"] else 1,
        urg_rank.get(d["urgency"], 1),
        d["deadline"] or "9999-12-31",
    ))

    return {
        "extracted_at": datetime.now().isoformat(timespec="seconds"),
        "today":        today_iso,
        "scanned":      len(files),
        "with_action":  n_with_action,
        "filtered_out": {"past": n_past, "already_responded": n_responded},
        "deadlines":    deadlines,
    }


# ─── Persistence ──────────────────────────────────────────────────────────────

SIGNALS_FILE = "signals/deadlines.json"


def save_signals(signals: dict, memory_path: Path) -> Path:
    out = memory_path / SIGNALS_FILE
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signals, indent=2))
    return out


def load_signals(memory_path: Path) -> Optional[dict]:
    p = memory_path / SIGNALS_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def signals_age_seconds(memory_path: Path) -> Optional[float]:
    p = memory_path / SIGNALS_FILE
    if not p.exists():
        return None
    try:
        return time.time() - p.stat().st_mtime
    except Exception:
        return None
