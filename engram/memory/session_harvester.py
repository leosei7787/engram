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
  - "fact":       a stable fact about people, projects, or accounts (e.g. "Carol now leads X")
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


# ─── Canonical name resolution ────────────────────────────────────────────────
# Haiku may emit "Dave", "Dave Johnson", or even "dave" for the
# same person. Resolve through the entity graph so all proposals about that
# person land at the same `wiki/people/<Canonical Name>.md` path.

_name_idx_cache: dict = {"mtime": 0.0, "idx": {}}


def _build_name_index(memory_path: Path) -> dict[str, str]:
    """Return {lowercase token → canonical name} for all `person` entities.

    Cached against graph.json mtime so repeated calls in one harvest don't
    reload the file.
    """
    gpath = memory_path / "graph.json"
    if not gpath.exists():
        return {}
    try:
        m = gpath.stat().st_mtime
    except Exception:
        m = 0.0
    if _name_idx_cache["idx"] and _name_idx_cache["mtime"] == m:
        return _name_idx_cache["idx"]
    try:
        graph = json.loads(gpath.read_text())
    except Exception:
        return {}
    idx: dict[str, str] = {}
    for ent_id, ent in (graph.get("entities") or {}).items():
        if ent.get("type") != "person":
            continue
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        idx[name.lower()] = name
        # Index first name + last name as fallbacks
        parts = name.split()
        if parts:
            first = parts[0].lower()
            if first and first not in idx:
                idx[first] = name
            if len(parts) > 1:
                last = parts[-1].lower()
                if last and last not in idx:
                    idx[last] = name
    _name_idx_cache["idx"] = idx
    _name_idx_cache["mtime"] = m
    return idx


def _resolve_canonical_name(subject: str, name_idx: dict[str, str]) -> str:
    """Return the canonical name for a subject string. Falls back to input."""
    if not subject:
        return subject
    s = subject.strip()
    sl = s.lower()
    if sl in name_idx:
        return name_idx[sl]
    parts = sl.split()
    if parts and parts[0] in name_idx:
        return name_idx[parts[0]]
    if len(parts) > 1 and parts[-1] in name_idx:
        return name_idx[parts[-1]]
    return s


def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", text.lower()).strip("_")[:60]


def _proposal_path_for(item: dict, name_idx: dict[str, str] | None = None) -> str:
    """Map an extracted item to the canonical destination path.

    Routing reflects the wiki/memory split:
      - Entity records (people, decisions) → wiki/<topic>/<Canonical Name>.md
        Wiki uses Title Case filenames matched by canonical name from the
        entity graph (kept in sync by the wiki ingestion scripts).
      - State files (open_questions.json) and raw input notes
        (daily/notes/chat_harvest.md) stay in MEMORY/.
    """
    kind     = item["kind"]
    subjects = item.get("subjects") or []
    if kind == "question":
        return "open_questions.json"
    if kind == "decision":
        return "wiki/decisions/chat_harvest.md"
    if subjects:
        canonical = _resolve_canonical_name(subjects[0], name_idx or {})
        return f"wiki/people/{canonical}.md"
    return "daily/notes/chat_harvest.md"


# ─── Throttle index ───────────────────────────────────────────────────────────
# Tracks per-session "last harvested" state so we run at most once per hour
# per session, only when there are new turns since the last run.

_HARVEST_INDEX = "sessions/.harvest_index.json"
_DEFAULT_INTERVAL_S = 3600   # 1 hour
_index_lock = threading.Lock()


def _harvest_index_path(memory_path: Path) -> Path:
    return memory_path / _HARVEST_INDEX


def _load_harvest_index(memory_path: Path) -> dict:
    p = _harvest_index_path(memory_path)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text())
    except Exception:
        return {}


def _save_harvest_index(memory_path: Path, idx: dict) -> None:
    p = _harvest_index_path(memory_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _index_lock:
        p.write_text(json.dumps(idx, indent=2))


def _mark_harvested(memory_path: Path, session_id: str, turn_count: int, items_added: int) -> None:
    idx = _load_harvest_index(memory_path)
    idx[_safe_id(session_id)] = {
        "last_harvested_at": datetime.now().isoformat(timespec="seconds"),
        "turn_count":        int(turn_count),
        "last_added":        int(items_added),
    }
    _save_harvest_index(memory_path, idx)


def get_harvest_status(memory_path: Path, session_id: str) -> dict:
    """Read current throttle state for a session. Used by the dashboard."""
    idx = _load_harvest_index(memory_path)
    entry = idx.get(_safe_id(session_id), {})
    last_at = entry.get("last_harvested_at")
    age = None
    if last_at:
        try:
            age = time.time() - datetime.fromisoformat(last_at).timestamp()
        except Exception:
            age = None
    return {
        "last_harvested_at": last_at,
        "age_seconds":       age,
        "turn_count":        entry.get("turn_count", 0),
        "last_added":        entry.get("last_added", 0),
    }


# ─── Session-file parser ──────────────────────────────────────────────────────
# Each turn is a "## YYYY-MM-DD HH:MM:SS" header followed by **User:** and
# **Assistant:** blocks separated by "---". This parser is paired with the
# writer in log_turn() — keep them in sync.

_TURN_HEADER_RX = re.compile(r"^##\s+(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*$", re.MULTILINE)


def _parse_session_turns(content: str) -> list[dict]:
    """Return list of {ts, user, assistant, active_context, attached} for each turn."""
    if not content:
        return []
    headers = list(_TURN_HEADER_RX.finditer(content))
    out: list[dict] = []
    for i, h in enumerate(headers):
        body_start = h.end()
        body_end = headers[i + 1].start() if i + 1 < len(headers) else len(content)
        block = content[body_start:body_end]
        # Pull **User:** … and **Assistant:** … sections.
        # User section ends at the first metadata marker (_Active context: …,
        # _Attached: …, _(Previous response …)) or the **Assistant:** header.
        u_m = re.search(
            r"\*\*User:\*\*\s*(.*?)(?=\n_(?:Active context|Attached|\(Previous response)|\n\*\*Assistant:\*\*|\Z)",
            block, re.DOTALL,
        )
        a_m = re.search(r"\*\*Assistant:\*\*\s*(.*?)(?=\n---|\Z)", block, re.DOTALL)
        if not u_m or not a_m:
            continue
        # Pull metadata lines written by log_turn()
        ctx_m = re.search(r"_Active context:\s*(.+?)_$", block, re.MULTILINE)
        att_m = re.search(r"_Attached:\s*(.+?)_$",       block, re.MULTILINE)
        active_context = []
        if ctx_m:
            active_context = [p.strip() for p in ctx_m.group(1).split(",") if p.strip()]
        attached = []
        if att_m:
            attached = [p.strip() for p in att_m.group(1).split(",") if p.strip()]
        out.append({
            "ts":             h.group(1),
            "user":           u_m.group(1).strip(),
            "assistant":      a_m.group(1).strip(),
            "active_context": active_context,
            "attached":       attached,
        })
    return out


def session_file_for(memory_path: Path, session_id: str) -> Path:
    """Public path resolver — needed by the dashboard's pin endpoints."""
    return _session_file(memory_path, session_id)


def parse_session_file(memory_path: Path, session_id: str) -> list[dict]:
    """Read + parse a session file. Returns turns or [] if missing."""
    fp = _session_file(memory_path, session_id)
    if not fp.exists():
        return []
    try:
        return _parse_session_turns(fp.read_text(errors="ignore"))
    except Exception:
        return []


# ─── Session-level harvest ────────────────────────────────────────────────────

def harvest_session(
    *,
    memory_path: Path,
    session_id:  str,
    user_name:   str,
    cfg,
    force:                bool = False,
    min_interval_seconds: int  = _DEFAULT_INTERVAL_S,
) -> dict:
    """Harvest *all* unharvested turns for a session. Returns a status dict.

    Throttle: skipped if the last harvest was less than ``min_interval_seconds``
    ago and ``force`` is False. ``force=True`` (manual button) bypasses the
    timer but still respects the turn-count check (no-op if nothing new).
    """
    fp = _session_file(memory_path, session_id)
    if not fp.exists():
        return {"ran": False, "reason": "no_session_file", "added": 0}

    try:
        content = fp.read_text(errors="ignore")
    except Exception:
        return {"ran": False, "reason": "read_error", "added": 0}

    all_turns = _parse_session_turns(content)
    if not all_turns:
        return {"ran": False, "reason": "no_turns_parsed", "added": 0}

    idx = _load_harvest_index(memory_path)
    entry = idx.get(_safe_id(session_id), {})
    last_count = int(entry.get("turn_count", 0))
    new_turns = all_turns[last_count:]

    if not new_turns:
        return {"ran": False, "reason": "no_new_turns",
                "added": 0, "turn_count": len(all_turns)}

    if not force:
        last_at = entry.get("last_harvested_at")
        if last_at:
            try:
                age = time.time() - datetime.fromisoformat(last_at).timestamp()
                if age < min_interval_seconds:
                    return {"ran": False,
                            "reason": f"throttled (last run {int(age/60)}m ago, min interval {int(min_interval_seconds/60)}m)",
                            "added": 0,
                            "new_turns": len(new_turns)}
            except Exception:
                pass

    # Run extraction over each new turn. Could batch into one Haiku call but
    # keeping per-turn so each item's provenance is unambiguous.
    name_idx = _build_name_index(memory_path)
    proposals: list[dict] = []
    for t in new_turns:
        items = extract_turn_signal(
            user_msg       = t["user"],
            assistant_text = t["assistant"],
            user_name      = user_name,
            cfg            = cfg,
        )
        for it in items:
            proposals.append({
                "path":      _proposal_path_for(it, name_idx),
                "operation": "update",
                "reason":    f"[{it['kind']}] {it['text']}",
                "salience":  max(0.3, min(1.0, it["salience"])),
            })

    added = 0
    if proposals:
        try:
            from engram.memory.proposals import add_proposals
            idx_path = memory_path / "proposals" / "index.json"
            added = add_proposals(
                idx_path, proposals,
                source=f"chat_session:{_safe_id(session_id)}",
                harvest_filename=str(fp.relative_to(memory_path)),
            )
        except Exception:
            print("[session_harvester] add_proposals error:\n" + traceback.format_exc(), flush=True)

    _mark_harvested(memory_path, session_id, len(all_turns), added)

    return {
        "ran":         True,
        "added":       added,
        "new_turns":   len(new_turns),
        "turn_count":  len(all_turns),
        "force":       force,
    }


def harvest_session_in_background(
    *,
    memory_path: Path,
    session_id:  str,
    user_name:   str,
    cfg,
    force:                bool = False,
    min_interval_seconds: int  = _DEFAULT_INTERVAL_S,
) -> threading.Thread:
    """Fire-and-forget wrapper around harvest_session for the chat path."""
    def _run():
        try:
            res = harvest_session(
                memory_path          = memory_path,
                session_id           = session_id,
                user_name            = user_name,
                cfg                  = cfg,
                force                = force,
                min_interval_seconds = min_interval_seconds,
            )
            if res.get("ran"):
                print(f"[session_harvester] {session_id}: harvested {res['new_turns']} turn(s) → {res['added']} proposal(s)", flush=True)
            else:
                print(f"[session_harvester] {session_id}: skipped — {res['reason']}", flush=True)
        except Exception:
            print("[session_harvester] background harvest error:\n" + traceback.format_exc(), flush=True)

    t = threading.Thread(target=_run, daemon=True, name="engram-session-harvest")
    t.start()
    return t


# ─── Dream-cycle integration ──────────────────────────────────────────────────
# Provides a `consolidation_runner` callable suitable for engram.memory.
# sleep_cycle.run_sleep_cycle()'s Phase 2 (Episodic Harvest). The runner
# walks every session file modified in the last `max_age_hours`, harvests
# each (force=True, no throttle — nightly batch), and returns a result dict
# in the shape the sleep cycle expects.

def make_session_consolidation_runner(
    *,
    memory_path:   Path,
    user_name:     str,
    cfg,
    max_age_hours: int = 48,
):
    """Return a `consolidation_runner` callable for the sleep cycle.

    Usage:

        from engram.memory.sleep_cycle import run_sleep_cycle
        runner = make_session_consolidation_runner(
            memory_path=cfg.memory_path,
            user_name=cfg.identity.user_name or "",
            cfg=cfg,
        )
        run_sleep_cycle(cfg.memory_path, consolidation_runner=runner)

    The runner is closure-bound so the sleep cycle can call it with no args
    and still get the right paths + config.
    """
    def _runner() -> dict:
        sessions_root = memory_path / "sessions"
        if not sessions_root.exists():
            return {"files_scanned": 0, "proposals": 0, "skipped": "no_sessions_dir"}

        cutoff = time.time() - max_age_hours * 3600
        # Walk YYYY-MM/chat_<id>.md files
        targets: list[Path] = []
        for month_dir in sessions_root.iterdir():
            if not month_dir.is_dir() or month_dir.name.startswith("."):
                continue
            for fp in month_dir.glob("chat_*.md"):
                try:
                    if fp.stat().st_mtime >= cutoff:
                        targets.append(fp)
                except Exception:
                    continue
        targets.sort(key=lambda f: f.stat().st_mtime, reverse=True)

        scanned = 0
        proposals_total = 0
        for fp in targets:
            # Recover session_id from filename (chat_<id>.md → <id>)
            m = re.match(r"^chat_(.+)$", fp.stem)
            if not m:
                continue
            session_id = m.group(1)
            try:
                res = harvest_session(
                    memory_path = memory_path,
                    session_id  = session_id,
                    user_name   = user_name,
                    cfg         = cfg,
                    force       = True,   # nightly batch overrides throttle
                )
                scanned += 1
                if res.get("ran"):
                    proposals_total += int(res.get("added") or 0)
            except Exception:
                print(f"[session_consolidation] error on {fp.name}:\n" + traceback.format_exc(), flush=True)
                continue
        return {
            "files_scanned": scanned,
            "proposals":     proposals_total,
            "max_age_hours": max_age_hours,
        }

    return _runner


# ─── Backwards-compat shim ────────────────────────────────────────────────────
# Old call sites used per-turn harvest. Route them through the throttled
# session-level path so they get the new semantics for free.

def harvest_in_background(
    *,
    memory_path: Path,
    session_id:  str,
    user_msg:    str,           # noqa: ARG001 — kept for signature compat
    assistant_text: str,        # noqa: ARG001
    user_name:   str,
    cfg,
) -> threading.Thread:
    return harvest_session_in_background(
        memory_path = memory_path,
        session_id  = session_id,
        user_name   = user_name,
        cfg         = cfg,
        force       = False,
    )
