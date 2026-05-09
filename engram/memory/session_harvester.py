"""
engram.memory.session_harvester — log chat sessions + extract proposals
=======================================================================

Two responsibilities:

  1. **Logging** — append each completed chat turn (user msg, assistant
     reply, selected context paths, raw-doc names) to a per-session file
     at ``MEMORY/sessions/<YYYY-MM>/chat_<id>.md``. The file is plain
     markdown so the curator's keyword scan picks it up naturally — past
     conversations become part of future retrieval without any extra
     wiring.

  2. **Harvesting** — after each turn, run a small Haiku call over the
     just-completed exchange to extract structured signal:

        - decisions made or referenced
        - facts about people / projects / accounts
        - commitments the user made
        - open questions raised

     Each extracted item is added to ``MEMORY/proposals/index.json`` as
     a "chat_session" proposal so the existing review queue surfaces it.

The harvest runs in a background thread off the chat endpoint's response
path — chat latency is unaffected. Failures are logged and swallowed.
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


# ─── Session file logging ─────────────────────────────────────────────────────

_log_lock = threading.Lock()


def _safe_id(session_id: str) -> str:
    return re.sub(r"[^\w\-]", "_", session_id or "")[:32] or f"sess_{int(time.time())}"


def _session_file(memory_path: Path, session_id: str) -> Path:
    month_dir = memory_path / "sessions" / datetime.now().strftime("%Y-%m")
    return month_dir / f"chat_{_safe_id(session_id)}.md"


def log_turn(
    *,
    memory_path: Path,
    session_id:  str,
    user_msg:    str,
    assistant_text: str,
    selected:    list,
    raw_docs:    list,
    was_interruption: bool = False,
) -> Path:
    """Append a completed chat turn to its session file. Returns the path.

    File is created on first turn with a header. Subsequent turns are
    appended. Empty assistant text (interrupted-with-no-output) is skipped.
    """
    if not assistant_text and not was_interruption:
        return _session_file(memory_path, session_id)

    fp = _session_file(memory_path, session_id)
    fp.parent.mkdir(parents=True, exist_ok=True)
    is_new = not fp.exists()
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with _log_lock:
        with fp.open("a", encoding="utf-8") as f:
            if is_new:
                f.write(
                    f"# Chat session\n\n"
                    f"**ID:** {session_id}\n"
                    f"**Started:** {ts}\n\n"
                    f"This file is a running transcript. The curator's keyword scan "
                    f"sees it like any other memory file, so future sessions can "
                    f"retrieve and reason from past conversations.\n\n"
                    f"---\n\n"
                )
            f.write(f"## {ts}\n\n")
            f.write(f"**User:** {user_msg.strip()}\n\n")
            if selected:
                ctx_paths = [c.get("path", "") for c in selected if c.get("path")]
                if ctx_paths:
                    f.write(f"_Active context: {', '.join(ctx_paths[:6])}_\n\n")
            if raw_docs:
                names = [rd.get("name", "?") for rd in raw_docs]
                if names:
                    f.write(f"_Attached: {', '.join(names)}_\n\n")
            if was_interruption:
                f.write(f"_(Previous response was interrupted by this message.)_\n\n")
            f.write(f"**Assistant:** {assistant_text.strip()}\n\n---\n\n")
    return fp


# ─── Per-turn proposal extraction ─────────────────────────────────────────────

_HARVEST_PROMPT = """You are extracting durable signal from a chat exchange between {user_name} and an AI assistant. Read the most recent turn and return any items worth remembering across future sessions.

Categories:
  - "decision":   a choice that was made or referenced (e.g. "we'll go with option B")
  - "commitment": something {user_name} committed to do or deliver
  - "fact":       a stable fact about people, projects, or accounts (e.g. "Manuela now leads X")
  - "question":   an open question raised that has no answer yet

Avoid:
  - chit-chat or transient context
  - things already widely known (don't restate what was loaded as context)
  - speculation without grounding

Return STRICT JSON ONLY (no prose, no fences):
{{
  "items": [
    {{
      "kind":     "decision"|"commitment"|"fact"|"question",
      "text":     "<one short sentence>",
      "subjects": ["<person|project|account name>", ...],
      "salience": 0.0-1.0
    }}
  ]
}}

User name: {user_name}

User said:
<<<{user_msg}>>>

Assistant replied:
<<<{assistant_text}>>>

JSON:"""


def _haiku_call(prompt: str, cfg, *, max_tokens: int = 400, timeout: int = 18) -> str:
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
    # CLI fallback
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


def extract_turn_signal(
    *,
    user_msg:       str,
    assistant_text: str,
    user_name:      str,
    cfg,
) -> list[dict]:
    """Run Haiku over one (user, assistant) turn. Returns a list of items."""
    if not user_msg.strip() or not assistant_text.strip():
        return []
    if len(user_msg) + len(assistant_text) < 80:
        return []   # too short to mine anything stable
    prompt = _HARVEST_PROMPT.format(
        user_name=user_name or "the user",
        user_msg=user_msg[:3000],
        assistant_text=assistant_text[:6000],
    )
    try:
        text = _haiku_call(prompt, cfg)
    except Exception:
        print("[session_harvester] LLM error:\n" + traceback.format_exc(), flush=True)
        return []
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return []
    try:
        parsed = json.loads(m.group(0))
    except Exception:
        return []
    items = parsed.get("items") or []
    out: list[dict] = []
    for it in items:
        kind = it.get("kind", "")
        text = (it.get("text") or "").strip()
        if kind not in ("decision", "commitment", "fact", "question") or not text:
            continue
        out.append({
            "kind":     kind,
            "text":     text[:240],
            "subjects": [s for s in (it.get("subjects") or []) if isinstance(s, str)][:6],
            "salience": float(it.get("salience") or 0.5),
        })
    return out


def _proposal_path_for(item: dict) -> str:
    """Map an extracted item to the canonical memory path it would update."""
    kind     = item["kind"]
    subjects = item.get("subjects") or []
    if kind == "question":
        return "open_questions.json"
    if kind == "decision":
        return "decisions/chat_harvest.md"
    if subjects:
        # First subject becomes the canonical target. Crude — proposal review
        # is where the user disambiguates anyway.
        slug = re.sub(r"[^\w\-]", "_", subjects[0].lower())[:60]
        return f"context/people/{slug}.md"
    return "daily/notes/chat_harvest.md"


def harvest_turn_to_proposals(
    *,
    memory_path:  Path,
    session_id:   str,
    user_msg:     str,
    assistant_text: str,
    user_name:    str,
    cfg,
) -> int:
    """Extract signal from a turn and add proposals. Returns count added."""
    items = extract_turn_signal(
        user_msg=user_msg,
        assistant_text=assistant_text,
        user_name=user_name,
        cfg=cfg,
    )
    if not items:
        return 0

    proposals: list[dict] = []
    for it in items:
        proposals.append({
            "path":      _proposal_path_for(it),
            "operation": "update",
            "reason":    f"[{it['kind']}] {it['text']}",
            "salience":  max(0.3, min(1.0, it["salience"])),
        })

    try:
        from engram.memory.proposals import add_proposals
        idx_path = memory_path / "proposals" / "index.json"
        added = add_proposals(
            idx_path, proposals,
            source=f"chat_session:{_safe_id(session_id)}",
            harvest_filename=str(_session_file(memory_path, session_id).relative_to(memory_path)),
        )
        return added
    except Exception:
        print("[session_harvester] add_proposals error:\n" + traceback.format_exc(), flush=True)
        return 0


# ─── Background-friendly entrypoint ───────────────────────────────────────────

def harvest_in_background(
    *,
    memory_path: Path,
    session_id:  str,
    user_msg:    str,
    assistant_text: str,
    user_name:   str,
    cfg,
) -> threading.Thread:
    """Fire-and-forget the harvest call so the chat response path stays fast."""
    def _run():
        try:
            n = harvest_turn_to_proposals(
                memory_path=memory_path,
                session_id=session_id,
                user_msg=user_msg,
                assistant_text=assistant_text,
                user_name=user_name,
                cfg=cfg,
            )
            if n:
                print(f"[session_harvester] {session_id}: {n} proposal(s) queued", flush=True)
        except Exception:
            print("[session_harvester] background harvest error:\n" + traceback.format_exc(), flush=True)

    t = threading.Thread(target=_run, daemon=True, name="engram-session-harvest")
    t.start()
    return t
