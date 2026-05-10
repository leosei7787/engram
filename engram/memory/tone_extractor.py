"""
engram.memory.tone_extractor — keep MEMORY/context/tone_of_voice.md in sync
==========================================================================

Goal: any AI-drafted message from engram should sound like the user wrote
it. Two halves:

  1. **Hard rules** — user-curated. Lives at the top of the file. Things
     the model should NEVER do (em-dashes, "amazing", "circle back", etc.).
  2. **Observed patterns** — auto-updated. Lives in a delimited section
     between TONE_OBSERVATIONS_START / END comments. Each time a batch
     of outgoing emails is processed, Haiku reads them, updates the
     observations.

The file is always loaded into the chat system prompt (see server.py's
Phase 4), so the model sees both halves on every turn.

How updates fire:
  - The watcher's email-ingest path detects emails with a From: header
    matching the user's name or email.
  - Each such email schedules a debounced tone-extraction run (60s).
  - The extractor reads the most recent N user-from emails and refreshes
    the observations section in tone_of_voice.md.

Hard rules are NEVER touched by the harvester. Edit them in the Browse
tab (or any text editor) — they're explicit policy.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from typing import Optional


# ─── Default file content ─────────────────────────────────────────────────────

_DEFAULT_HARD_RULES = """# Tone of voice

Use this whenever you draft a message, email, or document on the user's behalf. The "Hard rules" below are explicit policy — never break them. The "Observed patterns" section is auto-updated from the user's actual outgoing emails.

## Hard rules (always)

- **No em-dashes.** Never use "—" or "--". Use commas, semicolons, periods, or parentheses.
- **No AI tells.** Avoid these breathless American business words and phrases: "amazing", "incredible", "fantastic", "leverage", "synergy", "unpack", "deep dive", "drill down", "circle back", "touch base", "low-hanging fruit", "best practice", "mission-critical", "value-add", "ideate", "rightsizing", "operationalize", "going forward", "at the end of the day", "to be honest", "if you will".
- **No throat-clearing openers.** Skip "I hope this finds you well", "Just wanted to reach out", "Quick question:", "Hope you're well", "Trust this finds you well".
- **No saccharine closers.** Skip "Thanks so much!", "Looking forward to hearing from you!", "Let me know your thoughts!", "Have a wonderful day!".
- **No exclamation points** unless the user uses them in the source material.
- **No emoji** unless the user explicitly asked for them.
- **No bullet-point answers** when a sentence will do. The user writes prose, not slide decks.
- **Don't moralize, don't hedge.** No "I should mention…", no "It's worth noting…", no "Of course, this depends on…".

<!-- TONE_OBSERVATIONS_START -->
## Observed patterns (auto-updated)

_(No outgoing emails analysed yet — patterns will populate after the first watcher cycle picks up an email from the user.)_
<!-- TONE_OBSERVATIONS_END -->
"""


_TONE_FILE_REL = Path("context") / "tone_of_voice.md"


def tone_file_path(memory_path: Path) -> Path:
    return memory_path / _TONE_FILE_REL


def ensure_tone_file(memory_path: Path) -> Path:
    """Create tone_of_voice.md with default hard rules if it doesn't exist."""
    fp = tone_file_path(memory_path)
    if fp.exists():
        return fp
    fp.parent.mkdir(parents=True, exist_ok=True)
    fp.write_text(_DEFAULT_HARD_RULES, encoding="utf-8")
    print(f"[tone] created {fp}", flush=True)
    return fp


# ─── Detect "this email is from the user" ─────────────────────────────────────

_FROM_RX = re.compile(r"^\s*\*?\*?From\*?\*?\s*:\s*(.+?)\s*$", re.MULTILINE | re.IGNORECASE)


def is_from_user(content: str, *, user_name: str = "", user_email: str = "") -> bool:
    """Heuristic: does the From: header point at the user?

    Matches by exact email substring first, then by all name tokens being
    present in the From: value. We're conservative — false positives would
    contaminate the observations file.
    """
    if not content or (not user_name and not user_email):
        return False
    m = _FROM_RX.search(content[:2500])
    if not m:
        return False
    sender = m.group(1).strip().lower()
    if user_email and user_email.lower() in sender:
        return True
    name_tokens = [t for t in (user_name or "").lower().split() if len(t) > 1]
    if name_tokens and all(tok in sender for tok in name_tokens):
        return True
    return False


# ─── Extraction prompt ────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """You are analysing {user_name}'s outgoing email writing style. The user has {n_emails} email samples below — these are messages {user_name} ACTUALLY WROTE. Your job is to extract durable stylistic patterns so a future AI drafting on {user_name}'s behalf can match the voice.

You must return STRICT JSON ONLY, with these keys:

{{
  "style_observations": [
    "<concrete bullets describing HOW {user_name} writes — sentence length, formality register, opener/closer patterns, vocabulary preferences, structural habits, paragraph density. Be specific. Avoid 'professional' or 'clear' — those mean nothing.>"
  ],
  "phrases_user_actually_uses": [
    "<short phrases / constructions that appear repeatedly>"
  ],
  "phrases_user_never_uses": [
    "<phrases the user demonstrably AVOIDS that AI tends to over-use, like 'circle back', 'leverage', 'touch base'>"
  ]
}}

Be terse. 5-10 items per list maximum. If you can't find something, return an empty list — don't pad.

Email samples:

{samples}

JSON:"""


# ─── Haiku call ───────────────────────────────────────────────────────────────

def _haiku_call(prompt: str, cfg, *, max_tokens: int = 800, timeout: int = 30) -> str:
    """Invoke Haiku via SDK if API key is set, else fall back to claude CLI."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model      = cfg.models.haiku,
            max_tokens = max_tokens,
            messages   = [{"role": "user", "content": prompt}],
        )
        out = ""
        for block in (resp.content or []):
            if getattr(block, "type", "") == "text":
                out += getattr(block, "text", "")
        return out
    import shutil as _shutil
    cli_bin = (getattr(getattr(cfg, "chat", None), "cli_bin", None) or None) \
              or _shutil.which("claude")
    if not cli_bin:
        return ""
    proc = subprocess.run(
        [cli_bin, "-p", prompt, "--output-format", "text", "--model", cfg.models.haiku],
        capture_output=True, text=True, timeout=timeout,
    )
    return (proc.stdout or "").strip()


# ─── Pull recent user-from emails ─────────────────────────────────────────────

def _gather_user_emails(memory_path: Path, *, user_name: str, user_email: str,
                       max_emails: int = 25, days_back: int = 30) -> list[str]:
    """Walk MEMORY/daily/emails/ for recent emails sent by the user."""
    emails_dir = memory_path / "daily" / "emails"
    if not emails_dir.exists():
        return []
    cutoff = time.time() - days_back * 86400
    candidates: list[tuple[float, str]] = []
    for f in emails_dir.glob("*.md"):
        try:
            if f.stat().st_mtime < cutoff:
                continue
            text = f.read_text(errors="ignore")[:8000]
        except Exception:
            continue
        if is_from_user(text, user_name=user_name, user_email=user_email):
            candidates.append((f.stat().st_mtime, text))
    candidates.sort(key=lambda x: x[0], reverse=True)
    return [t for _, t in candidates[:max_emails]]


# ─── Update the observations section ──────────────────────────────────────────

_OBS_BLOCK_RX = re.compile(
    r"<!--\s*TONE_OBSERVATIONS_START\s*-->.*?<!--\s*TONE_OBSERVATIONS_END\s*-->",
    re.DOTALL,
)


def _render_observations_block(parsed: dict) -> str:
    obs       = parsed.get("style_observations") or []
    uses      = parsed.get("phrases_user_actually_uses") or []
    avoids    = parsed.get("phrases_user_never_uses") or []
    ts        = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines: list[str] = [
        "<!-- TONE_OBSERVATIONS_START -->",
        "## Observed patterns (auto-updated)",
        f"_Last updated: {ts}_",
        "",
        "### Style observations",
    ]
    if obs:
        lines.extend(f"- {str(o).strip()}" for o in obs if str(o).strip())
    else:
        lines.append("_(none yet)_")
    lines += ["", "### Phrases the user actually uses"]
    if uses:
        lines.extend(f"- `{str(p).strip()}`" for p in uses if str(p).strip())
    else:
        lines.append("_(none yet)_")
    lines += ["", "### Phrases the user demonstrably avoids"]
    if avoids:
        lines.extend(f"- `{str(p).strip()}`" for p in avoids if str(p).strip())
    else:
        lines.append("_(none yet)_")
    lines.append("<!-- TONE_OBSERVATIONS_END -->")
    return "\n".join(lines)


def _splice_observations(content: str, new_block: str) -> str:
    if _OBS_BLOCK_RX.search(content):
        return _OBS_BLOCK_RX.sub(new_block, content)
    # No markers found — append at end.
    return content.rstrip() + "\n\n" + new_block + "\n"


# ─── Public entrypoint ────────────────────────────────────────────────────────

def refresh_tone_observations(
    *,
    memory_path: Path,
    user_name:   str,
    user_email:  str,
    cfg,
    days_back:   int = 30,
    max_emails:  int = 25,
) -> dict:
    """Read recent user-from emails → run Haiku → update tone_of_voice.md.

    Returns a status dict for telemetry. Safe to call repeatedly; produces
    a fresh observations block each time. Hard rules are never modified.
    """
    fp = ensure_tone_file(memory_path)

    samples = _gather_user_emails(
        memory_path,
        user_name  = user_name,
        user_email = user_email,
        max_emails = max_emails,
        days_back  = days_back,
    )
    if not samples:
        return {"updated": False, "reason": "no_user_emails", "scanned": 0}

    prompt = _EXTRACT_PROMPT.format(
        user_name = user_name or "the user",
        n_emails  = len(samples),
        samples   = "\n\n────\n\n".join(s[:1500] for s in samples),
    )

    try:
        text = _haiku_call(prompt, cfg)
    except Exception:
        print("[tone] LLM error:\n" + traceback.format_exc(), flush=True)
        return {"updated": False, "reason": "llm_error", "scanned": len(samples)}

    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return {"updated": False, "reason": "unparseable", "scanned": len(samples)}
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return {"updated": False, "reason": "json_error", "scanned": len(samples)}

    block   = _render_observations_block(parsed)
    content = fp.read_text(errors="ignore")
    new     = _splice_observations(content, block)
    fp.write_text(new, encoding="utf-8")

    n_obs    = len(parsed.get("style_observations") or [])
    n_uses   = len(parsed.get("phrases_user_actually_uses") or [])
    n_avoids = len(parsed.get("phrases_user_never_uses") or [])
    return {
        "updated":           True,
        "scanned":           len(samples),
        "style_observations": n_obs,
        "phrases_used":      n_uses,
        "phrases_avoided":   n_avoids,
        "path":              str(fp),
    }


# ─── Background-friendly wrapper (for the watcher) ────────────────────────────

_refresh_lock = threading.Lock()
_refresh_in_flight = False


def refresh_in_background(*, memory_path: Path, user_name: str, user_email: str, cfg) -> None:
    """Fire-and-forget tone refresh. Single-flight so multiple email
    arrivals in a burst collapse to one extraction run."""
    global _refresh_in_flight
    with _refresh_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def _run():
        global _refresh_in_flight
        try:
            res = refresh_tone_observations(
                memory_path = memory_path,
                user_name   = user_name,
                user_email  = user_email,
                cfg         = cfg,
            )
            if res.get("updated"):
                print(f"[tone] refreshed from {res['scanned']} emails — "
                      f"{res['style_observations']} obs, "
                      f"{res['phrases_used']} uses, "
                      f"{res['phrases_avoided']} avoids", flush=True)
            else:
                print(f"[tone] skipped: {res.get('reason')}", flush=True)
        except Exception:
            print("[tone] background error:\n" + traceback.format_exc(), flush=True)
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    threading.Thread(target=_run, daemon=True, name="engram-tone-refresh").start()
