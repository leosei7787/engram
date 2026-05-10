"""
engram.dashboard.server — Standalone engram web app
====================================================

Pages:
  /          → Chat with live context assembly (default)
  /health    → Engram Health — three-pillar stats

API:
  POST /api/chat          → SSE: memory scan + Claude streaming response
  GET  /api/stats         → live three-pillar stats JSON
  GET  /api/config        → public config info
  GET  /api/file          → raw file content for preview
  POST /api/export        → generate DOCX / PPTX / MD / PDF and download
  GET  /api/outputs       → list recent files in the outputs folder

Usage:
    python3 engram/dashboard/server.py
    ENGRAM_PORT=7090 ANTHROPIC_API_KEY=sk-... python3 engram/dashboard/server.py
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import traceback
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flask import Flask, jsonify, request, Response, stream_with_context
from engram.retrieval.config import load_config, EngramConfig
from engram.retrieval.pipeline import memory_scan
from engram.retrieval.curator import build_candidates, curate_context, monitor_context, detect_drift
from engram.ingest.watcher import InboxWatcher
from engram.ingest.cleaner import EmailCleaner
from engram.ingest.ics import parse_ics, format_agenda, upcoming as ics_upcoming
from engram.memory.proposals import load_index as load_proposals_index

app = Flask(__name__)
_cfg: EngramConfig | None = None

# ─── In-flight chat state ─────────────────────────────────────────────────────
# Tracks the single in-flight chat response so /api/chat/interject can:
#   1. read the in-flight question + partial response (for the classifier)
#   2. signal abort (the streamer checks the Event between chunks)
#   3. terminate the CLI subprocess if one is running
# engram is single-user-per-server, so a singleton is sufficient.
_chat_lock  = threading.Lock()
_chat_state: dict = {
    "active":     False,
    "abort":      threading.Event(),     # replaced per chat
    "user_msg":   "",
    "partial":    [],                    # list of token strings
    "started_at": 0.0,
    "proc":       None,                  # subprocess.Popen (CLI backend)
}


def _begin_chat(user_msg: str) -> threading.Event:
    """Register an in-flight chat. Returns the abort event the streamer should poll."""
    with _chat_lock:
        # Aggressively abort any prior chat (defensive; shouldn't happen normally).
        _chat_state["abort"].set()
        prev_proc = _chat_state.get("proc")
        if prev_proc is not None:
            try: prev_proc.terminate()
            except Exception: pass

        new_evt = threading.Event()
        _chat_state.update({
            "active":     True,
            "abort":      new_evt,
            "user_msg":   user_msg,
            "partial":    [],
            "started_at": time.time(),
            "proc":       None,
        })
        return new_evt


def _end_chat() -> None:
    with _chat_lock:
        _chat_state["active"] = False
        _chat_state["proc"]   = None


def _append_partial(text: str) -> None:
    with _chat_lock:
        if _chat_state["active"]:
            _chat_state["partial"].append(text)


def _set_chat_proc(proc) -> None:
    with _chat_lock:
        _chat_state["proc"] = proc


def _snapshot_inflight() -> tuple[bool, str, str]:
    """Return (active, user_msg, partial_text) for the classifier."""
    with _chat_lock:
        if not _chat_state["active"]:
            return (False, "", "")
        return (True, _chat_state["user_msg"], "".join(_chat_state["partial"]))


def _signal_pivot() -> None:
    """Set the abort event and terminate any running CLI subprocess."""
    with _chat_lock:
        _chat_state["abort"].set()
        proc = _chat_state["proc"]
    if proc is not None:
        try: proc.terminate()
        except Exception: pass


# ─── Watcher state ────────────────────────────────────────────────────────────
_watcher: InboxWatcher | None = None
_watcher_thread: threading.Thread | None = None
_watcher_status: dict = {
    "running": False,
    "inbox":   None,
    "last_scan": None,
    "files_seen": 0,
    "files_new":  0,
}


def get_cfg() -> EngramConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


def _start_watcher(cfg: EngramConfig):
    """Start inbox watcher as a background daemon thread."""
    global _watcher, _watcher_thread, _watcher_status

    inbox = (
        getattr(cfg, "inbox_src", None)
        or (cfg.paths.inbox_src if hasattr(cfg, "paths") else None)
    )
    if not inbox:
        return

    inbox_path = Path(inbox)
    if not inbox_path.exists():
        print(f"[watcher] inbox does not exist yet: {inbox_path}", flush=True)
        return

    _watcher_status["inbox"] = str(inbox_path)

    cleaner = EmailCleaner(skip_marketing=True)

    def _ingest(path: Path, content: str) -> bool:
        """
        Cleanse and ingest an inbox file.

        Pipeline:
          1. Skip placeholder files (<200 chars).
          2. For .eml/.html/.md files: strip HTML/CSS noise, detect marketing.
             Marketing emails are silently dropped (no copy, mark as done).
          3. Write the cleaned markdown into memory/daily/emails/.
        """
        if len(content.strip()) < 200:
            return True  # placeholder

        cleaned_md: str = content
        is_marketing  = False
        skip_reason   = ""
        original_size = len(content)

        # Apply cleaner for email-shaped formats
        ext = path.suffix.lower()
        if ext in (".eml", ".html", ".htm", ".md") and "<html" in content.lower()[:8000] \
           or ext == ".eml" or "From:" in content[:2000]:
            res = cleaner.clean(content, filename=path.name)
            is_marketing = res.is_marketing
            skip_reason  = res.skip_reason
            if is_marketing:
                # Don't copy — log and mark done so we don't re-process
                _watcher_status.setdefault("skipped", 0)
                _watcher_status["skipped"] += 1
                _watcher_status.setdefault("skipped_recent", [])
                _watcher_status["skipped_recent"].insert(0, {
                    "name":   path.name[:80],
                    "reason": skip_reason,
                    "at":     datetime.now(timezone.utc).isoformat(),
                })
                _watcher_status["skipped_recent"] = _watcher_status["skipped_recent"][:20]
                return True
            if res.cleaned_chars > 50:
                cleaned_md = res.to_markdown()

        dest_dir = cfg.memory_path / "daily" / "emails"
        dest_dir.mkdir(parents=True, exist_ok=True)
        safe_name = re.sub(r"[^\w.\-]", "_", path.name)[:120]
        # If we cleaned the content, save as .md (not the original .eml/.html)
        if cleaned_md is not content:
            safe_name = re.sub(r"\.(eml|html?|txt)$", ".md", safe_name, flags=re.IGNORECASE)
            if not safe_name.endswith(".md"):
                safe_name += ".md"
        dest = dest_dir / safe_name

        try:
            if cleaned_md is content:
                import shutil as _shutil
                _shutil.copy2(str(path), str(dest))
            else:
                dest.write_text(cleaned_md, encoding="utf-8")
                # Preserve the source mtime so recent-activity sorts by when
                # the email actually arrived, not when we cleaned it.
                try:
                    src_mtime = path.stat().st_mtime
                    os.utime(dest, (src_mtime, src_mtime))
                except Exception:
                    pass

            _watcher_status["files_new"] += 1
            _watcher_status["last_scan"] = datetime.now(timezone.utc).isoformat()
            _watcher_status.setdefault("recent_files", [])
            _watcher_status["recent_files"].insert(0, {
                "name":     path.name[:80],
                "size":     len(cleaned_md),
                "original": original_size,
                "stripped_pct": int(100 * (1 - len(cleaned_md) / max(original_size, 1))),
                "at":       datetime.now(timezone.utc).isoformat(),
            })
            _watcher_status["recent_files"] = _watcher_status["recent_files"][:20]

            # New email landed in MEMORY/daily/emails/ — schedule a debounced
            # deadline-extraction pass so the Top of Mind tile stays current
            # without manual refresh. Multiple emails arriving in a burst
            # collapse into a single LLM run.
            try:
                _schedule_signals_refresh(cfg, delay_s=30)
            except Exception:
                pass
            return True
        except Exception as e:
            print(f"[watcher] copy error: {e}", flush=True)
            return False

    _watcher = InboxWatcher(
        inbox_path       = inbox_path,
        memory_path      = cfg.memory_path,
        claude_bin       = str(cfg.paths.claude_bin) if cfg.paths.claude_bin else None,
        model            = cfg.models.haiku,
        interval_seconds = 60,
        extensions       = {".md", ".txt", ".eml", ".vtt", ".html"},
        on_new_file      = _ingest,
    )

    def _run():
        global _watcher_status
        _watcher_status["running"] = True
        # First pass: count files already seen
        _watcher_status["last_scan"] = datetime.now(timezone.utc).isoformat()
        _watcher.run()
        _watcher_status["running"] = False

    _watcher_thread = threading.Thread(target=_run, daemon=True, name="engram-watcher")
    _watcher_thread.start()
    print(f"[watcher] started — watching {inbox_path}", flush=True)


# ─── Sleep-cycle scheduler ────────────────────────────────────────────────────
# Background thread that watches the wall clock and fires run_sleep_cycle()
# once a day at the time configured in cfg.sleep_cycle.schedule (default
# "02:00"). State persists across server restarts via a small JSON file so
# the cycle doesn't double-fire if you bounce the dashboard.
_sleep_thread:    threading.Thread | None = None
_sleep_lock = threading.Lock()
_sleep_state: dict = {
    "running":         False,    # actively executing right now
    "last_run_date":   None,     # YYYY-MM-DD of last completed run
    "last_run_at":     None,     # ISO timestamp
    "last_result":     None,     # summary dict from run_sleep_cycle
    "next_due":        None,     # ISO timestamp of next scheduled fire
    "schedule":        "02:00",
    "manual_pending":  False,    # operator triggered via /api/sleep-cycle/trigger
}


def _sleep_state_path(memory_path: Path) -> Path:
    return memory_path / ".sleep_scheduler_state.json"


def _load_sleep_state(memory_path: Path) -> None:
    p = _sleep_state_path(memory_path)
    if not p.exists():
        return
    try:
        data = json.loads(p.read_text())
        for k in ("last_run_date", "last_run_at", "last_result"):
            if k in data:
                _sleep_state[k] = data[k]
    except Exception:
        pass


def _save_sleep_state(memory_path: Path) -> None:
    p = _sleep_state_path(memory_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        snapshot = {k: _sleep_state.get(k) for k in ("last_run_date", "last_run_at", "last_result")}
        p.write_text(json.dumps(snapshot, indent=2))
    except Exception:
        pass


def _parse_schedule(s: str) -> tuple[int, int]:
    """Parse 'HH:MM' (24h). Falls back to 02:00 on bad input."""
    try:
        h, m = s.split(":")
        return max(0, min(23, int(h))), max(0, min(59, int(m)))
    except Exception:
        return (2, 0)


def _next_due(schedule_str: str, last_run_date: str | None, now: datetime | None = None) -> datetime:
    """When should the cycle fire next?"""
    now = now or datetime.now()
    h, m = _parse_schedule(schedule_str)
    today_at = now.replace(hour=h, minute=m, second=0, microsecond=0)
    today = now.strftime("%Y-%m-%d")
    # If we haven't run today and we're past today's scheduled time → due now
    if last_run_date != today and now >= today_at:
        return now
    # Otherwise: today's slot if not yet reached, else tomorrow's
    if now < today_at:
        return today_at
    return today_at + timedelta(days=1)


def _run_sleep_cycle_now(cfg: "EngramConfig", *, manual: bool = False) -> None:
    """Run the cycle synchronously in the calling thread; persist state."""
    with _sleep_lock:
        if _sleep_state["running"]:
            print("[sleep] skipping — already running", flush=True)
            return
        _sleep_state["running"] = True

    try:
        # Imports inside the try block — if a downstream module fails to load,
        # we log + clear `running` rather than crashing the scheduler thread.
        from engram.memory             import sleep_cycle as _sc
        from engram.memory.session_harvester import make_session_consolidation_runner

        # engram.memory.__init__ resolves V3_* / *_FILE constants from the
        # ENGRAM_MEMORY_PATH env var at import time. The dashboard doesn't
        # set that env var (it's config-driven), so the constants would
        # otherwise point at a stale default. Re-bind them to the live
        # cfg.memory_path so every status / audit / graph write lands where
        # the rest of the dashboard expects.
        mp = cfg.memory_path
        _sc.V3_GRAPH          = mp / "graph.json"
        _sc.V3_OPEN_QUESTIONS = mp / "open_questions.json"
        _sc.V3_CONTRADICTIONS = mp / "contradictions.json"
        _sc.V3_COMMUNITIES    = mp / "communities.json"
        _sc.V3_HEALTH         = mp / "health" / "health_snapshot.json"
        _sc.V3_AUDIT_LOG      = mp / "health" / "audit_log.jsonl"
        _sc.V3_SLEEP_STATUS   = mp / ".sleep_cycle_status.json"
        _sc.V3_COST_LOG       = mp / "health" / "cost_log.jsonl"

        run_sleep_cycle = _sc.run_sleep_cycle

        runner = make_session_consolidation_runner(
            memory_path   = cfg.memory_path,
            user_name     = cfg.identity.user_name or "",
            cfg           = cfg,
            max_age_hours = 48,
        )
        print(f"[sleep] cycle starting (manual={manual}, "
              f"schedule={_sleep_state['schedule']})", flush=True)
        t0 = time.time()
        summary = run_sleep_cycle(
            cfg.memory_path,
            consolidation_runner = runner,
            skip_compression     = True,
        )
        elapsed = time.time() - t0
        print(f"[sleep] cycle done in {elapsed:.1f}s", flush=True)

        # Capture a compact summary for the status endpoint
        cons = (summary.get("consolidation") or {})
        result = {
            "duration_s":    round(elapsed, 1),
            "phases_run":    len(summary.get("phases") or []),
            "files_scanned": cons.get("files_scanned", 0),
            "proposals":     cons.get("proposals", 0),
        }

        with _sleep_lock:
            _sleep_state["last_run_date"] = datetime.now().strftime("%Y-%m-%d")
            _sleep_state["last_run_at"]   = datetime.now().isoformat(timespec="seconds")
            _sleep_state["last_result"]   = result
        _save_sleep_state(cfg.memory_path)
    except Exception:
        print("[sleep] cycle error:\n" + traceback.format_exc(), flush=True)
    finally:
        with _sleep_lock:
            _sleep_state["running"] = False


def _start_sleep_scheduler(cfg: "EngramConfig") -> None:
    """Start the background scheduler thread. Idempotent."""
    global _sleep_thread

    sleep_cfg = getattr(cfg, "sleep_cycle", None)
    if sleep_cfg and not getattr(sleep_cfg, "enabled", True):
        print("[sleep] scheduler disabled in config — skipping", flush=True)
        return

    schedule_str = getattr(sleep_cfg, "schedule", "02:00") if sleep_cfg else "02:00"
    _sleep_state["schedule"] = schedule_str
    _load_sleep_state(cfg.memory_path)

    if _sleep_thread and _sleep_thread.is_alive():
        return

    def _loop() -> None:
        # Sleep-then-check pattern. Wake every 60s; cheap.
        while True:
            try:
                with _sleep_lock:
                    last_date = _sleep_state["last_run_date"]
                    manual    = _sleep_state.get("manual_pending", False)
                    if manual:
                        _sleep_state["manual_pending"] = False
                due = _next_due(_sleep_state["schedule"], last_date)
                now = datetime.now()
                with _sleep_lock:
                    _sleep_state["next_due"] = due.isoformat(timespec="seconds")
                if manual or now >= due:
                    _run_sleep_cycle_now(cfg, manual=manual)
            except Exception:
                print("[sleep] scheduler loop error:\n" + traceback.format_exc(), flush=True)
            time.sleep(60)

    _sleep_thread = threading.Thread(target=_loop, daemon=True, name="engram-sleep-scheduler")
    _sleep_thread.start()
    print(f"[sleep] scheduler started — fires daily at {schedule_str}", flush=True)


@app.route("/api/sleep-cycle/status")
def sleep_cycle_status():
    with _sleep_lock:
        snapshot = dict(_sleep_state)
    return jsonify(snapshot)


@app.route("/api/sleep-cycle/trigger", methods=["POST"])
def sleep_cycle_trigger():
    """Run the cycle now (manual override). Returns immediately; the work
    happens in the scheduler thread on its next 60s tick.
    """
    with _sleep_lock:
        if _sleep_state["running"]:
            return jsonify({"status": "already_running"})
        _sleep_state["manual_pending"] = True
    return jsonify({"status": "queued"})


# ─── Inbox save (chat uploads also enter the long-term pipeline) ──────────────
# Files / pasted text submitted via the chat sidebar modal are normally
# ephemeral (live only in the browser's rawDocs array, gone on tab close).
# To make them part of long-term knowledge, we drop a copy into the watcher's
# inbox folder — the next watcher cycle (≤60s) ingests it through the full
# pipeline (cleaner, redactor, future dream-cycle extraction).

def _save_to_inbox(filename: str, data: bytes) -> Path | None:
    """Save bytes to the inbox folder for watcher pickup.

    Returns the path written, or None if no inbox is configured / save failed.
    Filename is sanitised and timestamped to avoid collisions; the watcher
    de-dupes by content hash anyway.
    """
    cfg = get_cfg()
    inbox = getattr(getattr(cfg, "paths", None), "inbox_src", None)
    if not inbox:
        return None
    inbox_path = Path(inbox).expanduser()
    if not inbox_path.exists():
        return None
    stem = Path(filename).stem
    ext  = Path(filename).suffix or ".md"
    safe_stem = re.sub(r"[^\w\-]", "_", stem)[:80] or "chat_upload"
    ts = time.strftime("%Y%m%d_%H%M%S")
    out = inbox_path / f"chat_upload_{ts}_{safe_stem}{ext}"
    try:
        out.write_bytes(data)
        print(f"[chat/upload] saved to inbox for pipeline ingest: {out.name}", flush=True)
        return out
    except Exception as e:
        print(f"[chat/upload] inbox save failed: {e}", flush=True)
        return None


# ─── Claude backend helpers ───────────────────────────────────────────────────

def _format_messages_for_cli(messages: list) -> str:
    """
    Flatten a multi-turn message list into a single string for the Claude CLI.
    The CLI -p flag is single-turn; we embed history as context so the model
    can see prior turns.
    """
    if len(messages) == 1:
        return messages[0].get("content", "")

    parts: list[str] = []
    for m in messages[:-1]:
        role = "Human" if m["role"] == "user" else "Assistant"
        parts.append(f"{role}: {m.get('content', '')}")
    history = "\n\n".join(parts)
    current = messages[-1].get("content", "")
    return f"[Conversation history]\n{history}\n\n[Current question]\n{current}"


def _stream_cli(messages: list, system_prompt: str, cli_bin: str, model: str | None = None,
                abort: threading.Event | None = None):
    """
    Call Claude CLI in print mode and yield response text in small chunks.

    Uses `claude -p <msg> --output-format stream-json --verbose [--system-prompt ...] [--model ...]`.

    The CLI in -p mode returns batched NDJSON events (not incremental token deltas).
    We parse the `assistant` message event and emit the text in ~10-word chunks so
    the typing-indicator UX stays responsive. Falls back to `result` event if needed.

    If `abort` is provided and gets set mid-stream, the subprocess is terminated
    and the generator stops (used by /api/chat/interject for human-like pivots).

    Yields plain text strings (no SSE framing — caller wraps in data: ... \\n\\n).
    """
    user_msg = _format_messages_for_cli(messages)

    # subprocess.Popen rejects any string containing \x00 with "embedded null
    # byte". These show up when upstream PDF/Word extractors mangle ligatures
    # (fl/fi → \x00). Strip defensively so a single bad source file can't
    # take down the chat. Tracked count goes to the log for visibility.
    def _strip_nulls(s: str) -> tuple[str, int]:
        if not s or "\x00" not in s:
            return (s, 0)
        return (s.replace("\x00", ""), s.count("\x00"))

    user_msg, n_user_nulls   = _strip_nulls(user_msg)
    system_prompt, n_sys_nulls = _strip_nulls(system_prompt)
    if n_user_nulls or n_sys_nulls:
        print(f"[chat/cli] stripped null bytes: user={n_user_nulls} system={n_sys_nulls}", flush=True)

    cmd = [cli_bin, "-p", user_msg, "--output-format", "stream-json", "--verbose"]
    if system_prompt:
        cmd += ["--system-prompt", system_prompt]
    if model:
        cmd += ["--model", model]

    print(
        f"[chat/cli] {cli_bin} -p <{len(user_msg)}chars>"
        f"{' --system-prompt <'+str(len(system_prompt))+'chars>' if system_prompt else ''}",
        flush=True,
    )

    def _emit_text(text: str):
        """Yield text in ~10-word chunks so the cursor appears to stream."""
        if not text:
            return
        words = text.split(" ")
        chunk: list[str] = []
        for word in words:
            chunk.append(word)
            if len(chunk) >= 10:
                yield " ".join(chunk) + " "
                chunk = []
        if chunk:
            yield " ".join(chunk)

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        # Register so /api/chat/interject can terminate us on a pivot.
        _set_chat_proc(proc)

        full_text   = ""
        result_text = ""
        aborted     = False

        for line in proc.stdout:
            if abort is not None and abort.is_set():
                aborted = True
                try: proc.terminate()
                except Exception: pass
                break
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                etype = event.get("type", "")

                # True streaming (future CLI versions or different modes)
                if etype == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        yield delta.get("text", "")

                # Batched assistant message (current CLI -p --verbose behavior)
                elif etype == "assistant":
                    msg = event.get("message", {})
                    for block in (msg.get("content") or []):
                        if block.get("type") == "text":
                            full_text += block.get("text", "")

                # Final result event (fallback)
                elif etype == "result":
                    result_text = event.get("result", "") or ""

            except json.JSONDecodeError:
                pass

        if aborted:
            # Caller will emit its own "interrupted" notice; just stop quietly.
            return

        proc.wait()
        if proc.returncode not in (0, None):
            err = (proc.stderr.read() if proc.stderr else "").strip()
            if err:
                print(f"[chat/cli] exit {proc.returncode}: {err[:300]}", flush=True)
                yield f"\n\n⚠ CLI error (exit {proc.returncode}): {err[:200]}"
                return

        # Emit the batched text in word-chunks
        text = full_text or result_text
        if text:
            yield from _emit_text(text)

    except Exception as e:
        yield f"\n\n⚠ CLI error: {e}"


# ─── Chat API ─────────────────────────────────────────────────────────────────

@app.route("/api/chat", methods=["POST"])
def chat():
    data     = request.get_json(force=True) or {}
    messages = data.get("messages", [])
    query    = (messages[-1].get("content", "") if messages else "").strip()
    if not query:
        return jsonify({"error": "empty query"}), 400

    was_interruption = bool(data.get("was_interruption", False))
    interruption_reason = (data.get("interruption_reason", "") or "").strip()[:200]
    session_id = (data.get("session_id", "") or "").strip()

    def generate():
        cfg = get_cfg()

        # Register this chat as in-flight so /api/chat/interject can read its
        # state and signal a pivot. The returned event is checked by the
        # streamer between chunks.
        abort_evt = _begin_chat(query)

        # ── Phase 0: Announce scanning ────────────────────────────────────────
        yield f"data: {json.dumps({'context': {'phase': 'scanning', 'candidates_total': 0, 'selected': [], 'reasoning': ''}})}\n\n"

        # ── Phase 1: Wide memory scan (up to 40 direct + graph + wiki) ────────
        try:
            scan = memory_scan(query, cfg, max_files=40)
        except Exception as e:
            scan = {"direct": [], "graph": [], "wiki": [], "graph_context": "", "suggestions": []}
            print(f"[chat] memory_scan error: {e}", flush=True)

        # ── Phase 2: Build candidate list with snippets ───────────────────────
        try:
            all_candidates = build_candidates(scan, cfg, snippet_chars=400)
        except Exception as e:
            all_candidates = []
            print(f"[chat] build_candidates error: {e}", flush=True)

        n_candidates = len(all_candidates)

        # ── Phase 3: Immediate top-N selection — no Haiku wait ───────────────────
        # Feed all top candidates directly. Haiku refines AFTER the response
        # (see Phase 6) so it never blocks time-to-first-token.
        curator_cfg = getattr(cfg, 'curator', None)
        max_ctx = getattr(curator_cfg, 'max_context_files', 20) if curator_cfg else 20
        selected  = all_candidates[:max_ctx]
        reasoning = f"Top {len(selected)} of {n_candidates} by relevance score"

        sel_payload = [{"path": c["path"], "type": c["type"]} for c in selected]
        yield f"data: {json.dumps({'context': {'phase': 'ready', 'candidates_total': n_candidates, 'selected': sel_payload, 'reasoning': reasoning}})}\n\n"

        # ── Phase 4: Build system prompt from selected files ──────────────────
        system_parts: list[str] = []
        id_cfg = cfg.identity
        if id_cfg.user_name or id_cfg.user_role:
            system_parts.append(
                f"You are an AI assistant for {id_cfg.user_name}"
                + (f", {id_cfg.user_role}" if id_cfg.user_role else "")
                + (f" at {id_cfg.org_name}" if id_cfg.org_name else "") + "."
            )
        sp = cfg.system_prompt
        if sp.user_tone:
            system_parts.append(f"Tone: {sp.user_tone}")

        # ── Interruption note ────────────────────────────────────────────────
        # When the previous response was cut off by a new user message, give
        # the model a short preamble so its reply reads like a human reacting:
        # "ah, you're right — let me reconsider…" rather than a flat restart.
        if was_interruption:
            note = (
                "The user just interrupted your previous response with this new message. "
                "Acknowledge the interruption naturally in your opening (e.g. 'Ah, you're right —' "
                "or 'Got it, let me switch tracks —') and then address their new direction. "
                "Don't apologise excessively or over-explain the switch."
            )
            if interruption_reason:
                note += f" Classifier reason for the pivot: {interruption_reason}"
            system_parts.append(note)

        base   = cfg.base_path
        budget = cfg.retrieval.context_budget.max_total_chars
        trunc  = cfg.retrieval.context_budget.wiki_page_truncate_chars
        used   = 0

        # ── Always-load files (CLAUDE.md, preferences…) ──────────────────────
        # Prepended before dynamic context — always in window regardless of query.
        always_paths = list(getattr(cfg.system_prompt, "always_load", None) or [])
        for rel in always_paths:
            p = Path(rel) if Path(rel).is_absolute() else base / rel
            if not p.exists():
                continue
            try:
                content = p.read_text(errors="ignore")[:20000]
                system_parts.append(f"\n\n---\n# {p.name} (always loaded)\n\n{content}")
                used += len(content)
            except Exception:
                pass

        # ── Calendar auto-load ────────────────────────────────────────────────
        # Two-phase calendar loading:
        #   1. ICS file (authoritative — actual event source from Outlook/Google).
        #      Searched first; in inbox_src or memory_path. Parsed into a clean
        #      agenda so the model sees structured events, not raw ICS noise.
        #   2. Markdown calendar fallback (legacy hand-curated calendar*.md files).
        #      Only loaded if no ICS was found.
        cal_patterns = list(getattr(cfg.system_prompt, "calendar_globs", None) or ["calendar*.md"])

        # Phase 1: ICS — search inbox + memory for the most-recent .ics
        ics_search_dirs: list[Path] = []
        try:
            inbox = getattr(cfg.paths, "inbox_src", None) if hasattr(cfg, "paths") else None
            if inbox and Path(inbox).exists():
                ics_search_dirs.append(Path(inbox))
        except Exception:
            pass
        ics_search_dirs.append(base)

        ics_hits: list[Path] = []
        for d in ics_search_dirs:
            try:
                # Skip files under _processed/ — those are archived after watcher
                # ingest and would otherwise shadow the live calendar if their
                # mtime got touched.
                ics_hits.extend(p for p in d.rglob("*.ics") if "_processed" not in p.parts)
            except Exception:
                continue

        ics_loaded = False
        if ics_hits and used < budget:
            ics_file = max(ics_hits, key=lambda f: f.stat().st_mtime)
            try:
                events = parse_ics(ics_file)
                if events:
                    # Adaptive window: longer agenda when query is calendar-related,
                    # short focused window otherwise (keeps prompt small for general queries).
                    qlow = query.lower()
                    is_cal_query = any(kw in qlow for kw in (
                        "week", "tomorrow", "today", "agenda", "calendar", "schedule",
                        "meeting", "next month", "upcoming", "1:1", "1on1", "free time",
                        "available", "busy", "block",
                    ))
                    days_ahead = 14 if is_cal_query else 7
                    # Generous event budget — at 5-10 events/day, a 14-day
                    # window can easily hit 100+. Truncating by event count
                    # silently drops later days (e.g. Friday afternoon vanishes
                    # while Monday is fully shown). The char cap below is the
                    # real budget guard.
                    max_events = 300 if is_cal_query else 60
                    agenda = format_agenda(events, days_ahead=days_ahead, max_events=max_events)
                    # Hard cap to keep TTFT reasonable — bigger prompt = slower first token
                    agenda_cap = 14000 if is_cal_query else 4500
                    agenda = agenda[:agenda_cap]
                    today = datetime.now().strftime("%A %d %b %Y")
                    system_parts.append(
                        f"\n\n---\n# Upcoming calendar — {ics_file.name} (parsed ICS, next {days_ahead}d)\n"
                        f"Today is {today}. This agenda is the **authoritative** event source "
                        f"(parsed directly from your Outlook/Google ICS). "
                        f"Use it for any time, scheduling, or 'what's coming' question — "
                        f"do NOT request additional calendar context.\n\n"
                        f"{agenda}"
                    )
                    used += len(agenda)
                    ics_loaded = True
            except Exception as e:
                print(f"[chat] ICS parse error: {e}", flush=True)

        # Phase 2: Markdown calendar fallback (only if no ICS was loaded)
        if not ics_loaded:
            cal_hits: list[Path] = []
            for pat in cal_patterns:
                cal_hits.extend(base.rglob(pat))
            if cal_hits and used < budget:
                cal_file = max(cal_hits, key=lambda f: f.stat().st_mtime)
                try:
                    cal_content = cal_file.read_text(errors="ignore")[:8000]
                    system_parts.append(f"\n\n---\n# Upcoming calendar ({cal_file.name})\n\n{cal_content}")
                    used += len(cal_content)
                except Exception:
                    pass

        for c in selected:
            if used >= budget:
                break
            p = Path(c["path"])
            if not p.is_absolute():
                p = base / c["path"]
            if not p.exists():
                continue
            try:
                content = p.read_text(errors="ignore")
                if c["type"] == "wiki":
                    cap   = min(trunc, budget - used)
                    label = f"Wiki: {p.stem}"
                else:
                    cap   = min(5000, budget - used)
                    label = f"Context: {c['path']}"
                if len(content) > cap:
                    content = content[:cap] + "\n…(truncated)"
                system_parts.append(f"\n\n---\n# {label}\n\n{content}")
                used += len(content)
            except Exception:
                pass

        if scan.get("graph_context"):
            system_parts.append("\n\n" + scan["graph_context"])

        # ── request_more_context tool instruction ─────────────────────────────
        # Be reluctant — every REQUEST_CONTEXT spawns a 2nd CLI subprocess (~2s).
        # The user already has 20 candidate files + CLAUDE.md + calendar loaded.
        # Only use this for genuinely missing specifics (a number from a report,
        # a quote from an unloaded email, etc.). Never for calendar/agenda
        # questions or general status — those are covered.
        system_parts.append(
            "\n\n---\n"
            "# Context management\n"
            "**Strongly prefer answering from the context above.** It already includes:\n"
            "  - CLAUDE.md (core identity, workstreams, priorities — always loaded)\n"
            "  - The most recent calendar/agenda file (always loaded, see top of context)\n"
            "  - 20 files selected by relevance score for this query\n"
            "  - Active graph entities and recent decisions\n"
            "\n"
            "Do NOT use REQUEST_CONTEXT for: calendar/agenda questions, general status, "
            "org structure questions, or anything answerable from the loaded files.\n"
            "\n"
            "ONLY output REQUEST_CONTEXT when the answer requires a specific concrete fact "
            "(a number, a quote, a date) that is clearly missing from everything loaded. "
            "Format (entire line, nothing else):\n"
            "REQUEST_CONTEXT: <focused one-sentence query>\n"
        )

        system_prompt = "\n".join(system_parts)

        # Inject raw documents added by user (sent in request body)
        raw_docs = data.get("raw_docs", [])   # [{"name": str, "content": str}]
        if raw_docs:
            raw_parts = []
            for rd in raw_docs[:5]:   # cap at 5 raw docs
                name    = rd.get("name", "raw_document")
                content = rd.get("content", "")[:6000]
                raw_parts.append(f"\n\n---\n# Raw document: {name}\n\n{content}")
            system_prompt += "\n".join(raw_parts)

        # ── Phase 5: Stream response via configured backend ───────────────────
        chat_cfg      = getattr(cfg, "chat", None)
        backend       = getattr(chat_cfg, "backend", "api") if chat_cfg else "api"
        assistant_text = ""

        if backend == "cli":
            cli_bin = (getattr(chat_cfg, "cli_bin", None) or None) or shutil.which("claude")
            if not cli_bin:
                yield f"data: {json.dumps({'token': '⚠ Claude CLI not found. Install claude or set chat.cli_bin in config.'})}\n\n"
                yield "data: [DONE]\n\n"
                _end_chat()
                return
            cli_model = getattr(chat_cfg, "cli_model", None) or None
            try:
                for text in _stream_cli(messages, system_prompt, cli_bin, model=cli_model, abort=abort_evt):
                    if abort_evt.is_set():
                        yield f"data: {json.dumps({'interrupted': True})}\n\n"
                        break
                    assistant_text += text
                    _append_partial(text)
                    yield f"data: {json.dumps({'token': text})}\n\n"
                if abort_evt.is_set():
                    yield "data: [DONE]\n\n"
                    _end_chat()
                    return
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        else:
            api_key = os.environ.get("ANTHROPIC_API_KEY", "")
            if not api_key:
                yield (
                    f"data: {json.dumps({'token': '⚠ No ANTHROPIC_API_KEY set. '
                    'Either set the env var, or switch to the CLI backend: '
                    'add  chat:\\n  backend: cli  to ~/.engram/config.yaml'})}\n\n"
                )
                yield "data: [DONE]\n\n"
                _end_chat()
                return
            try:
                import anthropic
                client = anthropic.Anthropic(api_key=api_key)
                with client.messages.stream(
                    model      = cfg.models.primary,
                    max_tokens = 4096,
                    system     = system_prompt,
                    messages   = messages,
                ) as stream:
                    for text in stream.text_stream:
                        if abort_evt.is_set():
                            yield f"data: {json.dumps({'interrupted': True})}\n\n"
                            break
                        assistant_text += text
                        _append_partial(text)
                        yield f"data: {json.dumps({'token': text})}\n\n"
                if abort_evt.is_set():
                    yield "data: [DONE]\n\n"
                    _end_chat()
                    return
            except Exception as e:
                yield f"data: {json.dumps({'error': str(e)})}\n\n"

        # ── Phase 5b: Handle REQUEST_CONTEXT sentinel ────────────────────────
        # If model said REQUEST_CONTEXT: <query>, re-run retrieval on that
        # focused query and inject the new files, then stream a fresh response.
        rc_match = re.search(r"REQUEST_CONTEXT:\s*(.+)", assistant_text)
        if rc_match and not getattr(generate, "_in_context_retry", False):
            focused_q = rc_match.group(1).strip()
            print(f"[chat] REQUEST_CONTEXT: {focused_q!r}", flush=True)
            # Tell client to clear the RC text and show a search pill instead
            yield f"data: {json.dumps({'clear_response': True})}\n\n"
            yield f"data: {json.dumps({'request_context': {'query': focused_q[:120]}})}\n\n"
            try:
                extra_scan = memory_scan(focused_q, cfg, max_files=8)
                extra_cands = build_candidates(extra_scan, cfg, snippet_chars=200)
                extra_sel, extra_reason = curate_context(
                    focused_q, extra_cands, cfg, max_files=5
                )
                if extra_sel:
                    # Inject new files into system prompt
                    for c in extra_sel:
                        p = Path(c["path"])
                        if not p.is_absolute():
                            p = base / c["path"]
                        if p.exists() and used < budget:
                            content = p.read_text(errors="ignore")[:4000]
                            system_prompt += f"\n\n---\n# Additional context: {c['path']}\n\n{content}"
                            used += len(content)

                    add_pl = [{"path": c["path"], "type": c["type"]} for c in extra_sel]
                    yield f"data: {json.dumps({'context_update': {'add': add_pl, 'remove': [], 'reason': f'request_more_context: {focused_q[:60]}'}})}\n\n"

                    # Clear the REQUEST_CONTEXT text and stream a fresh response
                    assistant_text = ""
                    generate._in_context_retry = True

                    if backend == "cli":
                        for text in _stream_cli(messages, system_prompt, cli_bin, model=cli_model, abort=abort_evt):
                            if abort_evt.is_set(): break
                            assistant_text += text
                            _append_partial(text)
                            yield f"data: {json.dumps({'token': text})}\n\n"
                    else:
                        with client.messages.stream(
                            model=cfg.models.primary, max_tokens=4096,
                            system=system_prompt, messages=messages,
                        ) as stream:
                            for text in stream.text_stream:
                                if abort_evt.is_set(): break
                                assistant_text += text
                                _append_partial(text)
                                yield f"data: {json.dumps({'token': text})}\n\n"
            except Exception as e:
                print(f"[chat] REQUEST_CONTEXT retry error: {e}", flush=True)
            finally:
                if hasattr(generate, "_in_context_retry"):
                    del generate._in_context_retry

        # ── Phase 6: Haiku monitor — update context for next turn ─────────────
        if assistant_text and all_candidates:
            try:
                all_msgs = messages + [{"role": "assistant", "content": assistant_text}]
                update   = monitor_context(all_msgs, selected, all_candidates, cfg)
                if update.get("action") == "update":
                    add_pl    = [{"path": c["path"], "type": c["type"]} for c in update.get("add", [])]
                    remove_pl = [{"path": c["path"], "type": c["type"]} for c in update.get("remove", [])]
                    if add_pl or remove_pl:
                        yield f"data: {json.dumps({'context_update': {'add': add_pl, 'remove': remove_pl, 'reason': update.get('reason', '')}})}\n\n"
            except Exception as e:
                print(f"[chat] monitor error: {e}", flush=True)

        # ── Phase 7: Log this turn + harvest proposals in background ─────────
        # Sessions are stored at MEMORY/sessions/<YYYY-MM>/chat_<id>.md so the
        # curator's keyword scan picks them up — past conversations naturally
        # become part of future retrieval. The harvest call is async so it
        # doesn't add latency to the chat response.
        if assistant_text and session_id:
            try:
                from engram.memory.session_harvester import (
                    log_turn as _log_turn,
                    harvest_in_background as _harvest_bg,
                )
                _log_turn(
                    memory_path     = cfg.memory_path,
                    session_id      = session_id,
                    user_msg        = query,
                    assistant_text  = assistant_text,
                    selected        = selected,
                    raw_docs        = raw_docs if raw_docs else [],
                    was_interruption= was_interruption,
                )
                _harvest_bg(
                    memory_path     = cfg.memory_path,
                    session_id      = session_id,
                    user_msg        = query,
                    assistant_text  = assistant_text,
                    user_name       = cfg.identity.user_name or "",
                    cfg             = cfg,
                )
            except Exception:
                print("[chat] session log/harvest error:\n" + traceback.format_exc(), flush=True)

        yield "data: [DONE]\n\n"
        _end_chat()

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Interject (human-like interruption) ──────────────────────────────────────
# When the user sends a second message while the assistant is still streaming,
# the frontend POSTs here. A small Haiku call decides whether the assistant
# should keep its current train of thought ("continue") or pivot to the new
# message immediately ("pivot"). On pivot, we set the abort event and the
# in-flight stream stops at its next chunk — feels like cutting someone off.

_INTERJECT_PROMPT = """You are a real-time conversational classifier. The user is talking to an AI assistant. The assistant is mid-response when the user sends a NEW message. You decide what should happen next.

Two possible actions:
  - "continue": let the assistant finish its current thought, then address the new message naturally as a follow-up. Use this when the new message is a clarifying question, a "yes/ok/go on" acknowledgement, or adds context without redirecting.
  - "pivot": stop the current response immediately and address the new message. Use this when the new message corrects, contradicts, redirects, or makes the in-flight question moot. Bias toward pivot when the user sounds like they're cutting in.

Return STRICT JSON ONLY: {"action": "continue" | "pivot", "reason": "<one short sentence>"}

In-flight question from user:
<<<{in_flight}>>>

What the assistant has said so far (may be empty if just started):
<<<{partial}>>>

User's new message (just arrived):
<<<{new_msg}>>>

JSON:"""


def _classify_interjection(in_flight: str, partial: str, new_msg: str, cfg) -> dict:
    """Run a Haiku classifier. Returns {'action': 'continue'|'pivot', 'reason': str, 'latency_ms': int, 'source': str}."""
    import time as _t
    t0 = _t.time()

    # Truncate inputs to keep the classifier fast and cheap.
    prompt = _INTERJECT_PROMPT.format(
        in_flight=in_flight[:600],
        partial=partial[-400:],
        new_msg=new_msg[:600],
    )

    text = ""
    source = "haiku-api"
    try:
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if api_key:
            import anthropic
            client = anthropic.Anthropic(api_key=api_key)
            resp = client.messages.create(
                model      = cfg.models.haiku,
                max_tokens = 120,
                messages   = [{"role": "user", "content": prompt}],
            )
            for block in (resp.content or []):
                if getattr(block, "type", "") == "text":
                    text += getattr(block, "text", "")
        else:
            # Fall back to CLI Haiku — slower (~3-8s) but works without an API key.
            cli_bin = (getattr(getattr(cfg, "chat", None), "cli_bin", None) or None) or shutil.which("claude")
            if not cli_bin:
                raise RuntimeError("no API key and no claude CLI on PATH")
            source = "haiku-cli"
            proc = subprocess.run(
                [cli_bin, "-p", prompt, "--output-format", "text", "--model", cfg.models.haiku],
                capture_output=True, text=True, timeout=10,
            )
            text = (proc.stdout or "").strip()
    except Exception:
        # Log the full traceback server-side; return a generic reason to the
        # client so we don't leak stack details (CodeQL py/stack-trace-exposure).
        import traceback as _tb
        print("[chat/interject] classifier error:\n" + _tb.format_exc(), flush=True)
        return {
            "action":     "continue",
            "reason":     "classifier unavailable — defaulting to continue",
            "latency_ms": int((_t.time() - t0) * 1000),
            "source":     "fallback",
        }

    # Tolerant JSON extraction — strip code fences, find the first {...} block.
    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    m = re.search(r"\{.*?\}", cleaned, re.DOTALL)
    parsed = None
    if m:
        try: parsed = json.loads(m.group(0))
        except Exception: parsed = None

    if not parsed or parsed.get("action") not in ("continue", "pivot"):
        return {
            "action":     "continue",
            "reason":     "classifier output unparseable — defaulting to continue",
            "latency_ms": int((_t.time() - t0) * 1000),
            "source":     source,
        }

    return {
        "action":     parsed["action"],
        "reason":     str(parsed.get("reason", ""))[:200],
        "latency_ms": int((_t.time() - t0) * 1000),
        "source":     source,
    }


@app.route("/api/chat/interject", methods=["POST"])
def chat_interject():
    body = request.get_json(force=True, silent=True) or {}
    new_msg = (body.get("message", "") or "").strip()
    if not new_msg:
        return jsonify({"error": "empty message"}), 400

    active, in_flight, partial = _snapshot_inflight()
    if not active:
        return jsonify({"action": "no_active_chat"})

    cfg = get_cfg()
    verdict = _classify_interjection(in_flight, partial, new_msg, cfg)

    if verdict["action"] == "pivot":
        _signal_pivot()
        print(f"[chat/interject] pivot ({verdict['latency_ms']}ms via {verdict['source']}): {verdict['reason']}", flush=True)
    else:
        print(f"[chat/interject] continue ({verdict['latency_ms']}ms via {verdict['source']}): {verdict['reason']}", flush=True)

    return jsonify(verdict)


# ─── Stats API ────────────────────────────────────────────────────────────────

# ─── Pinned answers ───────────────────────────────────────────────────────────
# Users can pin an assistant answer from the chat. Pinned items show up on the
# Top of Mind tab and clicking restores the chat with that conversation's
# context so the user can keep going from that point.

_pinned_lock = threading.Lock()


def _pinned_index_path(memory_path: Path) -> Path:
    return memory_path / "pinned" / "index.json"


def _load_pinned_index(memory_path: Path) -> list:
    p = _pinned_index_path(memory_path)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text())
    except Exception:
        return []


def _save_pinned_index(memory_path: Path, items: list) -> None:
    p = _pinned_index_path(memory_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with _pinned_lock:
        p.write_text(json.dumps(items, indent=2))


@app.route("/api/pinned/list")
def pinned_list():
    cfg = get_cfg()
    items = _load_pinned_index(cfg.memory_path)
    items.sort(key=lambda p: p.get("pinned_at", ""), reverse=True)
    return jsonify({"items": items})


@app.route("/api/pinned/add", methods=["POST"])
def pinned_add():
    """Pin an assistant answer. Body: {session_id, turn_index, title?}.

    Reads the live session file to capture the user msg + assistant text +
    active context at that turn — those are the source of truth, not whatever
    the client thinks.
    """
    import uuid as _uuid

    cfg = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    session_id = (body.get("session_id", "") or "").strip()
    turn_index = body.get("turn_index")
    title      = (body.get("title", "") or "").strip()[:160]
    if not session_id or turn_index is None:
        return jsonify({"error": "session_id and turn_index required"}), 400

    try:
        from engram.memory.session_harvester import parse_session_file
        turns = parse_session_file(cfg.memory_path, session_id)
    except Exception:
        print("[pinned/add] parse error:\n" + traceback.format_exc(), flush=True)
        return jsonify({"error": "could not read session file"}), 500

    try:
        ti = int(turn_index)
    except Exception:
        return jsonify({"error": "turn_index must be an integer"}), 400
    if ti < 0 or ti >= len(turns):
        return jsonify({"error": f"turn_index {ti} out of range (0–{len(turns)-1})"}), 400

    turn = turns[ti]
    user_msg = (turn.get("user") or "").strip()
    asst     = (turn.get("assistant") or "").strip()
    auto_title = user_msg.split("\n", 1)[0][:140] or "Pinned answer"

    pin = {
        "pin_id":            f"pin_{int(time.time())}_{_uuid.uuid4().hex[:8]}",
        "session_id":        session_id,
        "turn_index":        ti,
        "turn_ts":           turn.get("ts", ""),
        "title":             title or auto_title,
        "user_msg":          user_msg[:600],
        "assistant_snippet": asst[:600],
        "active_context":    turn.get("active_context", []),
        "pinned_at":         datetime.now().isoformat(timespec="seconds"),
    }

    items = _load_pinned_index(cfg.memory_path)
    # If this exact (session, turn_index) is already pinned, return that record
    # rather than creating a duplicate.
    existing = next(
        (it for it in items
         if it.get("session_id") == session_id and it.get("turn_index") == ti),
        None,
    )
    if existing:
        return jsonify(existing)

    items.append(pin)
    _save_pinned_index(cfg.memory_path, items)
    return jsonify(pin)


@app.route("/api/pinned/remove", methods=["POST"])
def pinned_remove():
    cfg = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    pin_id = (body.get("pin_id", "") or "").strip()
    # Allow remove by (session_id, turn_index) for the unpin button on bubbles
    session_id = (body.get("session_id", "") or "").strip()
    turn_index = body.get("turn_index")

    items = _load_pinned_index(cfg.memory_path)
    before = len(items)
    if pin_id:
        items = [it for it in items if it.get("pin_id") != pin_id]
    elif session_id and turn_index is not None:
        try:
            ti = int(turn_index)
        except Exception:
            return jsonify({"error": "turn_index invalid"}), 400
        items = [it for it in items
                 if not (it.get("session_id") == session_id and it.get("turn_index") == ti)]
    else:
        return jsonify({"error": "pin_id or (session_id, turn_index) required"}), 400

    if len(items) == before:
        return jsonify({"removed": 0})
    _save_pinned_index(cfg.memory_path, items)
    return jsonify({"removed": before - len(items)})


@app.route("/api/pinned/restore")
def pinned_restore():
    """Return the conversation up through a pinned turn so the chat tab can
    rehydrate. Frontend swaps SESSION_ID to match so new turns continue the
    same session file.
    """
    cfg = get_cfg()
    pin_id = (request.args.get("pin_id", "") or "").strip()
    if not pin_id:
        return jsonify({"error": "pin_id required"}), 400

    items = _load_pinned_index(cfg.memory_path)
    record = next((it for it in items if it.get("pin_id") == pin_id), None)
    if not record:
        return jsonify({"error": "pin not found"}), 404

    try:
        from engram.memory.session_harvester import parse_session_file
        turns = parse_session_file(cfg.memory_path, record["session_id"])
    except Exception:
        return jsonify({"error": "could not read session"}), 500

    ti = record["turn_index"]
    if ti >= len(turns):
        return jsonify({"error": "turn_index past end of session — file may have been truncated"}), 400

    messages: list = []
    for t in turns[: ti + 1]:
        messages.append({"role": "user",      "content": t.get("user", "")})
        messages.append({"role": "assistant", "content": t.get("assistant", "")})

    return jsonify({
        "pin":            record,
        "session_id":     record["session_id"],
        "messages":       messages,
        "active_context": turns[ti].get("active_context", []),
        "turn_index":     ti,
        "total_turns":    len(turns),
    })


@app.route("/api/sessions/harvest", methods=["POST"])
def sessions_harvest():
    """Manually trigger harvest for a session.

    Body: {"session_id": "...", "force": true|false}
      - force=true bypasses the 1h throttle (button-driven manual run)
      - force=false (default) respects throttle, useful for "scheduled"
        triggers from outside the chat path.
    """
    body = request.get_json(force=True, silent=True) or {}
    session_id = (body.get("session_id", "") or "").strip()
    force = bool(body.get("force", True))   # default True for manual button
    if not session_id:
        return jsonify({"error": "session_id required"}), 400

    cfg = get_cfg()

    # Run synchronously so the button gets a real result. Cap with a soft
    # timeout via threading wouldn't help (the SDK doesn't support cancel).
    try:
        from engram.memory.session_harvester import harvest_session
        result = harvest_session(
            memory_path = cfg.memory_path,
            session_id  = session_id,
            user_name   = cfg.identity.user_name or "",
            cfg         = cfg,
            force       = force,
        )
    except Exception:
        print("[sessions/harvest] error:\n" + traceback.format_exc(), flush=True)
        return jsonify({"ran": False, "reason": "internal_error", "added": 0}), 500

    return jsonify(result)


@app.route("/api/sessions/harvest-status")
def sessions_harvest_status():
    """Return throttle state for a session: last run, age, turn count."""
    session_id = (request.args.get("session_id", "") or "").strip()
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    cfg = get_cfg()
    try:
        from engram.memory.session_harvester import get_harvest_status
        return jsonify(get_harvest_status(cfg.memory_path, session_id))
    except Exception:
        print("[sessions/harvest-status] error:\n" + traceback.format_exc(), flush=True)
        return jsonify({"error": "internal_error"}), 500


@app.route("/api/stats")
def stats():
    cfg         = get_cfg()
    memory_path = cfg.memory_path
    wiki_path   = cfg.wiki_path

    wiki_topics: dict = {}
    wiki_total = 0
    wiki_recent: list = []
    try:
        wiki_pages_dir = wiki_path / "wiki"
        for td in sorted(wiki_pages_dir.iterdir()):
            if td.is_dir() and not td.name.startswith("."):
                pages = [f for f in td.glob("*.md") if f.name != "_index.md"]
                wiki_topics[td.name] = len(pages)
                wiki_total += len(pages)
        wiki_log = wiki_pages_dir / "log.jsonl"
        if wiki_log.exists():
            for line in reversed(wiki_log.read_text(errors="ignore").strip().splitlines()[-20:]):
                try:
                    e  = json.loads(line)
                    pc = e.get("pages_created", 0)
                    pu = e.get("pages_updated", 0)
                    wiki_recent.append({
                        "date":          e.get("date", "")[:10],
                        "file":          Path(e.get("file", "")).name,
                        "pages_created": len(pc) if isinstance(pc, list) else int(pc or 0),
                        "pages_updated": len(pu) if isinstance(pu, list) else int(pu or 0),
                        "summary":       (e.get("summary") or "")[:120],
                    })
                except Exception:
                    pass
            wiki_recent = wiki_recent[:8]
    except Exception as e:
        print(f"[stats] wiki: {e}", flush=True)

    mem_folders: dict = {}
    mem_total = 0
    try:
        for f in memory_path.rglob("*.md"):
            parts  = f.relative_to(memory_path).parts
            folder = parts[0] if len(parts) > 1 else "root"
            mem_folders[folder] = mem_folders.get(folder, 0) + 1
            mem_total += 1
    except Exception:
        pass

    sleep_history: list = []
    last_run: dict = {}
    phase_details: list = []
    tier_dist: dict = {}
    open_q = 0
    contradictions_pending = 0
    health: dict = {}

    try:
        ss_path = memory_path / ".sleep_cycle_status.json"
        if ss_path.exists():
            ss = json.loads(ss_path.read_text())
            for h in (ss.get("history", []))[-8:]:
                sleep_history.append({
                    "date":         h.get("started_at", "")[:16].replace("T", " "),
                    "duration_min": round(h.get("duration_s", 0) / 60, 1),
                    "phases":       h.get("phases_completed", 0),
                })
            lr          = ss.get("last_run", {})
            phases_list = lr.get("phases") or []
            last_run    = {
                "date":             (lr.get("started_at") or "")[:16].replace("T", " "),
                "duration_min":     round(lr.get("duration_s", 0) / 60, 1),
                "phases_completed": len(phases_list) if isinstance(phases_list, list)
                                    else lr.get("phases_completed", 0),
            }
            for ph in (lr.get("phases") or []):
                name   = ph.get("phase", "?")
                detail: dict = {"phase": name}
                if name == "decay":
                    detail["edges_updated"] = ph.get("edges_updated", 0)
                elif name == "episodic_harvest":
                    detail["files_scanned"] = ph.get("consolidation", {}).get("files_scanned", 0)
                elif name == "graph_enrichment":
                    detail["new_edges"]   = ph.get("enrichment", {}).get("new_edges", 0)
                    detail["communities"] = ph.get("communities", {}).get("count", 0)
                elif name == "compression":
                    detail["files_compressed"] = ph.get("files_compressed", 0)
                elif name == "contradiction_resolution":
                    detail["resolved"] = ph.get("resolved", 0)
                phase_details.append(detail)
    except Exception as e:
        print(f"[stats] sleep: {e}", flush=True)

    try:
        hf = memory_path / "health" / "health_snapshot.json"
        if hf.exists():
            hd = json.loads(hf.read_text())
            gq = hd.get("graph_quality", {})
            health = {
                "coverage_score":  hd.get("coverage", {}).get("score", 0),
                "avg_confidence":  round(gq.get("avg_confidence", 0), 3),
                "avg_edge_weight": round(gq.get("avg_edge_weight", 0), 3),
            }
        oq_f = memory_path / "open_questions.json"
        if oq_f.exists():
            oqd   = json.loads(oq_f.read_text())
            qs    = oqd if isinstance(oqd, list) else oqd.get("questions", [])
            open_q = len([q for q in qs if q.get("status") not in ("answered", "resolved", "dismissed", "stale")])
        ct_f = memory_path / "contradictions.json"
        if ct_f.exists():
            ctd  = json.loads(ct_f.read_text())
            cs   = ctd if isinstance(ctd, list) else ctd.get("contradictions", [])
            contradictions_pending = len([c for c in cs if c.get("status") not in ("resolved_A", "resolved_B", "both_true", "both_false", "dismissed", "superseded")])
        # Pending memory-write proposals (chat-session harvest, consolidation,
        # reconsolidation, manual writes). Surfaced on this Health tab now
        # that Top of Mind's middle column became Pinned.
        prop_f = memory_path / "proposals" / "index.json"
        proposals_pending = 0
        if prop_f.exists():
            try:
                pidx = json.loads(prop_f.read_text())
                proposals_pending = len([p for p in pidx if p.get("status") == "pending"])
            except Exception:
                pass
    except Exception as e:
        print(f"[stats] health: {e}", flush=True)
        proposals_pending = 0

    graph_stats: dict = {}
    communities: list = []

    try:
        gf = memory_path / "graph.json"
        if gf.exists():
            g        = json.loads(gf.read_text(errors="ignore"))
            entities = g.get("entities", {})
            edges    = g.get("edges", [])
            type_counts: dict = {}
            for ent in entities.values():
                t = ent.get("type", "other")
                type_counts[t] = type_counts.get(t, 0) + 1
                tier = ent.get("tier", "unknown")
                tier_dist[tier] = tier_dist.get(tier, 0) + 1
            graph_stats = {
                "entities":  len(entities),
                "edges":     len(edges),
                "top_types": sorted(type_counts.items(), key=lambda x: -x[1])[:6],
            }
    except Exception as e:
        print(f"[stats] graph: {e}", flush=True)

    try:
        cf = memory_path / "communities.json"
        if cf.exists():
            cmd = json.loads(cf.read_text())
            raw = list(cmd.values()) if isinstance(cmd, dict) else (
                cmd if isinstance(cmd, list) else cmd.get("communities", [])
            )
            for c in sorted(raw, key=lambda x: -x.get("size", len(x.get("members", []))))[:6]:
                communities.append({"label": c.get("label", "Cluster"),
                                    "size":  c.get("size", len(c.get("members", [])))})
    except Exception as e:
        print(f"[stats] communities: {e}", flush=True)

    return jsonify({
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "compile": {
            "wiki_total":  wiki_total, "wiki_topics": wiki_topics,
            "wiki_recent": wiki_recent, "mem_total": mem_total,
            "mem_folders": dict(sorted(mem_folders.items(), key=lambda x: -x[1])[:8]),
        },
        "dream": {
            "last_run": last_run, "sleep_history": sleep_history,
            "phase_details": phase_details, "open_questions": open_q,
            "contradictions_pending": contradictions_pending,
            "proposals_pending":      proposals_pending,
            "health": health, "tier_distribution": tier_dist,
        },
        "retrieve": {
            "graph_stats": graph_stats, "tier_distribution": tier_dist,
            "communities": communities, "crystallised_count": tier_dist.get("crystallised", 0),
            "wiki_topics": wiki_topics,
        },
    })


@app.route("/api/file")
def serve_file():
    """Return raw text content of a memory or wiki file for preview.

    Paths may arrive in multiple forms — graph.json stores them with the
    "MEMORY/" prefix (relative to base_path); keyword scan emits paths
    relative to memory_path; wiki paths can be absolute. Try each root.
    """
    cfg  = get_cfg()
    path = request.args.get("path", "").strip()
    if not path or path.startswith("__raw__"):
        return jsonify({"error": "no path"}), 400

    candidates: list[Path] = []
    if Path(path).is_absolute():
        candidates.append(Path(path))
    else:
        # base_path + "MEMORY/foo" → memory file (graph.json convention)
        candidates.append(cfg.base_path / path)
        # memory_path + "foo"      → bare relative path (legacy / keyword scan)
        candidates.append(cfg.memory_path / path)
        # wiki_path + "foo"        → wiki page
        candidates.append(cfg.wiki_path / path)

    p = next((c for c in candidates if c.exists() and c.is_file()), None)
    if p is None:
        return jsonify({"error": "not found"}), 404

    # Safety: must resolve inside memory_path, wiki_path, or base_path
    abs_p = p.resolve()
    roots = [cfg.memory_path.resolve(), cfg.wiki_path.resolve(), cfg.base_path.resolve()]
    if not any(str(abs_p).startswith(str(r)) for r in roots):
        return jsonify({"error": "forbidden"}), 403

    content = p.read_text(errors="ignore")[:24000]
    return jsonify({"path": str(p), "name": p.name, "content": content})


@app.route("/api/config")
def config_info():
    cfg = get_cfg()
    return jsonify({
        "org_name":    cfg.identity.org_name,
        "system_name": cfg.identity.system_name,
        "user_name":   cfg.identity.user_name,
        "wiki_topics": cfg.wiki.topics,
    })


@app.route("/api/upload", methods=["POST"])
def upload_doc():
    """Accept file upload and return extracted text for context injection.

    Also saves the original bytes to the watcher's inbox folder so the file
    enters the long-term knowledge pipeline (cleaner → memory → graph) for
    future chat sessions. The current chat keeps its in-memory raw_docs copy
    for immediate use.
    """
    from io import BytesIO

    f = request.files.get("file")
    if not f:
        return jsonify({"error": "no file"}), 400

    name = f.filename or "document"
    ext  = Path(name).suffix.lower()

    # Read bytes once — the parsers below all accept BytesIO, and we need
    # the bytes for the inbox copy too.
    try:
        raw_bytes = f.read()
    except Exception as e:
        print(f"[upload] read failed: {e}", flush=True)
        return jsonify({"error": "could not read uploaded file"}), 500

    try:
        if ext in (".txt", ".md", ".eml", ".vtt", ".csv"):
            content = raw_bytes.decode("utf-8", errors="ignore")

        elif ext == ".pdf":
            try:
                import pypdf
                reader  = pypdf.PdfReader(BytesIO(raw_bytes))
                content = "\n\n".join(
                    page.extract_text() or "" for page in reader.pages
                )
            except ImportError:
                return jsonify({"error": "pypdf not installed — run: pip install pypdf"}), 500

        elif ext == ".docx":
            from docx import Document as DocxDoc
            doc     = DocxDoc(BytesIO(raw_bytes))
            content = "\n".join(p.text for p in doc.paragraphs if p.text.strip())

        elif ext == ".pptx":
            from pptx import Presentation as PptxPrs
            prs     = PptxPrs(BytesIO(raw_bytes))
            content = "\n".join(
                shape.text for slide in prs.slides
                for shape in slide.shapes if hasattr(shape, "text") and shape.text.strip()
            )

        else:
            return jsonify({"error": f"Unsupported file type: {ext}. Supported: .txt .md .pdf .docx .pptx .eml"}), 400

        content = content.strip()[:20000]
        if not content:
            return jsonify({"error": "File appears to be empty or could not be parsed"}), 400

        # Drop the original bytes into the inbox so the watcher picks it up
        # for long-term ingestion. Failures are non-fatal: the immediate
        # chat still works without long-term persistence.
        saved_path = _save_to_inbox(name, raw_bytes)

        stem = Path(name).stem
        return jsonify({
            "name":           stem,
            "content":        content,
            "chars":          len(content),
            "saved_to_inbox": str(saved_path) if saved_path else None,
        })

    except Exception:
        import traceback as _tb
        print("[upload] extraction error:\n" + _tb.format_exc(), flush=True)
        return jsonify({"error": "Failed to parse file — check server log."}), 500


@app.route("/api/inbox-save", methods=["POST"])
def inbox_save_paste():
    """Save pasted-text content to the watcher's inbox as a markdown file.

    Used by the chat sidebar's raw-doc modal when the user enters content via
    the textarea (rather than file upload). Body: {"name": str, "content": str}.
    """
    body = request.get_json(force=True, silent=True) or {}
    name    = (body.get("name", "")    or "raw_paste").strip()
    content = (body.get("content", "") or "")
    if not content.strip():
        return jsonify({"error": "no content"}), 400
    saved = _save_to_inbox(f"{name}.md", content.encode("utf-8"))
    return jsonify({"saved_to_inbox": str(saved) if saved else None})


@app.route("/api/export", methods=["POST"])
def export_file():
    """Generate DOCX / PPTX / MD / PDF from chat content and return as download."""
    from flask import send_file as flask_send_file
    from engram.export.converters import export as do_export

    data     = request.get_json(force=True) or {}
    text     = data.get("content", "").strip()
    fmt      = data.get("format", "md").lower()
    filename = data.get("filename", "engram_export").strip() or "engram_export"

    if not text:
        return jsonify({"error": "no content"}), 400

    cfg      = get_cfg()
    out_dir  = Path(cfg.paths.outputs_path) if getattr(cfg.paths, "outputs_path", None) else \
               Path(cfg.memory_path).parent / "outputs"
    out_dir.mkdir(parents=True, exist_ok=True)

    result = do_export(text, fmt, filename, out_dir)
    if "error" in result:
        return jsonify(result), 500

    path = Path(result["path"])
    mime_map = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdf":  "application/pdf",
        "md":   "text/markdown; charset=utf-8",
        "html": "text/html; charset=utf-8",
    }
    mime = mime_map.get(result["format"], "application/octet-stream")

    note = result.get("note", "")
    resp = flask_send_file(str(path), mimetype=mime,
                           as_attachment=True, download_name=path.name)
    if note:
        resp.headers["X-Export-Note"] = note
    return resp


@app.route("/api/outputs")
def list_outputs():
    """Return the 20 most-recently modified files in the outputs folder."""
    cfg     = get_cfg()
    out_dir = Path(cfg.paths.outputs_path) if getattr(cfg.paths, "outputs_path", None) else \
              Path(cfg.memory_path).parent / "outputs"

    if not out_dir.exists():
        out_dir.mkdir(parents=True, exist_ok=True)
        return jsonify({"files": [], "dir": str(out_dir)})

    files = sorted(
        [f for f in out_dir.iterdir() if f.is_file() and not f.name.startswith(".")],
        key=lambda f: f.stat().st_mtime, reverse=True
    )[:20]

    return jsonify({
        "dir": str(out_dir),
        "files": [
            {"name": f.name, "size": f.stat().st_size,
             "modified": f.stat().st_mtime,
             "ext": f.suffix.lstrip(".").lower(),
             "abspath": str(f.resolve())}
            for f in files
        ],
    })


@app.route("/api/open-path", methods=["POST"])
def open_path():
    """
    Open a file or folder in the OS default app / file manager.
    Body: {"path": "/abs/path", "reveal": false}
    On macOS uses `open` (with `-R` for reveal).
    """
    body   = request.get_json(force=True, silent=True) or {}
    path   = body.get("path", "")
    reveal = bool(body.get("reveal", False))

    if not path:
        return jsonify({"error": "no path"}), 400

    p = Path(path)
    if not p.exists():
        return jsonify({"error": "not found"}), 404

    cfg = get_cfg()
    # Safety: only allow paths under memory_path, outputs_path, or wiki_path
    allowed = [
        cfg.memory_path,
        Path(cfg.paths.outputs_path) if getattr(cfg.paths, "outputs_path", None) else None,
        cfg.wiki_path,
    ]
    allowed = [a.resolve() for a in allowed if a]
    abs_p = p.resolve()
    if not any(str(abs_p).startswith(str(a)) for a in allowed):
        return jsonify({"error": "path outside allowed roots"}), 403

    try:
        if sys.platform == "darwin":
            cmd = ["open"] + (["-R"] if reveal else []) + [str(abs_p)]
        elif sys.platform.startswith("linux"):
            cmd = ["xdg-open", str(abs_p.parent if reveal else abs_p)]
        else:  # windows
            cmd = ["explorer", "/select," + str(abs_p)] if reveal else ["start", "", str(abs_p)]
        subprocess.Popen(cmd)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/health-detail")
def health_detail():
    cfg  = get_cfg()
    what = request.args.get("what", "")
    mem  = cfg.memory_path

    if what == "contradictions":
        p = mem / "contradictions.json"
        if not p.exists():
            return jsonify({"items": [], "total": 0})
        try:
            data  = json.loads(p.read_text(errors="ignore"))
            items = data if isinstance(data, list) else data.get("contradictions", [])
            active = [it for it in items if it.get("status") not in ("resolved_A", "resolved_B", "both_true", "both_false", "dismissed", "superseded")]
            out   = []
            for it in active[:50]:
                ca = it.get("claim_A") or {}
                cb = it.get("claim_B") or {}
                out.append({
                    "id":       it.get("id", ""),
                    "type":     it.get("type", "factual_conflict"),
                    "severity": it.get("severity", "medium"),
                    "status":   it.get("status", "unresolved"),
                    "claim_A":  {
                        "statement": ca.get("statement", ""),
                        "source":    ca.get("source", ""),
                        "date":      (ca.get("date") or "")[:10],
                        "weight":    round(ca.get("weight", 0.5), 2),
                    },
                    "claim_B":  {
                        "statement": cb.get("statement", ""),
                        "source":    cb.get("source", ""),
                        "date":      (cb.get("date") or "")[:10],
                        "weight":    round(cb.get("weight", 0.5), 2),
                    },
                })
            return jsonify({"items": out, "total": len(active)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif what == "open_questions":
        p = mem / "open_questions.json"
        if not p.exists():
            return jsonify({"items": [], "total": 0})
        try:
            data = json.loads(p.read_text(errors="ignore"))
            qs   = data if isinstance(data, list) else data.get("questions", [])
            active = [q for q in qs if q.get("status") not in ("answered", "dismissed", "resolved", "closed", "stale")]
            out  = []
            for q in active[:50]:
                raw_text = q.get("text", "")
                # Strip markdown bold/status boilerplate if text is just template
                clean = re.sub(r"\*\*Status:\*\*.*$", "", raw_text, flags=re.DOTALL).strip()
                clean = re.sub(r"\*\*Review:\*\*.*$", "", clean, flags=re.DOTALL).strip()
                if not clean:
                    clean = raw_text
                out.append({
                    "id":       q.get("id", ""),
                    "text":     clean[:400],
                    "priority": q.get("priority", "medium"),
                    "status":   q.get("status", "open"),
                    "source":   q.get("created_from", q.get("source", "")),
                    "created":  (q.get("created_at") or "")[:10],
                })
            return jsonify({"items": out, "total": len(active)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    elif what == "proposals":
        # Pending memory-write proposals — surfaced in Engram Health since
        # the Top of Mind tile got swapped out for Pinned. Newest first
        # within salience tier.
        p = mem / "proposals" / "index.json"
        if not p.exists():
            return jsonify({"items": [], "total": 0})
        try:
            idx = json.loads(p.read_text(errors="ignore"))
            pending = [it for it in idx if it.get("status") == "pending"]
            pending.sort(key=lambda x: x.get("ts", ""), reverse=True)
            pending.sort(key=lambda x: x.get("salience") or 0, reverse=True)
            out: list = []
            for it in pending[:80]:
                out.append({
                    "uid":              it.get("uid", ""),
                    "path":             it.get("path", ""),
                    "operation":        it.get("operation", "update"),
                    "reason":           (it.get("reason") or "")[:300],
                    "salience":         round(it.get("salience") or 0, 2),
                    "source":           it.get("source", ""),
                    "harvest_filename": it.get("harvest_filename", ""),
                    "ts":               (it.get("ts") or "")[:16].replace("T", " "),
                })
            return jsonify({"items": out, "total": len(pending)})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    return jsonify({"error": "unknown"}), 400


# ── Statement parsing for cascade resolution ──────────────────────────────────
# Statements are of the form: "<Subject> <relation> <Object>"
# e.g. "Benoit Joly reports_to Leo Sei" → (Benoit Joly, reports_to, Leo Sei)
#
# Single-valued relations: a subject can have AT MOST ONE object for these.
# Once we know the truth, all conflicting claims for the same (subject, relation)
# can be auto-resolved or dismissed.
SINGLE_VALUED_RELATIONS = {
    "reports_to", "reports to",
    "ceo_of", "cfo_of", "cpo_of", "cto_of",
    "located_in", "born_in", "headquartered_in",
    "spouse_of", "married_to",
    "owned_by", "parent_company",
    "founded_by",
    "has_role", "current_role",
    "ceo", "cfo", "cpo", "cto",
}

# Multi-valued relations (don't dismiss non-matches, only positive-match).
# Anything not in SINGLE_VALUED_RELATIONS is treated as multi-valued.
KNOWN_RELATIONS = SINGLE_VALUED_RELATIONS | {
    "manages", "leads", "owns", "works_with", "collaborates_with",
    "knows", "advises", "mentors", "reports_from", "has_member",
    "is_a", "part_of", "related_to", "linked_to",
}


def _parse_statement(stmt: str) -> tuple[str, str, str] | None:
    """
    Parse "<Subject> <relation> <Object>" into (subject, relation, object).
    Tries known relation tokens in order of length (longest first).
    Returns None if no relation matches.
    """
    if not stmt:
        return None
    s = stmt.strip()
    # Try longest relations first to avoid partial matches
    for rel in sorted(KNOWN_RELATIONS, key=len, reverse=True):
        for token in (f" {rel} ", f" {rel.replace('_', ' ')} "):
            idx = s.lower().find(token.lower())
            if idx > 0:
                subject = s[:idx].strip()
                obj     = s[idx + len(token):].strip()
                return (subject, rel.replace(" ", "_"), obj)
    return None


def _norm(s: str) -> str:
    """Normalize a name for fuzzy comparison: lowercase, collapse whitespace, strip punctuation."""
    if not s:
        return ""
    return re.sub(r"[\s\-_.,]+", " ", s.lower()).strip()


def _cascade_resolve(items: list, source_item: dict, resolution: str) -> int:
    """
    Auto-resolve other contradictions about the same (subject, relation)
    based on the resolved one.
    Returns count of cascaded resolutions.
    """
    if resolution not in ("resolved_A", "resolved_B"):
        return 0  # only positive resolutions cascade

    winner_stmt = (source_item.get("claim_A" if resolution == "resolved_A" else "claim_B") or {}).get("statement", "")
    parsed = _parse_statement(winner_stmt)
    if not parsed:
        return 0
    w_subj, w_rel, w_obj = parsed
    w_subj_n, w_obj_n = _norm(w_subj), _norm(w_obj)
    is_single_valued = w_rel in SINGLE_VALUED_RELATIONS

    cascaded = 0
    now_iso = datetime.now(timezone.utc).isoformat()

    for it in items:
        if it.get("id") == source_item.get("id"):
            continue
        if it.get("status") not in (None, "", "unresolved", "resolved_neither"):
            continue

        a_stmt = (it.get("claim_A") or {}).get("statement", "")
        b_stmt = (it.get("claim_B") or {}).get("statement", "")
        a_parsed = _parse_statement(a_stmt)
        b_parsed = _parse_statement(b_stmt)

        # Both claims must reference the same subject + relation as the winner
        if not (a_parsed and b_parsed):
            continue
        if a_parsed[1] != w_rel or b_parsed[1] != w_rel:
            continue
        if _norm(a_parsed[0]) != w_subj_n or _norm(b_parsed[0]) != w_subj_n:
            continue

        a_match = _norm(a_parsed[2]) == w_obj_n
        b_match = _norm(b_parsed[2]) == w_obj_n

        new_status = None
        if a_match and not b_match:
            new_status = "resolved_A"
        elif b_match and not a_match:
            new_status = "resolved_B"
        elif not a_match and not b_match and is_single_valued:
            # Single-valued relation: if neither claim matches the established truth,
            # both are wrong → dismiss.
            new_status = "dismissed"
        # If both match (rare) or relation is multi-valued with no match: skip.

        if new_status:
            it["status"]       = new_status
            it["resolved_by"]  = f"cascade_from_{source_item.get('id')}"
            it["resolved_at"]  = now_iso
            cascaded += 1

    return cascaded


@app.route("/api/resolve-contradiction", methods=["POST"])
def resolve_contradiction():
    cfg  = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    item_id    = body.get("id", "")
    resolution = body.get("resolution", "dismissed")
    # Accepted: resolved_A | resolved_B | both_true | both_false | dismissed

    if resolution not in ("resolved_A", "resolved_B", "both_true", "both_false", "dismissed"):
        return jsonify({"error": "invalid resolution"}), 400

    p = cfg.memory_path / "contradictions.json"
    if not p.exists():
        return jsonify({"error": "file not found"}), 404
    try:
        from engram.memory.contradictions import record_resolution
        registry_path = cfg.memory_path / ".rejected_claims.json"

        data  = json.loads(p.read_text(errors="ignore"))
        items = data if isinstance(data, list) else data.get("contradictions", [])
        target = next((it for it in items if it.get("id") == item_id), None)
        if not target:
            return jsonify({"error": "not found"}), 404

        target["status"]      = resolution
        target["resolved_by"] = "user"
        target["resolved_at"] = datetime.now(timezone.utc).isoformat()

        # Persist the user's decision into the rejection registry so future
        # sleep cycles don't re-extract these triples.
        registry_added = record_resolution(registry_path, target, resolution)

        # Cascade resolution to related contradictions (only for resolved_A/B)
        cascaded = _cascade_resolve(items, target, resolution)

        # Each cascaded resolution also gets recorded into the registry
        if cascaded > 0:
            for it in items:
                rb = str(it.get("resolved_by", ""))
                if rb.startswith(f"cascade_from_{item_id}"):
                    registry_added += record_resolution(registry_path, it, it.get("status",""))

        if isinstance(data, list):
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            data["contradictions"] = items
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False))

        return jsonify({
            "ok":              True,
            "id":              item_id,
            "resolution":      resolution,
            "cascaded":        cascaded,
            "registry_added":  registry_added,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/cleanup-resolved", methods=["POST"])
def cleanup_resolved():
    """
    Retroactively backfill the rejected-claims registry from all already-resolved
    contradictions, then purge matching edges from graph.json.

    Useful after the user has resolved many contradictions but the system was
    not yet recording them as ground truths. Idempotent.
    """
    cfg = get_cfg()
    from engram.memory.contradictions import (
        record_resolution, load_rejected_registry, save_rejected_registry,
        purge_rejected_edges_from_graph,
    )

    p = cfg.memory_path / "contradictions.json"
    g = cfg.memory_path / "graph.json"
    registry_path = cfg.memory_path / ".rejected_claims.json"

    if not p.exists():
        return jsonify({"error": "no contradictions"}), 404

    data  = json.loads(p.read_text(errors="ignore"))
    items = data if isinstance(data, list) else data.get("contradictions", [])

    # Backfill registry from already-resolved items
    backfilled = 0
    for it in items:
        status = it.get("status", "")
        if status in ("resolved_A", "resolved_B", "both_false"):
            backfilled += record_resolution(registry_path, it, status)

    # Purge bad edges from the graph
    purged = 0
    if g.exists():
        graph = json.loads(g.read_text(errors="ignore"))
        registry = load_rejected_registry(registry_path)
        purged = purge_rejected_edges_from_graph(graph, registry)
        if purged:
            # Backup before write
            backup = g.with_suffix(f".pre_cleanup_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}.bak")
            backup.write_text(g.read_text())
            g.write_text(json.dumps(graph, indent=2, ensure_ascii=False))

    registry = load_rejected_registry(registry_path)
    return jsonify({
        "ok":                True,
        "backfilled_registry": backfilled,
        "graph_edges_purged":  purged,
        "registry_total_rejected":  len(registry.get("rejected", [])),
        "registry_total_truths":    len(registry.get("ground_truths", [])),
    })


@app.route("/api/resolve-proposal", methods=["POST"])
def resolve_proposal():
    """Mark a pending proposal saved | skipped. Body: {uid, status}.

    Uses the existing engram.memory.proposals.update_status() so the index's
    *_at timestamps stay consistent with how consolidation writes them.
    """
    cfg  = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    uid    = (body.get("uid")    or body.get("id")         or "").strip()
    status = (body.get("status") or body.get("resolution") or "").strip()
    if status not in ("saved", "skipped"):
        return jsonify({"error": "invalid status"}), 400
    if not uid:
        return jsonify({"error": "uid required"}), 400

    try:
        from engram.memory.proposals import update_status
        ok = update_status(cfg.memory_path / "proposals" / "index.json", uid, status)
        if not ok:
            return jsonify({"error": "uid not found"}), 404
        return jsonify({"ok": True, "uid": uid, "status": status})
    except Exception:
        print("[resolve-proposal] error:\n" + traceback.format_exc(), flush=True)
        return jsonify({"error": "internal_error"}), 500


@app.route("/api/resolve-question", methods=["POST"])
def resolve_question():
    cfg = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    item_id    = body.get("id", "")
    resolution = body.get("resolution", "dismissed")  # answered | dismissed

    if resolution not in ("answered", "dismissed"):
        return jsonify({"error": "invalid resolution"}), 400

    p = cfg.memory_path / "open_questions.json"
    if not p.exists():
        return jsonify({"error": "file not found"}), 404
    try:
        data = json.loads(p.read_text(errors="ignore"))
        qs   = data if isinstance(data, list) else data.get("questions", [])
        found = False
        for q in qs:
            if q.get("id") == item_id:
                q["status"]      = resolution
                q["answered_by"] = "user"
                q["answered_at"] = datetime.now(timezone.utc).isoformat()
                found = True
                break
        if not found:
            return jsonify({"error": "not found"}), 404
        if isinstance(data, list):
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        else:
            data["questions"] = qs
            p.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        return jsonify({"ok": True, "id": item_id, "resolution": resolution})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/watcher-status")
def watcher_status():
    return jsonify(_watcher_status)


@app.route("/api/clean-emails", methods=["POST"])
def clean_emails():
    """
    Sweep MEMORY/daily/emails/ — strip HTML noise from each file, drop marketing.
    Backs up the original folder before any destructive change.
    """
    cfg = get_cfg()
    emails_dir = cfg.memory_path / "daily" / "emails"
    if not emails_dir.exists():
        return jsonify({"error": "no daily/emails dir"}), 404

    cleaner = EmailCleaner(skip_marketing=True)
    cleaned   = 0
    dropped   = 0
    untouched = 0
    bytes_before = 0
    bytes_after  = 0

    for f in emails_dir.iterdir():
        if not f.is_file() or f.name.startswith("."):
            continue
        try:
            raw = f.read_text(errors="ignore")
        except Exception:
            continue
        if len(raw) < 200:
            continue
        bytes_before += len(raw)

        res = cleaner.clean(raw, filename=f.name)

        if res.is_marketing:
            f.unlink()  # drop marketing
            dropped += 1
            continue

        if res.cleaned_chars < res.original_chars * 0.95 and res.cleaned_chars > 100:
            # Meaningful reduction — write cleaned markdown back, preserve mtime
            orig_mtime = f.stat().st_mtime
            md = res.to_markdown()
            f.write_text(md, encoding="utf-8")
            try:
                os.utime(f, (orig_mtime, orig_mtime))
            except Exception:
                pass
            cleaned += 1
            bytes_after += len(md)
        else:
            untouched += 1
            bytes_after += res.original_chars

    saved_pct = int(100 * (1 - bytes_after / max(bytes_before, 1)))
    return jsonify({
        "ok":           True,
        "cleaned":      cleaned,
        "dropped":      dropped,
        "untouched":    untouched,
        "bytes_saved":  bytes_before - bytes_after,
        "saved_pct":    saved_pct,
    })


@app.route("/api/watcher-rescan", methods=["POST"])
def watcher_rescan():
    """
    Reset the seen registry and trigger a fresh full sweep of the inbox.
    Useful after bulk deletions/restores or to re-ingest everything.
    """
    cfg = get_cfg()
    if _watcher is None:
        return jsonify({"error": "watcher not running"}), 400
    try:
        # Wipe registry
        registry_file = cfg.memory_path / ".watcher_seen.json"
        if registry_file.exists():
            registry_file.unlink()
        # Reset in-memory state
        _watcher._registry._seen = {}
        _watcher_status["files_new"] = 0
        _watcher_status["recent_files"] = []
        # Trigger immediate scan in background
        threading.Thread(target=_watcher.run_once, daemon=True).start()
        return jsonify({"ok": True, "message": "rescan triggered"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/recent-activity")
def recent_activity():
    """
    Real-time feed of recently modified memory files (last 7 days).
    Replaces the stale wiki/log.jsonl-based 'Recent activity' panel.
    """
    cfg = get_cfg()
    mem = cfg.memory_path
    cutoff = time.time() - 14 * 86400  # 14 days
    items: list = []
    try:
        for f in mem.rglob("*.md"):
            try:
                if "/sessions/" in str(f) or "/_pre_compression_backups/" in str(f):
                    continue
                m = f.stat().st_mtime
                if m < cutoff:
                    continue
                items.append({
                    "name":     f.name,
                    "rel":      str(f.relative_to(mem)),
                    "folder":   f.parent.relative_to(mem).as_posix() or "/",
                    "modified": m,
                    "size":     f.stat().st_size,
                })
            except Exception:
                continue
    except Exception as e:
        return jsonify({"items": [], "error": str(e)})

    # Sort by mtime (desc); break ties by name so same-second files have
    # a stable, predictable order (otherwise large bulk-rewrites bury new files).
    items.sort(key=lambda x: (x["modified"], x["name"]), reverse=True)
    return jsonify({"items": items[:50], "total": len(items)})


# ─── Top of Mind ──────────────────────────────────────────────────────────────
# Curates *executive-level* signal from the next two weeks: high-stakes
# meetings, decisions awaiting save, and deadlines extracted from recent
# emails. Filtered, not exhaustive — the calendar tab is for everything.

# Words that suggest an event is decision-grade or executive.
# Matched as whole words (\b…\b) — so "coo" won't fire on "Watercooler".
_EXEC_KEYWORD_WEIGHTS: dict = {
    # Decisions & approvals
    "qbr": 1.6, "go/no-go": 1.6, "go-no-go": 1.6, "approval": 1.2, "approve": 1.0,
    "decision": 1.2, "sign-off": 1.2, "milestone": 0.9,
    # Forums
    "board":     1.5, "steering":  1.4, "committee": 1.0, "council":   1.0,
    "executive": 1.1, "exec":      0.9, "leadership":1.0, "c-level":   1.4,
    # Senior individuals
    "ceo":       1.3, "cfo": 1.2, "cto": 1.2, "cmo": 1.2, "coo": 1.2,
    "svp":       1.0, "evp": 1.0, "vp": 0.7,
    # High-stakes commercial
    "negotiation":   1.0, "contract": 0.9, "rfp": 1.1, "rfq": 0.9, "tender": 0.9,
    "kickoff":  0.7, "go-live": 1.0, "launch":  0.7,
    # Strategy / planning
    "roadmap": 0.7, "okr": 0.7,
}
_EXEC_KEYWORD_RX = {
    kw: re.compile(r"(?<!\w)" + re.escape(kw) + r"(?!\w)", re.IGNORECASE)
    for kw in _EXEC_KEYWORD_WEIGHTS
}

# Words that demote an event regardless of other signals.
_PERSONAL_SKIP_RX = re.compile(
    r"\b(kids|drop[- ]off|pickup|pick up|school|dentist|doctor|"
    r"vacation|holiday|ooo|out of office|dns|do not schedule|"
    r"focus time|lunch|gym|workout|haircut|commute)\b",
    re.IGNORECASE,
)

# 1:1s — bilateral meetings; can be high-stakes but usually aren't.
_ONE_ON_ONE_RX = re.compile(r"\b(1[:\-]?1|one[- ]on[- ]one)\b", re.IGNORECASE)


def _classify_event(e) -> tuple[float, list[str]]:
    """Return (score, tags) for an event. Score >= 0; tags explain the why."""
    text = " ".join(filter(None, [e.summary or "", e.location or "", e.organizer or ""])).lower()

    if _PERSONAL_SKIP_RX.search(text):
        return (0.0, ["personal"])

    score = 0.0
    tags: list[str] = []
    for kw, w in _EXEC_KEYWORD_WEIGHTS.items():
        if _EXEC_KEYWORD_RX[kw].search(text):
            score += w
            tags.append(kw)

    # Structural signals
    try:
        duration_min = (e.end - e.start).total_seconds() / 60
    except Exception:
        duration_min = 60
    if duration_min >= 90 and not e.all_day:
        score += 0.4
        tags.append("long")
    if e.all_day:
        score += 0.5
        tags.append("all-day")
    if e.location and "Microsoft Teams" not in e.location and "Skype" not in e.location:
        score += 0.3
        tags.append("in-person")

    # 1:1 demotion (only if no exec keyword fired)
    if _ONE_ON_ONE_RX.search(text) and score < 1.0:
        score *= 0.4
        tags.append("1:1")

    return (round(score, 2), tags[:5])


# Deadline extraction now lives in engram/memory/signal_extractor.py — an
# AI-driven pass that resolves relative dates, filters past + already-replied
# threads, and persists to MEMORY/signals/deadlines.json. The dashboard reads
# the JSON; trigger refresh via /api/signals/refresh or scripts/extract-deadlines.py.


@app.route("/api/top-of-mind")
def top_of_mind():
    cfg = get_cfg()
    mem = cfg.memory_path
    days_ahead = int(request.args.get("days", 14))

    # 1) Curated calendar events — only executive-grade
    events_out: list = []
    ics_search_dirs: list[Path] = []
    try:
        inbox = getattr(cfg.paths, "inbox_src", None) if hasattr(cfg, "paths") else None
        if inbox and Path(inbox).exists():
            ics_search_dirs.append(Path(inbox))
    except Exception:
        pass
    ics_search_dirs.append(mem)

    ics_hits: list[Path] = []
    for d in ics_search_dirs:
        try:
            # Skip files under _processed/ — those are archived after watcher
            # ingest and would otherwise shadow the live calendar if their
            # mtime got touched.
            ics_hits.extend(p for p in d.rglob("*.ics") if "_processed" not in p.parts)
        except Exception:
            continue

    if ics_hits:
        try:
            ics_file = max(ics_hits, key=lambda f: f.stat().st_mtime)
            evs = parse_ics(ics_file)
            scored: list = []
            for e in ics_upcoming(evs, days_ahead=days_ahead):
                score, tags = _classify_event(e)
                if score < 0.6:
                    continue
                scored.append((score, tags, e))
            # Sort: by date asc within score-tier; cap at 12.
            scored.sort(key=lambda x: x[2].start)
            scored.sort(key=lambda x: x[0], reverse=True)
            for score, tags, e in scored[:12]:
                local = e.start.astimezone()
                local_end = e.end.astimezone()
                if e.all_day:
                    tline = "all-day"
                else:
                    tline = local.strftime("%H:%M") + "–" + local_end.strftime("%H:%M")
                events_out.append({
                    "date":      local.strftime("%a %d %b"),
                    "iso":       local.strftime("%Y-%m-%d"),
                    "time":      tline,
                    "summary":   (e.summary or "")[:140],
                    "location":  "" if (e.location and ("Microsoft Teams" in e.location or "Skype" in e.location)) else (e.location or "")[:60],
                    "organizer": (e.organizer.split("@")[0] if e.organizer and "@" in e.organizer else ""),
                    "score":     score,
                    "tags":      tags,
                })
            # Re-sort for display by date asc so the timeline reads naturally.
            events_out.sort(key=lambda x: x["iso"])
        except Exception:
            pass

    # 2) Pinned chat answers — user pinned them while chatting; clicking
    # restores the conversation with full context so they can keep going.
    pinned_out: list = []
    try:
        items = _load_pinned_index(mem)
        items.sort(key=lambda p: p.get("pinned_at", ""), reverse=True)
        for p in items[:30]:
            pinned_out.append({
                "pin_id":            p.get("pin_id", ""),
                "title":             p.get("title", "")[:160],
                "user_msg":          (p.get("user_msg") or "")[:240],
                "assistant_snippet": (p.get("assistant_snippet") or "")[:280],
                "session_id":        p.get("session_id", ""),
                "turn_index":        p.get("turn_index", 0),
                "pinned_at":         p.get("pinned_at", ""),
            })
    except Exception:
        pass

    # 3) Deadlines from AI-extracted signals (MEMORY/signals/deadlines.json).
    # The extractor resolves relative dates against each email's send date,
    # filters past deadlines and already-replied threads, and persists. The
    # endpoint just reads JSON — no regex, no on-the-fly LLM calls.
    deadlines_out: list = []
    extracted_at = None
    extracted_age_s = None
    try:
        from engram.memory.signal_extractor import (
            load_signals as _load_signals,
            signals_age_seconds as _signals_age,
        )
        sigs = _load_signals(mem)
        if sigs:
            deadlines_out = sigs.get("deadlines", []) or []
            extracted_at  = sigs.get("extracted_at")
            extracted_age_s = _signals_age(mem)
    except Exception as e:
        print(f"[top-of-mind] signals load error: {e}", flush=True)

    return jsonify({
        "events":    events_out,
        "pinned":    pinned_out,
        "deadlines": deadlines_out,
        "deadlines_meta": {
            "extracted_at": extracted_at,
            "age_seconds":  extracted_age_s,
        },
        "counts": {
            "events":    len(events_out),
            "pinned":    len(pinned_out),
            "deadlines": len(deadlines_out),
        },
        "days_ahead": days_ahead,
    })


# ─── Signal refresh endpoint (manual or button-triggered) ─────────────────────
_signals_refresh_lock = threading.Lock()
_signals_refreshing = False

# Debouncer for watcher-driven signal refreshes. When new emails land in a
# burst, each call resets the timer; the extractor runs once after the burst
# settles. Keeps us from spawning 10 parallel Haiku passes in the same minute.
_signals_debounce_lock  = threading.Lock()
_signals_debounce_timer: threading.Timer | None = None


def _run_signals_refresh(cfg, *, days_back: int = 14, limit: int = 80) -> None:
    """Synchronous extraction → save. Used by both the manual endpoint and the
    watcher-driven debouncer. Single-flight enforced via _signals_refresh_lock.
    """
    global _signals_refreshing
    with _signals_refresh_lock:
        if _signals_refreshing:
            return
        _signals_refreshing = True
    try:
        from engram.memory.signal_extractor import (
            extract_recent_email_signals,
            save_signals,
        )
        user = cfg.identity.user_name or ""
        sigs = extract_recent_email_signals(
            memory_path = cfg.memory_path,
            user_name   = user,
            cfg         = cfg,
            days_back   = days_back,
            limit       = limit,
        )
        save_signals(sigs, cfg.memory_path)
        print(f"[signals] refresh done: {sigs['scanned']} scanned, "
              f"{len(sigs['deadlines'])} surfaced, "
              f"{sigs['filtered_out']['past']} past + "
              f"{sigs['filtered_out']['already_responded']} responded filtered out",
              flush=True)
    except Exception:
        print("[signals] refresh error:\n" + traceback.format_exc(), flush=True)
    finally:
        with _signals_refresh_lock:
            _signals_refreshing = False


def _schedule_signals_refresh(cfg, *, delay_s: int = 30) -> None:
    """Schedule (or reschedule) a background signals refresh in `delay_s`.

    Multiple emails arriving within `delay_s` collapse into one run. Called
    from the watcher's _ingest after a successful email write.
    """
    global _signals_debounce_timer
    with _signals_debounce_lock:
        if _signals_debounce_timer is not None:
            try:
                _signals_debounce_timer.cancel()
            except Exception:
                pass
        t = threading.Timer(delay_s, _run_signals_refresh, args=(cfg,))
        t.daemon = True
        t.name   = "engram-signals-debounced"
        _signals_debounce_timer = t
        t.start()


@app.route("/api/signals/refresh", methods=["POST"])
def signals_refresh():
    """Kick off a background extraction pass over recent emails.

    Returns immediately with status. The Top of Mind tab can poll
    /api/top-of-mind to see updated deadlines once the pass completes.
    Locks so concurrent presses don't pile up parallel extractions.
    """
    cfg = get_cfg()
    body = request.get_json(force=True, silent=True) or {}
    days_back = int(body.get("days_back", 14))
    limit     = int(body.get("limit", 80))

    with _signals_refresh_lock:
        if _signals_refreshing:
            return jsonify({"status": "already_running"})

    threading.Thread(
        target=_run_signals_refresh,
        kwargs={"cfg": cfg, "days_back": days_back, "limit": limit},
        daemon=True, name="engram-signals",
    ).start()
    return jsonify({"status": "started", "days_back": days_back, "limit": limit})


# ─── Main page ────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/health")
@app.route("/top-of-mind")
def index():
    cfg          = get_cfg()
    if request.path == "/health":
        active_tab = "health"
    elif request.path == "/top-of-mind":
        active_tab = "top-of-mind"
    else:
        active_tab = "chat"
    return _build_html(cfg, active_tab), 200, {"Content-Type": "text/html; charset=utf-8"}


def _build_html(cfg: EngramConfig, active_tab: str = "chat") -> str:
    system = cfg.identity.system_name
    org    = cfg.identity.org_name
    user   = cfg.identity.user_name or "User"

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{system}</title>
<style>
/* ── Reset ── */
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
html, body {{ height: 100%; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
        background: #fff; color: #1a1a1a; font-size: 14px; line-height: 1.5; }}

/* ── Layout ── */
.shell {{ display: flex; flex-direction: column; height: 100vh; }}

/* ── Topbar ── */
.topbar {{ display: flex; align-items: center; gap: 0; padding: 0 24px;
          background: #fff; border-bottom: 1px solid #e8e8e8;
          height: 52px; flex-shrink: 0; }}
.logo {{ display: flex; align-items: center; gap: 10px; margin-right: 32px; text-decoration: none; }}
.logo-mark {{ width: 28px; height: 28px; border-radius: 7px;
              background: linear-gradient(135deg, #D97757 0%, #b85c38 100%);
              display: flex; align-items: center; justify-content: center;
              font-size: 14px; flex-shrink: 0; }}
.logo-name {{ font-size: 15px; font-weight: 700; color: #1a1a1a; letter-spacing: -0.3px; }}
.logo-org  {{ font-size: 11px; color: #999; margin-left: 4px; font-weight: 400; }}
.tabs {{ display: flex; gap: 0; height: 100%; }}
.tab {{ display: flex; align-items: center; padding: 0 18px; font-size: 13px; font-weight: 500;
        color: #666; text-decoration: none; border-bottom: 2px solid transparent;
        margin-bottom: -1px; transition: color .15s; cursor: pointer; }}
.tab:hover {{ color: #1a1a1a; }}
.tab.active {{ color: #1a1a1a; border-bottom-color: #D97757; font-weight: 600; }}
.topbar-right {{ margin-left: auto; display: flex; align-items: center; gap: 10px; }}
.badge-live {{ display: flex; align-items: center; gap: 5px; font-size: 11px; color: #34A853;
               background: rgba(52,168,83,.08); padding: 3px 9px; border-radius: 20px;
               border: 1px solid rgba(52,168,83,.2); }}
.badge-live-dot {{ width: 6px; height: 6px; border-radius: 50%; background: #34A853;
                   animation: pulse 2.5s infinite; }}
@keyframes pulse {{ 0%,100%{{opacity:1}} 50%{{opacity:.3}} }}

/* ── Tab panels ── */
.tab-panel {{ display: none; flex: 1; overflow: hidden; }}
.tab-panel.active {{ display: flex; }}

/* ═══════════════════════════════════════════
   CHAT PANEL
═══════════════════════════════════════════ */
.chat-layout {{ display: flex; width: 100%; height: 100%; overflow: hidden; }}

/* Context sidebar */
.ctx-sidebar {{ width: 260px; flex-shrink: 0; background: #fafafa; border-right: 1px solid #e8e8e8;
                display: flex; flex-direction: column; overflow: hidden; }}
.ctx-header {{ padding: 14px 16px 10px; border-bottom: 1px solid #efefef; }}
.ctx-title {{ font-size: 11px; font-weight: 700; text-transform: uppercase;
              letter-spacing: 1.5px; color: #999; }}
.ctx-count {{ font-size: 11px; color: #bbb; margin-top: 2px; }}
.ctx-list {{ flex: 1; overflow-y: auto; padding: 8px 0; }}
.ctx-item {{ padding: 5px 16px; display: flex; align-items: flex-start; gap: 7px; }}
.ctx-dot {{ width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0; margin-top: 5px; }}
.ctx-dot.memory {{ background: #4285F4; }}
.ctx-dot.direct {{ background: #4285F4; }}
.ctx-dot.graph  {{ background: #34A853; }}
.ctx-dot.wiki   {{ background: #D97757; }}
.ctx-file {{ font-size: 11px; color: #555; line-height: 1.4; word-break: break-word; }}
.ctx-empty {{ padding: 12px 16px; font-size: 11px; color: #bbb; font-style: italic; }}
.ctx-reasoning {{ padding: 0 16px 8px; font-size: 10px; color: #aaa; font-style: italic;
                  line-height: 1.5; min-height: 0; }}
.ctx-legend {{ padding: 8px 16px; border-top: 1px solid #efefef; display: flex;
               gap: 10px; flex-wrap: wrap; }}
.ctx-legend-item {{ display: flex; align-items: center; gap: 4px;
                    font-size: 10px; color: #aaa; }}

/* Context inspector actions */
.ctx-item {{ display: flex; align-items: flex-start; gap: 7px; padding: 4px 8px 4px 16px;
             position: relative; }}
.ctx-item:hover .ctx-actions {{ opacity: 1; }}
.ctx-actions {{ display: flex; gap: 2px; margin-left: auto; opacity: 0;
                transition: opacity .15s; flex-shrink: 0; }}
.ctx-btn {{ background: none; border: none; cursor: pointer; padding: 1px 3px;
            font-size: 10px; color: #bbb; border-radius: 3px; line-height: 1; }}
.ctx-btn:hover {{ color: #555; background: #eee; }}
.ctx-btn.pinned {{ color: #D97757; opacity: 1 !important; }}
.ctx-add-bar {{ padding: 8px 12px; border-top: 1px solid #efefef; display: flex; flex-direction: column; gap: 6px; }}
.ctx-harvest-btn {{ width: 100%; background: transparent; border: 1px solid #e6e6e6;
                    color: #666; padding: 7px 10px; font-size: 11px; border-radius: 7px;
                    cursor: pointer; text-align: left; transition: background .15s, border-color .15s;
                    display: flex; justify-content: space-between; align-items: center; }}
.ctx-harvest-btn:hover {{ background: #f7f7f7; border-color: #ddd; color: #333; }}
.ctx-harvest-btn:disabled {{ opacity: 0.5; cursor: not-allowed; }}
#harvest-btn-status {{ font-size: 10px; color: #aaa; font-weight: 400; }}
.ctx-add-btn {{ width: 100%; background: #fafafa; border: 1px dashed #ddd;
                border-radius: 6px; padding: 5px 10px; font-size: 11px; color: #aaa;
                cursor: pointer; text-align: center; transition: all .15s; }}
.ctx-add-btn:hover {{ background: #f0f0f0; color: #666; border-color: #ccc; }}

/* Add raw doc modal */
.ctx-modal-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.3); z-index: 100;
                       display: flex; align-items: center; justify-content: center; }}
.ctx-modal {{ background: #fff; border-radius: 12px; padding: 20px; width: 480px;
              max-height: 70vh; display: flex; flex-direction: column; gap: 12px;
              box-shadow: 0 8px 32px rgba(0,0,0,.15); }}
.ctx-modal-title {{ font-size: 14px; font-weight: 700; color: #1a1a1a; }}
.ctx-modal-sub   {{ font-size: 11px; color: #aaa; margin-top: -8px; }}
.ctx-modal textarea {{ flex: 1; border: 1px solid #ddd; border-radius: 8px; padding: 10px;
                        font-size: 12px; font-family: inherit; resize: none; outline: none;
                        min-height: 160px; line-height: 1.5; }}
.ctx-modal textarea:focus {{ border-color: #D97757; }}
.ctx-modal-row {{ display: flex; gap: 8px; }}
.ctx-modal-name {{ flex: 1; border: 1px solid #ddd; border-radius: 6px; padding: 6px 10px;
                   font-size: 12px; font-family: inherit; outline: none; }}
.ctx-modal-name:focus {{ border-color: #D97757; }}
.ctx-modal-actions {{ display: flex; gap: 8px; justify-content: flex-end; }}
.ctx-modal-cancel {{ padding: 6px 14px; border-radius: 6px; border: 1px solid #ddd;
                      background: #fff; font-size: 12px; cursor: pointer; }}
.ctx-modal-add {{ padding: 6px 14px; border-radius: 6px; border: none;
                   background: #D97757; color: #fff; font-size: 12px; cursor: pointer;
                   font-weight: 600; }}
.ctx-modal-add:hover {{ background: #c06040; }}
.ctx-modal-upload-zone {{ border: 2px dashed #ddd; border-radius: 8px; padding: 20px;
                            text-align: center; cursor: pointer; transition: all .15s;
                            background: #fafafa; }}
.ctx-modal-upload-zone:hover {{ border-color: #D97757; background: #fff8f5; }}
.ctx-modal-upload-zone.uploading {{ opacity: .6; pointer-events: none; }}
.ctx-modal-upload-icon {{ font-size: 24px; margin-bottom: 6px; }}
.ctx-modal-upload-label {{ font-size: 13px; color: #555; font-weight: 500; }}
.ctx-modal-upload-sub {{ font-size: 11px; color: #bbb; margin-top: 3px; }}
.ctx-modal-divider {{ display: flex; align-items: center; gap: 10px; color: #bbb;
                       font-size: 11px; margin: 4px 0; }}
.ctx-modal-divider::before, .ctx-modal-divider::after {{ content: ''; flex: 1;
    height: 1px; background: #efefef; }}

/* Context item animations */
.ctx-item.ctx-adding   {{ animation: ctx-add .4s ease-out; }}
.ctx-item.ctx-removing {{ animation: ctx-remove .3s ease-in forwards; overflow: hidden; }}
@keyframes ctx-add    {{ from {{ opacity:0; transform:translateX(-6px); }} to {{ opacity:1; transform:translateX(0); }} }}
@keyframes ctx-remove {{ to   {{ opacity:0; max-height:0; padding-top:0; padding-bottom:0; }} }}

/* Clickable ctx item body */
.ctx-item-body {{ flex: 1; cursor: pointer; display: flex; align-items: flex-start;
                   gap: 7px; min-width: 0; overflow: hidden; }}
.ctx-item-body:hover .ctx-file {{ color: #D97757; }}

/* Request-context pill (shown when model requests more context) */
.rc-pill {{ display: inline-flex; align-self: flex-start; align-items: center; gap: 5px;
             padding: 3px 10px; background: rgba(66,133,244,.08);
             border: 1px solid rgba(66,133,244,.18); border-radius: 20px;
             font-size: 11px; color: #4285F4; margin-bottom: 8px;
             max-width: 100%; overflow: hidden; text-overflow: ellipsis;
             white-space: nowrap; }}

/* Export bar */
.export-bar {{ display: flex; align-items: center; gap: 6px; margin-top: 8px;
               padding: 0 2px; opacity: 0; transition: opacity .2s;
               flex-wrap: wrap; }}
.message.assistant:hover .export-bar,
.export-bar.visible {{ opacity: 1; }}
.export-label {{ font-size: 10px; color: #bbb; text-transform: uppercase;
                  letter-spacing: 1px; margin-right: 2px; white-space: nowrap; }}
.export-btn {{ display: inline-flex; align-items: center; gap: 4px;
               padding: 3px 9px; border-radius: 5px; border: 1px solid #ddd;
               background: #fff; font-size: 11px; color: #555; cursor: pointer;
               transition: all .15s; white-space: nowrap; }}
.export-btn:hover {{ border-color: #D97757; color: #D97757; background: #fff8f5; }}
.export-btn.loading {{ opacity: .5; pointer-events: none; }}
.export-btn.done {{ border-color: #34A853; color: #34A853; }}

/* Pin button — appears below assistant bubbles, lets user pin to Top of Mind */
.pin-btn {{ display: inline-flex; align-items: center; gap: 4px; margin-top: 8px;
            padding: 4px 11px; border-radius: 14px; border: 1px solid #e0e0e0;
            background: #fff; font-size: 11px; color: #777; cursor: pointer;
            transition: all .15s; opacity: 0; }}
.message.assistant:hover .pin-btn,
.pin-btn.pinned {{ opacity: 1; }}
.pin-btn:hover {{ border-color: #D97757; color: #D97757; }}
.pin-btn.pinned {{ border-color: #D97757; color: #D97757; background: #fff8f5; font-weight: 600; }}
.pin-btn:disabled {{ opacity: .5; cursor: wait; }}

/* Outputs panel in sidebar */
.outputs-section {{ border-top: 1px solid #efefef; padding: 8px 0; }}
.outputs-title {{ font-size: 10px; font-weight: 700; text-transform: uppercase;
                   letter-spacing: 1.5px; color: #bbb; padding: 4px 16px 6px; }}
.output-item {{ display: flex; align-items: center; gap: 7px; padding: 3px 16px;
                font-size: 11px; color: #666; cursor: default; }}
.output-ext {{ font-size: 9px; font-weight: 700; text-transform: uppercase;
               padding: 1px 5px; border-radius: 3px; background: #f0f0f0;
               color: #888; flex-shrink: 0; }}
.output-ext.docx {{ background: #dbeafe; color: #2563eb; }}
.output-ext.pptx {{ background: #fce7f3; color: #be185d; }}
.output-ext.pdf  {{ background: #fee2e2; color: #dc2626; }}
.output-ext.md   {{ background: #f3f4f6; color: #6b7280; }}
.output-name {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}

/* File preview modal */
.fp-overlay {{ position: fixed; inset: 0; background: rgba(0,0,0,.45); z-index: 200;
               display: none; align-items: center; justify-content: center; }}
.fp-overlay.open {{ display: flex; }}
.fp-modal {{ background: #fff; border-radius: 12px; width: 700px; max-width: 92vw;
              max-height: 82vh; display: flex; flex-direction: column;
              box-shadow: 0 12px 48px rgba(0,0,0,.22); overflow: hidden; }}
.fp-head {{ padding: 14px 18px; border-bottom: 1px solid #efefef;
             display: flex; align-items: center; gap: 10px; flex-shrink: 0; }}
.fp-name {{ font-size: 13px; font-weight: 700; color: #1a1a1a; flex: 1;
             overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
.fp-path {{ font-size: 10px; color: #bbb; margin-top: 2px; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }}
.fp-close {{ background: none; border: none; cursor: pointer; font-size: 20px;
              color: #aaa; padding: 2px 6px; border-radius: 4px; line-height: 1;
              flex-shrink: 0; }}
.fp-close:hover {{ color: #333; background: #f0f0f0; }}
.fp-body {{ flex: 1; overflow-y: auto; padding: 20px 22px; }}
.fp-body pre {{ font-size: 12px; font-family: 'SF Mono', 'Fira Code', ui-monospace, monospace;
                white-space: pre-wrap; word-break: break-word; color: #333; line-height: 1.65; }}
.fp-loading {{ color: #ccc; font-size: 12px; font-style: italic; }}

/* Health detail cards */
.hd-card {{ border: 1px solid #efefef; border-radius: 10px; padding: 14px 16px;
             margin-bottom: 12px; background: #fafafa; transition: opacity .3s; }}
.hd-claim {{ background: #fff; border: 1px solid #e8e8e8; border-radius: 8px; padding: 10px 12px; }}
.hd-claim.claim-a {{ border-left: 3px solid #3b82f6; }}
.hd-claim.claim-b {{ border-left: 3px solid #f59e0b; }}
.hd-claim-label {{ font-size: 10px; font-weight: 700; color: #aaa; text-transform: uppercase;
                    letter-spacing: .5px; margin-bottom: 5px; }}
.hd-claim-text {{ font-size: 12px; color: #1a1a1a; line-height: 1.5; margin-bottom: 4px; }}
.hd-claim-meta  {{ font-size: 10px; color: #bbb; }}
.hd-actions {{ display: flex; gap: 6px; flex-wrap: wrap; }}
.hd-btn {{ font-size: 11px; font-weight: 600; border: none; border-radius: 6px;
            padding: 5px 12px; cursor: pointer; transition: opacity .15s; }}
.hd-btn:hover {{ opacity: .8; }}
.hd-btn:disabled {{ opacity: .45; cursor: default; }}
.hd-btn-a          {{ background: #ebf5ff; color: #1d4ed8; }}
.hd-btn-b          {{ background: #fef3c7; color: #92400e; }}
.hd-btn-both-true  {{ background: #dcfce7; color: #15803d; }}
.hd-btn-both-false {{ background: #fee2e2; color: #b91c1c; }}
.hd-btn-resolve    {{ background: #dcfce7; color: #15803d; }}
.hd-btn-dismiss    {{ background: #f3f4f6; color: #6b7280; }}

/* Cascade toast */
.hd-toast {{ position: fixed; bottom: 24px; left: 50%; transform: translateX(-50%);
              background: #1a1a1a; color: #fff; padding: 10px 18px; border-radius: 8px;
              font-size: 12px; font-weight: 500; box-shadow: 0 6px 20px rgba(0,0,0,.25);
              z-index: 300; opacity: 0; transition: opacity .25s; pointer-events: none; }}
.hd-toast.show {{ opacity: 1; }}

/* Chat main */
.chat-main {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
.messages {{ flex: 1; overflow-y: auto; padding: 24px 0; }}
.message {{ width: 100%; margin: 0 0 20px; padding: 0 28px; }}
.message.user   {{ display: flex; flex-direction: column; align-items: flex-end; }}
.message.assistant {{ display: flex; flex-direction: column; align-items: flex-start; }}
.bubble {{ padding: 12px 18px; border-radius: 12px; font-size: 14px; line-height: 1.65;
           white-space: pre-wrap; }}
.message.user .bubble {{ background: #1a1a1a; color: #fff; border-radius: 12px 12px 2px 12px;
                          max-width: 72%; }}
.message.assistant .bubble {{ background: #f5f5f5; color: #1a1a1a; border-radius: 12px 12px 12px 2px;
                               width: 100%; }}
.bubble a {{ color: #4285F4; }}
.typing {{ display: inline-block; }}
.typing::after {{ content: '▋'; animation: blink .7s infinite; }}
@keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:0}} }}

/* Interruption — bubble cut off mid-stream */
.bubble.interrupted {{ opacity: .65; border-left: 2px solid #D97757; padding-left: 16px; }}
.interrupted-mark {{ font-size: 11px; color: #D97757; margin-top: 4px;
                     letter-spacing: 0.3px; font-weight: 600; }}

/* Queued user message — sent during a stream, awaiting next turn */
.queued-badge {{ font-size: 10.5px; font-weight: 600; color: #888;
                 background: #f0f0f0; border: 1px solid #e0e0e0;
                 padding: 2px 8px; border-radius: 10px; margin-top: 6px;
                 letter-spacing: 0.2px; align-self: flex-end; }}

/* Empty state */
.empty-state {{ display: flex; flex-direction: column; align-items: center; justify-content: center;
                height: 100%; gap: 8px; color: #ccc; }}
.empty-icon {{ font-size: 36px; margin-bottom: 4px; }}
.empty-title {{ font-size: 16px; font-weight: 600; color: #999; }}
.empty-sub {{ font-size: 13px; color: #bbb; }}

/* Input bar */
.input-bar {{ padding: 16px 28px; border-top: 1px solid #e8e8e8; background: #fff; }}
.input-wrap {{ display: flex; gap: 10px; align-items: flex-end; }}
.input-box {{ flex: 1; border: 1px solid #ddd; border-radius: 10px; padding: 10px 14px;
              font-size: 14px; font-family: inherit; resize: none; outline: none;
              line-height: 1.5; max-height: 160px; overflow-y: auto;
              transition: border-color .15s; }}
.input-box:focus {{ border-color: #D97757; }}
.send-btn {{ width: 38px; height: 38px; border-radius: 8px; border: none; cursor: pointer;
             background: #D97757; color: #fff; display: flex; align-items: center;
             justify-content: center; font-size: 16px; flex-shrink: 0;
             transition: background .15s, opacity .15s; }}
.send-btn:hover {{ background: #c06040; }}
.send-btn:disabled {{ background: #ddd; cursor: not-allowed; }}

/* ═══════════════════════════════════════════
   HEALTH PANEL
═══════════════════════════════════════════ */
.health-layout {{ width: 100%; height: 100%; overflow-y: auto; background: #fafafa; }}
.health-inner {{ max-width: 1200px; margin: 0 auto; padding: 28px 32px; }}
.health-title {{ font-size: 20px; font-weight: 700; color: #1a1a1a; margin-bottom: 4px; }}
.health-sub   {{ font-size: 13px; color: #999; margin-bottom: 28px; }}

.pillars {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }}
.pillar {{ background: #fff; border-radius: 12px; border: 1px solid #e8e8e8;
           padding: 22px 20px; overflow: hidden; }}
.pillar-stripe {{ height: 3px; border-radius: 2px; margin: -22px -20px 18px; }}
.pillar-1 .pillar-stripe {{ background: #D97757; }}
.pillar-2 .pillar-stripe {{ background: #4285F4; }}
.pillar-3 .pillar-stripe {{ background: #34A853; }}
.pillar-num {{ font-size: 9px; font-weight: 700; letter-spacing: 2.5px;
               text-transform: uppercase; color: #bbb; margin-bottom: 6px; }}
.pillar-title-text {{ font-size: 18px; font-weight: 800; letter-spacing: -0.4px; margin-bottom: 4px; }}
.pillar-1 .pillar-title-text {{ color: #D97757; }}
.pillar-2 .pillar-title-text {{ color: #4285F4; }}
.pillar-3 .pillar-title-text {{ color: #34A853; }}
.pillar-sub {{ font-size: 11px; color: #aaa; line-height: 1.6; margin-bottom: 20px; }}

.section {{ margin-bottom: 20px; }}
.section-label {{ font-size: 9px; font-weight: 700; letter-spacing: 2px;
                  text-transform: uppercase; color: #bbb; margin-bottom: 10px; }}
.pillar-1 .section-label {{ color: #c97050; }}
.pillar-2 .section-label {{ color: #4285F4; }}
.pillar-3 .section-label {{ color: #34A853; }}

.big-stats {{ display: grid; grid-template-columns: repeat(3,1fr); gap: 8px; margin-bottom: 4px; }}
.big-stat {{ background: #fafafa; border: 1px solid #efefef; border-radius: 8px;
             padding: 10px 8px; text-align: center; }}
.big-stat .val {{ font-size: 22px; font-weight: 800; letter-spacing: -1px; color: #1a1a1a; }}
.big-stat .lbl {{ font-size: 10px; color: #aaa; margin-top: 2px; }}

.topic-bars {{ display: flex; flex-direction: column; gap: 5px; }}
.topic-row {{ display: flex; align-items: center; gap: 8px; }}
.topic-name {{ font-size: 11px; color: #888; width: 88px; flex-shrink: 0; text-align: right; }}
.topic-bar-wrap {{ flex: 1; background: #f0f0f0; border-radius: 3px; height: 5px; overflow: hidden; }}
.topic-bar {{ height: 100%; border-radius: 3px; transition: width .5s; opacity: .8; }}
.topic-count {{ font-size: 10px; color: #bbb; width: 28px; }}

.mem-table, .type-table {{ width: 100%; border-collapse: collapse; }}
.mem-table tr, .type-table tr {{ border-bottom: 1px solid #f0f0f0; }}
.mem-table td, .type-table td {{ padding: 4px 3px; font-size: 11px; color: #666; }}
.mem-table td:last-child, .type-table td:last-child {{ text-align: right; color: #aaa; }}
.type-table td:last-child {{ color: #34A853; font-weight: 600; }}

.run-history {{ display: flex; align-items: flex-end; gap: 3px; height: 40px;
                background: #f5f5f5; border-radius: 7px; padding: 4px 6px; }}
.run-bar-wrap {{ flex: 1; height: 100%; display: flex; flex-direction: column; justify-content: flex-end; }}
.run-bar {{ width: 100%; border-radius: 2px 2px 0 0; background: #4285F4; min-height: 3px;
            opacity: .7; transition: opacity .15s; }}
.run-bar:hover {{ opacity: 1; }}
.run-bar.partial {{ background: #a0bce8; }}

.phases {{ display: flex; flex-direction: column; gap: 0; }}
.phase-row {{ display: flex; align-items: center; gap: 8px; padding: 5px 0;
              border-bottom: 1px solid #f5f5f5; }}
.phase-name {{ font-size: 11px; color: #555; width: 160px; flex-shrink: 0; }}
.phase-detail {{ font-size: 10px; color: #aaa; }}

.tier-bars {{ display: flex; height: 7px; border-radius: 4px; overflow: hidden;
              gap: 2px; margin-bottom: 7px; }}
.tier-bar {{ border-radius: 2px; min-width: 2px; opacity: .85; }}
.tier-bar.tier-working      {{ background: #FBBC04; }}
.tier-bar.tier-episodic     {{ background: #4285F4; }}
.tier-bar.tier-semantic     {{ background: #D97757; }}
.tier-bar.tier-crystallised {{ background: #34A853; }}
.tier-legend {{ display: flex; gap: 10px; flex-wrap: wrap; }}
.tier-item {{ display: flex; align-items: center; gap: 4px; font-size: 10px; color: #aaa; }}
.tier-dot {{ width: 7px; height: 7px; border-radius: 50%; }}
.tier-dot.tier-working      {{ background: #FBBC04; }}
.tier-dot.tier-episodic     {{ background: #4285F4; }}
.tier-dot.tier-semantic     {{ background: #D97757; }}
.tier-dot.tier-crystallised {{ background: #34A853; }}

.meter {{ display: flex; align-items: center; gap: 8px; margin-bottom: 5px; }}
.meter-label {{ font-size: 10px; color: #aaa; width: 78px; flex-shrink: 0; }}
.meter-track {{ flex: 1; height: 5px; background: #f0f0f0; border-radius: 3px; overflow: hidden; }}
.meter-fill {{ height: 100%; background: #34A853; border-radius: 3px; transition: width .5s; }}
.meter-val {{ font-size: 10px; color: #888; width: 28px; text-align: right; }}

.alert-row {{ display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }}
.alert-chip {{ padding: 3px 10px; border-radius: 20px; font-size: 10px; font-weight: 600; cursor: pointer; }}
.alert-chip.warn {{ background: rgba(251,188,4,.1); color: #a07800; border: 1px solid rgba(251,188,4,.25); }}
.alert-chip.info {{ background: rgba(66,133,244,.08); color: #4285F4; border: 1px solid rgba(66,133,244,.2); }}
.alert-chip.ok   {{ background: rgba(52,168,83,.08); color: #34A853; border: 1px solid rgba(52,168,83,.2); }}

.community-list {{ display: flex; flex-wrap: wrap; gap: 6px; }}
.community-chip {{ background: #fafafa; border: 1px solid #e8e8e8; border-radius: 7px;
                   padding: 4px 10px; display: flex; align-items: center; gap: 6px; }}
.chip-label {{ font-size: 11px; color: #444; }}
.chip-size  {{ font-size: 10px; color: #bbb; }}

.mode-row {{ display: flex; align-items: flex-start; gap: 10px; padding: 8px 0;
             border-bottom: 1px solid #f5f5f5; }}
.mode-accent {{ width: 3px; flex-shrink: 0; margin-top: 3px; height: 30px; border-radius: 2px; }}
.mode-label {{ font-size: 12px; font-weight: 600; color: #333; }}
.mode-desc  {{ font-size: 10px; color: #aaa; margin-top: 2px; line-height: 1.5; }}

.loading {{ color: #ccc; font-size: 12px; padding: 16px 0; text-align: center; }}

/* ═══════════════════════════════════════════
   TOP OF MIND PANEL
═══════════════════════════════════════════ */
.tom-layout {{ width: 100%; height: 100%; overflow-y: auto; background: #fafafa; }}
.tom-inner  {{ max-width: 1200px; margin: 0 auto; padding: 28px 32px; }}
.tom-title  {{ font-size: 20px; font-weight: 700; color: #1a1a1a; margin-bottom: 4px; }}
.tom-sub    {{ font-size: 13px; color: #999; margin-bottom: 28px; }}

.tom-cols {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 20px; }}
@media (max-width: 980px) {{ .tom-cols {{ grid-template-columns: 1fr; }} }}

.tom-col {{ background: #fff; border-radius: 12px; border: 1px solid #e8e8e8;
            padding: 22px 20px; overflow: hidden; }}
.tom-col-stripe {{ height: 3px; border-radius: 2px; margin: -22px -20px 18px; }}
.tom-col.events    .tom-col-stripe {{ background: #4285F4; }}
.tom-col.proposals .tom-col-stripe {{ background: #D97757; }}
.tom-col.questions .tom-col-stripe {{ background: #34A853; }}
.tom-col-num   {{ font-size: 9px; font-weight: 700; letter-spacing: 2.5px;
                  text-transform: uppercase; color: #bbb; margin-bottom: 6px; }}
.tom-col-title {{ font-size: 18px; font-weight: 800; letter-spacing: -0.4px; margin-bottom: 4px; }}
.tom-col.events    .tom-col-title {{ color: #4285F4; }}
.tom-col.proposals .tom-col-title {{ color: #D97757; }}
.tom-col.questions .tom-col-title {{ color: #34A853; }}
.tom-col-sub   {{ font-size: 11px; color: #aaa; line-height: 1.6; margin-bottom: 16px; }}
.tom-col-count {{ font-size: 11px; color: #888; font-weight: 600; }}

.tom-list  {{ display: flex; flex-direction: column; gap: 0; }}
.tom-item  {{ padding: 9px 0; border-bottom: 1px solid #f4f4f4; }}
.tom-item:last-child {{ border-bottom: none; }}
.tom-item-row {{ display: flex; align-items: baseline; gap: 8px; }}
.tom-item-meta {{ font-size: 10px; color: #aaa; flex-shrink: 0; min-width: 70px;
                  font-variant-numeric: tabular-nums; }}
.tom-item-main {{ flex: 1; font-size: 12px; color: #333; line-height: 1.45;
                  word-break: break-word; }}
.tom-item-aux  {{ font-size: 10px; color: #999; margin-top: 2px; }}
.tom-item-path {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
                  font-size: 10.5px; color: #555; word-break: break-all; }}
.tom-empty {{ font-size: 11px; color: #ccc; font-style: italic;
              padding: 16px 0; text-align: center; }}

.tom-pri {{ display: inline-block; padding: 1px 6px; border-radius: 8px;
            font-size: 9.5px; font-weight: 700; text-transform: uppercase;
            letter-spacing: 0.5px; margin-right: 6px; vertical-align: middle; }}
.tom-pri.high   {{ background: rgba(217,119,87,.12); color: #c06040; }}
.tom-pri.medium {{ background: rgba(66,133,244,.10); color: #4285F4; }}
.tom-pri.low    {{ background: rgba(150,150,150,.10); color: #888; }}

.tom-day-header {{ font-size: 10px; font-weight: 700; letter-spacing: 1.5px;
                   text-transform: uppercase; color: #bbb;
                   padding: 12px 0 4px; border-top: 1px solid #f0f0f0; }}
.tom-day-header:first-child {{ border-top: none; padding-top: 0; }}

.tom-tags {{ display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }}
.tom-tag  {{ font-size: 9.5px; font-weight: 600; padding: 1px 7px;
             border-radius: 8px; background: rgba(66,133,244,.10);
             color: #4285F4; text-transform: lowercase; letter-spacing: 0.2px; }}

/* "💬 chat about this" button on Top of Mind tiles. Hover-reveals on the
   parent .tom-item so the list stays clean at rest. */
.tom-chat-btn {{ display: inline-flex; align-items: center; gap: 4px;
                 margin-top: 6px; padding: 3px 10px; border-radius: 12px;
                 border: 1px solid #e0e0e0; background: #fff;
                 font-size: 10.5px; color: #777; cursor: pointer;
                 opacity: 0; transition: opacity .15s, color .15s, border-color .15s; }}
.tom-item:hover .tom-chat-btn {{ opacity: 1; }}
.tom-chat-btn:hover {{ color: #D97757; border-color: #D97757; background: #fff8f5; }}
</style>
</head>
<body>
<div class="shell">

<!-- ── Topbar ── -->
<div class="topbar">
  <a class="logo" href="/">
    <div class="logo-mark">🧠</div>
    <span class="logo-name">engram<span class="logo-org">{org}</span></span>
  </a>
  <nav class="tabs">
    <a class="tab {'active' if active_tab == 'chat' else ''}" href="/" data-tab="chat" id="tab-chat" onclick="return switchTab(event, 'chat')">Chat</a>
    <a class="tab {'active' if active_tab == 'top-of-mind' else ''}" href="/top-of-mind" data-tab="top-of-mind" id="tab-top-of-mind" onclick="return switchTab(event, 'top-of-mind')">Top of Mind</a>
    <a class="tab {'active' if active_tab == 'health' else ''}" href="/health" data-tab="health" id="tab-health" onclick="return switchTab(event, 'health')">Engram Health</a>
  </nav>
  <div class="topbar-right">
    <div class="badge-live"><div class="badge-live-dot"></div>Live</div>
  </div>
</div>

<!-- ══════════════════════ CHAT PANEL ══════════════════════ -->
<div class="tab-panel {'active' if active_tab == 'chat' else ''}" id="panel-chat">
  <div class="chat-layout">

    <!-- Context sidebar -->
    <div class="ctx-sidebar">
      <div class="ctx-header">
        <div class="ctx-title">Active Context</div>
        <div class="ctx-count" id="ctx-count">Waiting for query…</div>
      </div>
      <div class="ctx-reasoning" id="ctx-reasoning"></div>
      <div class="ctx-list" id="ctx-list">
        <div class="ctx-empty">Context files will appear here after your first message.</div>
      </div>
      <div class="ctx-add-bar">
        <button class="ctx-add-btn" onclick="openRawDocModal()">+ Add document</button>
        <button class="ctx-harvest-btn" id="harvest-btn" onclick="triggerHarvest()"
                title="Extract proposals from this session's conversation now (otherwise auto-runs hourly)">
          <span id="harvest-btn-label">↑ Harvest this session</span>
          <span id="harvest-btn-status"></span>
        </button>
      </div>
      <div class="ctx-legend">
        <div class="ctx-legend-item"><div class="ctx-dot memory"></div>memory</div>
        <div class="ctx-legend-item"><div class="ctx-dot graph"></div>graph</div>
        <div class="ctx-legend-item"><div class="ctx-dot wiki"></div>wiki</div>
        <div class="ctx-legend-item"><div class="ctx-dot" style="background:#a855f7"></div>raw</div>
      </div>

      <!-- Outputs panel -->
      <div class="outputs-section" id="outputs-section" style="display:none">
        <div class="outputs-title-row" style="display:flex;align-items:center;justify-content:space-between;padding:4px 16px 6px">
          <div class="outputs-title" style="padding:0">Recent outputs</div>
          <button id="outputs-folder-btn" onclick="openOutputsFolder()" title="Show in Finder"
                  style="background:none;border:none;cursor:pointer;font-size:11px;color:#888;padding:2px 6px;border-radius:4px">&#128193; Folder</button>
        </div>
        <div id="outputs-list"></div>
      </div>
    </div>

    <!-- Chat main -->
    <div class="chat-main">
      <div class="messages" id="messages">
        <div class="empty-state" id="empty-state">
          <div class="empty-icon">🧠</div>
          <div class="empty-title">engram</div>
          <div class="empty-sub">Ask anything — context assembles automatically</div>
        </div>
      </div>
      <div class="input-bar">
        <div class="input-wrap">
          <textarea class="input-box" id="input" placeholder="Ask anything…" rows="1"></textarea>
          <button class="send-btn" id="send-btn" onclick="sendMessage()">↑</button>
        </div>
      </div>
    </div>

  </div>
</div>

<!-- ══════════════════════ TOP OF MIND PANEL ══════════════════════ -->
<div class="tab-panel {'active' if active_tab == 'top-of-mind' else ''}" id="panel-top-of-mind">
  <div class="tom-layout">
    <div class="tom-inner">
      <div class="tom-title">Top of Mind</div>
      <div class="tom-sub">Upcoming events, pending decisions, open questions · refreshes every 60s</div>
      <div class="tom-cols" id="tom-cols"><div class="loading">Loading…</div></div>
    </div>
  </div>
</div>

<!-- ══════════════════════ HEALTH PANEL ══════════════════════ -->
<div class="tab-panel {'active' if active_tab == 'health' else ''}" id="panel-health">
  <div class="health-layout">
    <div class="health-inner">
      <div class="health-title">Engram Health</div>
      <div class="health-sub">Three-pillar knowledge system · refreshes every 30s</div>
      <div class="pillars" id="pillars"><div class="loading">Loading…</div></div>
    </div>
  </div>
</div>

</div><!-- .shell -->

<!-- ── Health detail modal ───────────────────────────────────────────────── -->
<div class="fp-overlay" id="health-detail-overlay" onclick="closeHealthDetail(event)">
  <div class="fp-modal" onclick="event.stopPropagation()">
    <div class="fp-head">
      <div class="fp-name" id="health-detail-title">Detail</div>
      <button class="fp-close" onclick="closeHealthDetail()">×</button>
    </div>
    <div class="fp-body" id="health-detail-body" style="padding:16px 20px">
      <div class="fp-loading">Loading…</div>
    </div>
  </div>
</div>

<!-- ── File preview modal ─────────────────────────────────────────────── -->
<div class="fp-overlay" id="fp-overlay" onclick="closeFilePreview(event)">
  <div class="fp-modal" onclick="event.stopPropagation()">
    <div class="fp-head">
      <div style="flex:1;min-width:0">
        <div class="fp-name" id="fp-name"></div>
        <div class="fp-path" id="fp-path"></div>
      </div>
      <button class="fp-close" onclick="closeFilePreview()">×</button>
    </div>
    <div class="fp-body"><pre id="fp-content" class="fp-loading">Loading…</pre></div>
  </div>
</div>

<!-- ── Raw doc modal ─────────────────────────────────────────────────────── -->
<div class="ctx-modal-overlay" id="raw-doc-modal" style="display:none" onclick="closeRawDocModal(event)">
  <div class="ctx-modal" onclick="event.stopPropagation()">
    <div class="ctx-modal-title">Add document to context</div>
    <div class="ctx-modal-sub">Upload a file or paste content directly. Injected immediately into this conversation's context window.</div>
    <div class="ctx-modal-upload-zone" id="raw-doc-dropzone" onclick="document.getElementById('raw-doc-file').click()">
      <input type="file" id="raw-doc-file" style="display:none"
             accept=".md,.txt,.pdf,.docx,.pptx,.eml,.vtt,.csv"
             onchange="handleFileUpload(this)">
      <div class="ctx-modal-upload-icon">📎</div>
      <div class="ctx-modal-upload-label">Click to upload or drag &amp; drop</div>
      <div class="ctx-modal-upload-sub">.pdf · .docx · .pptx · .md · .txt</div>
    </div>
    <div class="ctx-modal-divider"><span>or paste content</span></div>
    <div class="ctx-modal-row">
      <input class="ctx-modal-name" id="raw-doc-name" placeholder="Document name (optional)" />
    </div>
    <textarea id="raw-doc-content" placeholder="Paste document content here…"></textarea>
    <div class="ctx-modal-actions">
      <button class="ctx-modal-cancel" onclick="closeRawDocModal()">Cancel</button>
      <button class="ctx-modal-add" onclick="addRawDoc()">Add to context</button>
    </div>
  </div>
</div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
const TOPIC_COLORS = {{
  competition:'#4285F4', concepts:'#D97757', decisions:'#FBBC04',
  people:'#34A853', problems:'#EA4335', projects:'#E8906E', systems:'#00ACC1',
}};

let messages      = [];
let streaming     = false;
let activeContext = [];      // [{{path, type}}, ...]
let pinnedPaths   = new Set(); // paths the user has pinned
let rawDocs       = [];      // [{{name, content}}, ...] injected by user

// One stable id per browser tab. Sent with every /api/chat request so the
// server can append turns to MEMORY/sessions/<YYYY-MM>/chat_<id>.md, making
// past conversations retrievable by future sessions via the curator's scan.
// `let` (not const) so restoring a pinned conversation can swap it, and so
// "Chat about this" actions on Top of Mind can mint a fresh session.
function _newSessionId() {{
  try {{
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID()
      : 'sess_' + Date.now().toString(36) + Math.random().toString(36).slice(2,8);
  }} catch (e) {{
    return 'sess_' + Date.now();
  }}
}}
let SESSION_ID = _newSessionId();

// Tracks how many (user, assistant) pairs have been completed in this
// session — used as the turn_index when pinning an answer.
let TURN_INDEX = 0;

// ── Auto-resize textarea ───────────────────────────────────────────────────
const input = document.getElementById('input');
input.addEventListener('input', () => {{
  input.style.height = 'auto';
  input.style.height = Math.min(input.scrollHeight, 160) + 'px';
}});
input.addEventListener('keydown', e => {{
  if (e.key === 'Enter' && !e.shiftKey) {{ e.preventDefault(); sendMessage(); }}
}});

// ── Chat ───────────────────────────────────────────────────────────────────
// Human-like interruption model:
//   - Send button never disables. Typing during a response is allowed.
//   - When user sends mid-response, we ask /api/chat/interject. A Haiku
//     classifier decides: keep current thought going (continue) or pivot.
//   - On pivot: in-flight stream stops at next chunk, partial bubble gets
//     an "interrupted" marker, then a fresh response starts with a system
//     prompt note so the model phrases its reply naturally.
//   - On continue: the queued user bubble gets a small "queued" badge and
//     fires automatically after the current response finishes.
let currentAssistantEl = null;
let pendingFollowup    = null;     // {{text, was_interruption, reason, userBubble}}
let interjectInFlight  = false;

function sendMessage() {{
  const text = input.value.trim();
  if (!text) return;
  input.value = ''; input.style.height = 'auto';
  document.getElementById('empty-state')?.remove();

  // Render the user bubble immediately — feels like speaking up.
  const userEl = appendBubble('user', text);

  if (!streaming) {{
    messages.push({{role: 'user', content: text}});
    _doSend(text, {{was_interruption: false, reason: ''}});
    return;
  }}

  // Streaming in progress — don't queue blindly. Ask the classifier.
  if (interjectInFlight) {{
    // Already deciding on a previous interjection; tag this bubble queued
    // and let it ride after the next [DONE].
    _addQueuedBadge(userEl, 'waiting…');
    pendingFollowup = {{text, was_interruption: false, reason: '', userBubble: userEl}};
    return;
  }}
  interjectInFlight = true;
  _addQueuedBadge(userEl, 'deciding…');

  fetch('/api/chat/interject', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify({{message: text}}),
  }}).then(r => r.json()).then(verdict => {{
    interjectInFlight = false;
    if (verdict.action === 'no_active_chat') {{
      // Race: stream finished while classifier was deciding — just send.
      _removeQueuedBadge(userEl);
      messages.push({{role: 'user', content: text}});
      _doSend(text, {{was_interruption: false, reason: ''}});
      return;
    }}
    if (verdict.action === 'pivot') {{
      // Mark partial as cut off; a [DONE] will arrive shortly. Send the new
      // message after that.
      _markCurrentInterrupted();
      _setQueuedBadge(userEl, 'pivoting →');
      pendingFollowup = {{text, was_interruption: true, reason: verdict.reason || '', userBubble: userEl}};
    }} else {{
      // continue — wait for current response to finish, then fire.
      _setQueuedBadge(userEl, 'queued — will answer next');
      pendingFollowup = {{text, was_interruption: false, reason: '', userBubble: userEl}};
    }}
  }}).catch(() => {{
    interjectInFlight = false;
    _setQueuedBadge(userEl, 'queued');
    pendingFollowup = {{text, was_interruption: false, reason: '', userBubble: userEl}};
  }});
}}

function _addQueuedBadge(userEl, label) {{
  let badge = userEl.querySelector('.queued-badge');
  if (!badge) {{
    badge = document.createElement('div');
    badge.className   = 'queued-badge';
    userEl.appendChild(badge);
  }}
  badge.textContent = label;
}}
function _setQueuedBadge(userEl, label) {{ _addQueuedBadge(userEl, label); }}
function _removeQueuedBadge(userEl) {{
  userEl.querySelector('.queued-badge')?.remove();
}}
function _markCurrentInterrupted() {{
  if (!currentAssistantEl) return;
  const bubble = currentAssistantEl.querySelector('.bubble');
  if (!bubble) return;
  bubble.classList.remove('typing');
  bubble.classList.add('interrupted');
  let mark = currentAssistantEl.querySelector('.interrupted-mark');
  if (!mark) {{
    mark = document.createElement('div');
    mark.className   = 'interrupted-mark';
    mark.textContent = '⏸ interrupted';
    currentAssistantEl.appendChild(mark);
  }}
}}

function _doSend(text, opts) {{
  const assistantEl = appendBubble('assistant', '');
  assistantEl.querySelector('.bubble').classList.add('typing');
  currentAssistantEl = assistantEl;
  streaming = true;

  // Reset context sidebar
  activeContext = [];
  document.getElementById('ctx-list').innerHTML = '<div class="ctx-empty">Scanning memory…</div>';
  document.getElementById('ctx-count').textContent = 'Scanning memory…';
  document.getElementById('ctx-reasoning').textContent = '';

  const body = {{
    messages,
    raw_docs:            rawDocs,
    pinned:              [...pinnedPaths],
    was_interruption:    !!opts.was_interruption,
    interruption_reason: opts.reason || '',
    session_id:          SESSION_ID,
  }};

  fetch('/api/chat', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify(body),
  }}).then(res => {{
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '', assistantText = '';

    function pump() {{
      reader.read().then(({{done, value}}) => {{
        if (done) {{
          assistantEl.querySelector('.bubble').classList.remove('typing');
          messages.push({{role:'assistant', content: assistantText}});
          if (assistantText.trim()) {{
            addExportBar(assistantEl, assistantText);
            // This is now a complete turn — let the user pin it.
            const myTurn = TURN_INDEX++;
            addPinButton(assistantEl, myTurn);
          }}
          streaming = false;
          currentAssistantEl = null;

          // Drain any pending follow-up: the user's queued/interjected message.
          if (pendingFollowup) {{
            const f = pendingFollowup;
            pendingFollowup = null;
            _removeQueuedBadge(f.userBubble);
            messages.push({{role: 'user', content: f.text}});
            _doSend(f.text, {{was_interruption: f.was_interruption, reason: f.reason}});
          }}
          return;
        }}
        buf += decoder.decode(value, {{stream: true}});
        const parts = buf.split('\\n\\n');
        buf = parts.pop();
        for (const part of parts) {{
          if (!part.startsWith('data: ')) continue;
          const raw = part.slice(6).trim();
          if (raw === '[DONE]') continue;
          try {{
            const j = JSON.parse(raw);
            if (j.context)        renderContext(j.context);
            if (j.context_update) applyContextUpdate(j.context_update);
            if (j.interrupted) {{
              // Server confirms it stopped streaming — make sure UI reflects.
              _markCurrentInterrupted();
            }}
            if (j.clear_response) {{
              assistantText = '';
              const bubble = assistantEl.querySelector('.bubble');
              bubble.textContent = '';
              bubble.classList.add('typing');
            }}
            if (j.request_context) {{
              const pill = document.createElement('div');
              pill.className   = 'rc-pill';
              pill.textContent = '🔍 Fetching: ' + j.request_context.query;
              const bubble = assistantEl.querySelector('.bubble');
              assistantEl.insertBefore(pill, bubble);
            }}
            if (j.token) {{
              assistantText += j.token;
              const bubble = assistantEl.querySelector('.bubble');
              bubble.textContent = assistantText;
              if (!bubble.classList.contains('typing')) bubble.classList.add('typing');
            }}
            if (j.error) {{
              const bubble = assistantEl.querySelector('.bubble');
              bubble.textContent = '⚠ ' + j.error;
              bubble.classList.remove('typing');
            }}
          }} catch(e) {{}}
        }}
        scrollToBottom();
        pump();
      }});
    }}
    pump();
  }}).catch(err => {{
    assistantEl.querySelector('.bubble').textContent = '⚠ Connection error: ' + err.message;
    assistantEl.querySelector('.bubble').classList.remove('typing');
    streaming = false;
    currentAssistantEl = null;
  }});
}}

function appendBubble(role, text) {{
  const wrap = document.createElement('div');
  wrap.className = 'message ' + role;
  wrap.innerHTML = '<div class="bubble"></div>';
  wrap.querySelector('.bubble').textContent = text;
  document.getElementById('messages').appendChild(wrap);
  scrollToBottom();
  return wrap;
}}

function scrollToBottom() {{
  const m = document.getElementById('messages');
  m.scrollTop = m.scrollHeight;
}}

function makeCtxItem(path, type) {{
  const label    = path.split('/').pop().replace(/\\.md$/, '');
  const isPinned = pinnedPaths.has(path);
  const isRaw    = path.startsWith('__raw__/');

  const el = document.createElement('div');
  el.className    = 'ctx-item';
  el.dataset.path = path;

  // Clickable body (dot + name) — opens file preview
  const body = document.createElement('div');
  body.className = 'ctx-item-body';

  const dot = document.createElement('div');
  dot.className = 'ctx-dot ' + type;
  dot.style.marginTop = '5px';
  dot.style.flexShrink = '0';

  const nameEl = document.createElement('div');
  nameEl.className   = 'ctx-file';
  nameEl.textContent = label;

  body.appendChild(dot);
  body.appendChild(nameEl);
  if (!isRaw) body.addEventListener('click', () => openFilePreview(path));

  // Pin / remove actions (revealed on hover)
  const actions = document.createElement('div');
  actions.className = 'ctx-actions';

  const pinBtn = document.createElement('button');
  pinBtn.className   = 'ctx-btn' + (isPinned ? ' pinned' : '');
  pinBtn.title       = isPinned ? 'Unpin' : 'Pin (keep in context)';
  pinBtn.textContent = '📌';
  pinBtn.addEventListener('click', (e) => {{ e.stopPropagation(); togglePin(path, pinBtn); }});

  const removeBtn = document.createElement('button');
  removeBtn.className   = 'ctx-btn';
  removeBtn.title       = 'Remove from context';
  removeBtn.textContent = '✕';
  removeBtn.addEventListener('click', (e) => {{ e.stopPropagation(); removeFile(path); }});

  actions.appendChild(pinBtn);
  actions.appendChild(removeBtn);

  el.appendChild(body);
  el.appendChild(actions);
  return el;
}}

function togglePin(path, btn) {{
  if (pinnedPaths.has(path)) {{
    pinnedPaths.delete(path);
    btn.classList.remove('pinned');
    btn.title = 'Pin (keep in context)';
  }} else {{
    pinnedPaths.add(path);
    btn.classList.add('pinned');
    btn.title = 'Unpin';
  }}
}}

function removeFile(path) {{
  if (pinnedPaths.has(path)) return;  // can't remove pinned
  const list = document.getElementById('ctx-list');
  for (const el of list.querySelectorAll('.ctx-item')) {{
    if (el.dataset.path === path) {{
      el.classList.add('ctx-removing');
      setTimeout(() => el.remove(), 320);
      break;
    }}
  }}
  activeContext = activeContext.filter(c => c.path !== path);
  const countEl = document.getElementById('ctx-count');
  const m = countEl.textContent.match(/of (\\d+)/);
  const total = m ? m[1] : '?';
  countEl.textContent = activeContext.length + ' of ' + total + ' candidate' + (total > 1 ? 's' : '');
}}

// ── File preview ───────────────────────────────────────────────────────────
async function openFilePreview(path) {{
  const overlay   = document.getElementById('fp-overlay');
  const nameEl    = document.getElementById('fp-name');
  const pathEl    = document.getElementById('fp-path');
  const contentEl = document.getElementById('fp-content');

  nameEl.textContent    = path.split('/').pop();
  pathEl.textContent    = path;
  contentEl.textContent = 'Loading…';
  contentEl.className   = 'fp-loading';
  overlay.classList.add('open');

  try {{
    const res  = await fetch('/api/file?path=' + encodeURIComponent(path));
    const data = await res.json();
    contentEl.className = '';
    contentEl.textContent = data.error ? ('⚠ ' + data.error) : data.content;
  }} catch (err) {{
    contentEl.className   = '';
    contentEl.textContent = '⚠ Failed to load: ' + err.message;
  }}
}}

function closeFilePreview(e) {{
  if (!e || e.target === document.getElementById('fp-overlay')) {{
    document.getElementById('fp-overlay').classList.remove('open');
  }}
}}

// ── Export ─────────────────────────────────────────────────────────────────
const EXPORT_FMTS = [
  {{ fmt: 'md',   icon: '📋', label: 'MD'   }},
  {{ fmt: 'docx', icon: '📝', label: 'DOCX' }},
  {{ fmt: 'pptx', icon: '📊', label: 'PPTX' }},
  {{ fmt: 'pdf',  icon: '📄', label: 'PDF'  }},
];

// ── Pin / unpin chat answers ─────────────────────────────────────────────
function addPinButton(msgEl, turnIndex) {{
  const btn = document.createElement('button');
  btn.className   = 'pin-btn';
  btn.title       = 'Pin this answer to your Top of Mind';
  btn.dataset.ti  = String(turnIndex);
  btn.textContent = '📌 Pin';
  btn.onclick = async () => {{
    if (btn.disabled) return;
    btn.disabled = true;
    if (btn.dataset.pinned === '1') {{
      // Unpin
      try {{
        await fetch('/api/pinned/remove', {{
          method:  'POST',
          headers: {{'Content-Type': 'application/json'}},
          body:    JSON.stringify({{session_id: SESSION_ID, turn_index: turnIndex}}),
        }});
        btn.dataset.pinned = '';
        btn.dataset.pinId  = '';
        btn.textContent    = '📌 Pin';
        btn.classList.remove('pinned');
      }} catch (e) {{}}
    }} else {{
      // Pin
      try {{
        const r = await fetch('/api/pinned/add', {{
          method:  'POST',
          headers: {{'Content-Type': 'application/json'}},
          body:    JSON.stringify({{session_id: SESSION_ID, turn_index: turnIndex}}),
        }});
        const j = await r.json();
        if (j.pin_id) {{
          btn.dataset.pinned = '1';
          btn.dataset.pinId  = j.pin_id;
          btn.textContent    = '📍 Pinned';
          btn.classList.add('pinned');
        }}
      }} catch (e) {{}}
    }}
    btn.disabled = false;
  }};
  msgEl.appendChild(btn);
  return btn;
}}

// Open the chat tab as a fresh session, pre-filled with a question and
// (optionally) some files pinned in the curator's view. Used by the "💬"
// buttons on Top of Mind tiles. Doesn't auto-send — user can edit the
// question first.
function startChatWithContext(opts) {{
  const question     = (opts && opts.question)     || '';
  const contextPaths = (opts && opts.contextPaths) || [];

  // Switch tab in-place (preserves URL via pushState).
  switchTab(null, 'chat');

  // Fresh session — new SESSION_ID, blank message log, sidebar context
  // populated only with whatever the caller pinned.
  SESSION_ID    = _newSessionId();
  messages      = [];
  rawDocs       = [];
  activeContext = [];
  pinnedPaths   = new Set(contextPaths);
  TURN_INDEX    = 0;
  pendingFollowup = null;

  const msgsEl = document.getElementById('messages');
  if (msgsEl) msgsEl.innerHTML = '';
  document.getElementById('empty-state')?.remove();

  // Render pinned paths in the sidebar so the user sees what's loaded.
  const list = document.getElementById('ctx-list');
  if (list) list.innerHTML = '';
  contextPaths.forEach(p => {{
    activeContext.push({{path: p, type: 'memory'}});
    const el = makeCtxItem(p, 'memory');
    el.dataset.path = p;
    if (list) list.appendChild(el);
  }});
  const countEl  = document.getElementById('ctx-count');
  const reasonEl = document.getElementById('ctx-reasoning');
  if (countEl) {{
    countEl.textContent = contextPaths.length
      ? `${{contextPaths.length}} pinned for this question`
      : 'Scanning memory…';
  }}
  if (reasonEl) reasonEl.textContent = '';

  // Pre-fill the input + focus. Auto-resize the textarea like sendMessage does.
  const inputEl = document.getElementById('input');
  if (inputEl) {{
    inputEl.value = question;
    inputEl.style.height = 'auto';
    inputEl.style.height = Math.min(inputEl.scrollHeight, 160) + 'px';
    inputEl.focus();
    // Move cursor to end so user can append / edit naturally
    try {{ inputEl.setSelectionRange(question.length, question.length); }} catch (e) {{}}
  }}
}}

// Delegated handler for the "💬 chat about this" buttons on Top of Mind
// tiles. We avoid stuffing a JSON payload into onclick="" because user
// content (apostrophes, quotes, em-dashes) can break attribute parsing.
// Instead the relevant fields ride on data-chat-q and data-chat-paths.
function startChatFromTile(btn) {{
  if (!btn) return;
  const question = btn.dataset.chatQ || '';
  const pathsRaw = btn.dataset.chatPaths || '';
  const paths    = pathsRaw ? pathsRaw.split(',').map(s => s.trim()).filter(Boolean) : [];
  startChatWithContext({{question, contextPaths: paths}});
}}

async function restorePinnedConversation(pinId) {{
  if (!pinId) return;
  let r;
  try {{
    r = await fetch('/api/pinned/restore?pin_id=' + encodeURIComponent(pinId));
  }} catch (e) {{
    alert('Could not reach server'); return;
  }}
  const j = await r.json();
  if (j.error) {{ alert('Restore failed: ' + j.error); return; }}

  // Switch to the chat tab in-place (preserves URL via pushState).
  switchTab(null, 'chat');

  // Hard reset of chat state, then rehydrate from the server's view.
  SESSION_ID = j.session_id;
  messages   = [];
  rawDocs    = [];
  activeContext = [];
  pinnedPaths   = new Set(j.active_context || []);
  TURN_INDEX = 0;
  pendingFollowup = null;

  const msgsEl = document.getElementById('messages');
  if (msgsEl) msgsEl.innerHTML = '';
  document.getElementById('empty-state')?.remove();

  // Rehydrate bubbles. j.messages is alternating user/assistant.
  for (let i = 0; i < j.messages.length; i += 2) {{
    const u = j.messages[i];
    const a = j.messages[i + 1];
    if (u) {{
      messages.push(u);
      appendBubble('user', u.content || '');
    }}
    if (a) {{
      messages.push(a);
      const aEl = appendBubble('assistant', a.content || '');
      if ((a.content || '').trim()) {{
        addExportBar(aEl, a.content);
        const ti = TURN_INDEX++;
        const pinBtn = addPinButton(aEl, ti);
        // Pre-mark the originally-pinned turn
        if (ti === j.turn_index) {{
          pinBtn.dataset.pinned = '1';
          pinBtn.dataset.pinId  = j.pin && j.pin.pin_id;
          pinBtn.textContent    = '📍 Pinned';
          pinBtn.classList.add('pinned');
        }}
      }}
    }}
  }}

  // Update sidebar count + render pinned context as preserved files.
  const list = document.getElementById('ctx-list');
  if (list) list.innerHTML = '';
  (j.active_context || []).forEach(p => {{
    activeContext.push({{path: p, type: 'memory'}});
    const el = makeCtxItem(p, 'memory');
    el.dataset.path = p;
    if (list) list.appendChild(el);
  }});
  const countEl = document.getElementById('ctx-count');
  if (countEl) countEl.textContent = `${{activeContext.length}} restored from pinned turn`;
  const reasonEl = document.getElementById('ctx-reasoning');
  if (reasonEl) reasonEl.textContent = `Resumed conversation from pin · turn ${{j.turn_index + 1}} of ${{j.total_turns}}`;

  scrollToBottom();
}}

function _agoStr(iso) {{
  if (!iso) return '';
  try {{
    const t = new Date(iso).getTime();
    const m = Math.round((Date.now() - t) / 60000);
    if (m < 1)   return 'just now';
    if (m < 60)  return `${{m}}m ago`;
    if (m < 1440) return `${{Math.round(m/60)}}h ago`;
    return `${{Math.round(m/1440)}}d ago`;
  }} catch (e) {{ return ''; }}
}}

function addExportBar(msgEl, content) {{
  const bar = document.createElement('div');
  bar.className = 'export-bar';

  const label = document.createElement('span');
  label.className   = 'export-label';
  label.textContent = 'Export as';
  bar.appendChild(label);

  // Derive a filename from the first non-empty line of the response
  const firstLine = content.split('\\n').find(l => l.trim()) || 'engram_export';
  const filename  = firstLine.replace(/^#+\\s*/, '').replace(/\\*\\*/g, '')
                             .replace(/[^a-zA-Z0-9_\\- ]/g, '').trim()
                             .replace(/\\s+/g, '_').slice(0, 48) || 'engram_export';

  EXPORT_FMTS.forEach(({{fmt, icon, label: lbl}}) => {{
    const btn = document.createElement('button');
    btn.className   = 'export-btn';
    btn.textContent = icon + ' ' + lbl;
    btn.title       = 'Export as ' + lbl;
    btn.addEventListener('click', () => exportAs(fmt, content, filename, btn));
    bar.appendChild(btn);
  }});

  msgEl.appendChild(bar);
}}

async function exportAs(fmt, content, filename, btn) {{
  if (btn) {{ btn.classList.add('loading'); btn.textContent = '⏳ ' + btn.textContent.slice(2); }}
  try {{
    const res = await fetch('/api/export', {{
      method: 'POST',
      headers: {{'Content-Type': 'application/json'}},
      body: JSON.stringify({{format: fmt, content, filename}}),
    }});
    if (!res.ok) {{ throw new Error('Export failed (' + res.status + ')'); }}

    const note = res.headers.get('X-Export-Note');
    const blob = await res.blob();
    const cd   = res.headers.get('Content-Disposition') || '';
    const name = cd.match(/filename="([^"]+)"/)?.[1] || filename + '.' + fmt;

    // Trigger browser download
    const url = URL.createObjectURL(blob);
    const a   = document.createElement('a');
    a.href = url; a.download = name; a.click();
    URL.revokeObjectURL(url);

    if (btn) {{ btn.classList.remove('loading'); btn.classList.add('done'); }}
    if (note) {{ alert(note); }}

    // Refresh outputs list in sidebar
    refreshOutputs();
  }} catch (err) {{
    if (btn) {{ btn.classList.remove('loading'); }}
    alert('Export error: ' + err.message);
  }}
}}

// ── Manual session harvest ─────────────────────────────────────────────────
async function triggerHarvest() {{
  const btn   = document.getElementById('harvest-btn');
  const lbl   = document.getElementById('harvest-btn-label');
  const stat  = document.getElementById('harvest-btn-status');
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  lbl.textContent  = 'Harvesting…';
  stat.textContent = '';
  try {{
    const res = await fetch('/api/sessions/harvest', {{
      method:  'POST',
      headers: {{'Content-Type': 'application/json'}},
      body:    JSON.stringify({{session_id: SESSION_ID, force: true}}),
    }});
    const j = await res.json();
    if (j.ran) {{
      lbl.textContent  = `↑ ${{j.added}} proposal${{j.added === 1 ? '' : 's'}} queued`;
      stat.textContent = `${{j.new_turns}} new turn${{j.new_turns === 1 ? '' : 's'}}`;
    }} else {{
      lbl.textContent  = '↑ Harvest this session';
      stat.textContent = j.reason || 'nothing new';
    }}
  }} catch (e) {{
    lbl.textContent  = '↑ Harvest this session';
    stat.textContent = 'error';
  }} finally {{
    btn.disabled = false;
    setTimeout(refreshHarvestStatus, 1000);
  }}
}}

async function refreshHarvestStatus() {{
  try {{
    const r = await fetch('/api/sessions/harvest-status?session_id=' + encodeURIComponent(SESSION_ID));
    const j = await r.json();
    const stat = document.getElementById('harvest-btn-status');
    if (!stat) return;
    if (j.last_harvested_at && j.age_seconds != null) {{
      const m = Math.round(j.age_seconds / 60);
      const ageStr = m < 60 ? `${{m}}m ago` : `${{Math.round(m/60)}}h ago`;
      stat.textContent = `last: ${{ageStr}}`;
    }} else {{
      stat.textContent = 'never';
    }}
  }} catch (e) {{}}
}}

// Auto-refresh status every 30s + on load
setInterval(refreshHarvestStatus, 30000);
setTimeout(refreshHarvestStatus, 1500);


let _outputsDir = null;

async function refreshOutputs() {{
  try {{
    const res  = await fetch('/api/outputs');
    const data = await res.json();
    const list = document.getElementById('outputs-list');
    const sec  = document.getElementById('outputs-section');
    _outputsDir = data.dir || null;

    if (!data.files || !data.files.length) {{
      sec.style.display = 'none';
      return;
    }}
    sec.style.display = 'block';
    list.innerHTML = '';
    data.files.slice(0, 8).forEach(f => {{
      const item = document.createElement('div');
      item.className = 'output-item';
      item.title     = f.name + ' — click to open';
      item.style.cursor = 'pointer';
      const cleanName = f.name.replace(/_\\d{{8}}_\\d{{6}}(\\.[^.]+)$/, '$1');
      const nameSpan = document.createElement('span');
      nameSpan.className = 'output-name';
      nameSpan.textContent = cleanName;
      const extSpan = document.createElement('span');
      extSpan.className = 'output-ext ' + f.ext;
      extSpan.textContent = f.ext;
      item.appendChild(extSpan);
      item.appendChild(nameSpan);
      const revealBtn = document.createElement('button');
      revealBtn.textContent = '⤴';
      revealBtn.title = 'Reveal in Finder';
      revealBtn.style.cssText = 'background:none;border:none;cursor:pointer;color:#bbb;font-size:11px;padding:0 2px';
      revealBtn.onclick = (e) => {{ e.stopPropagation(); openPath(f.abspath, true); }};
      item.appendChild(revealBtn);
      item.onclick = () => openPath(f.abspath, false);
      list.appendChild(item);
    }});
  }} catch (e) {{}}
}}

async function openPath(path, reveal) {{
  try {{
    await fetch('/api/open-path', {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ path, reveal }})
    }});
  }} catch (e) {{}}
}}

function openOutputsFolder() {{
  if (_outputsDir) openPath(_outputsDir, false);
}}

// Load outputs on page start
refreshOutputs();

// ── Raw doc modal ──────────────────────────────────────────────────────────
// Uploads do double duty: the extracted text is added to rawDocs for the
// current session, AND the original bytes go to the watcher's inbox so the
// file enters the long-term pipeline (cleaner → memory → graph) for future
// chats. For paste-only content we POST /api/inbox-save in parallel.
let _lastUploadInboxPath = null;   // set by /api/upload, consumed by addRawDoc

function openRawDocModal() {{
  document.getElementById('raw-doc-modal').style.display = 'flex';
  document.getElementById('raw-doc-content').focus();
  _lastUploadInboxPath = null;
}}

function closeRawDocModal(e) {{
  if (!e || e.target === document.getElementById('raw-doc-modal')) {{
    document.getElementById('raw-doc-modal').style.display = 'none';
  }}
}}

async function handleFileUpload(input) {{
  const file = input.files[0];
  if (!file) return;

  const zone = document.getElementById('raw-doc-dropzone');
  zone.classList.add('uploading');
  zone.querySelector('.ctx-modal-upload-label').textContent = 'Extracting text…';

  const form = new FormData();
  form.append('file', file);

  try {{
    const res  = await fetch('/api/upload', {{ method: 'POST', body: form }});
    const data = await res.json();

    if (data.error) {{
      alert('Upload error: ' + data.error);
    }} else {{
      document.getElementById('raw-doc-name').value    = data.name;
      document.getElementById('raw-doc-content').value = data.content;
      _lastUploadInboxPath = data.saved_to_inbox || null;
      const inboxNote = _lastUploadInboxPath ? '  ·  saved for future sessions' : '';
      zone.querySelector('.ctx-modal-upload-label').textContent =
        '✓ ' + file.name + ' (' + data.chars.toLocaleString() + ' chars)' + inboxNote;
    }}
  }} catch (err) {{
    alert('Upload failed: ' + err.message);
  }} finally {{
    zone.classList.remove('uploading');
    input.value = '';
  }}
}}

function addRawDoc() {{
  const name    = (document.getElementById('raw-doc-name').value.trim() || 'raw_doc_' + (rawDocs.length + 1));
  const content = document.getElementById('raw-doc-content').value.trim();
  if (!content) return;

  rawDocs.push({{name, content}});

  // Show in sidebar immediately
  const list = document.getElementById('ctx-list');
  const el   = makeCtxItem('__raw__/' + name, 'raw');
  el.dataset.path = '__raw__/' + name;
  el.classList.add('ctx-adding');
  list.insertBefore(el, list.firstChild);
  activeContext.unshift({{path: '__raw__/' + name, type: 'raw'}});

  // Update count
  const countEl = document.getElementById('ctx-count');
  const m = countEl.textContent.match(/of (\\d+)/);
  const total = m ? m[1] : '?';
  countEl.textContent = activeContext.length + ' of ' + total + ' candidate' + (Number(total) !== 1 ? 's' : '');

  // If the content didn't come from a file upload (i.e. user pasted directly),
  // ship a markdown copy to the inbox so it enters long-term pipeline. Fire
  // and forget — failure here just means no long-term persistence; the
  // current chat still has the rawDoc in context.
  if (!_lastUploadInboxPath) {{
    fetch('/api/inbox-save', {{
      method:  'POST',
      headers: {{'Content-Type': 'application/json'}},
      body:    JSON.stringify({{name, content}}),
    }}).catch(() => {{}});
  }}
  _lastUploadInboxPath = null;

  document.getElementById('raw-doc-name').value    = '';
  document.getElementById('raw-doc-content').value = '';
  closeRawDocModal();
}}

function renderContext(ctx) {{
  const phase  = ctx.phase  || 'ready';
  const total  = ctx.candidates_total || 0;
  const sel    = ctx.selected || [];
  const reason = ctx.reasoning || '';

  const countEl    = document.getElementById('ctx-count');
  const reasonEl   = document.getElementById('ctx-reasoning');
  const list       = document.getElementById('ctx-list');

  if (phase === 'scanning') {{
    countEl.textContent  = 'Scanning memory…';
    reasonEl.textContent = '';
    list.innerHTML = '<div class="ctx-empty">Scanning…</div>';
    return;
  }}

  if (phase === 'curating') {{
    countEl.textContent  = total + ' candidates — curating…';
    reasonEl.textContent = '';
    // Keep list as-is (still shows scanning placeholder)
    return;
  }}

  // phase === 'ready' — preserve pinned and raw docs across turns
  const preserved = activeContext.filter(c =>
    pinnedPaths.has(c.path) || c.path.startsWith('__raw__/')
  );
  const merged = [
    ...preserved,
    ...sel.filter(c => !preserved.find(p => p.path === c.path)),
  ];
  activeContext = merged;
  const n = merged.length;
  countEl.textContent  = n + ' of ' + total + ' candidate' + (total !== 1 ? 's' : '');
  reasonEl.textContent = reason;

  list.innerHTML = '';
  for (const f of merged) {{
    list.appendChild(makeCtxItem(f.path, f.type));
  }}
  if (!n) list.innerHTML = '<div class="ctx-empty">No context loaded</div>';
}}

function applyContextUpdate(upd) {{
  const toAdd    = upd.add    || [];
  const toRemove = upd.remove || [];
  const reason   = upd.reason || '';

  if (!toAdd.length && !toRemove.length) return;

  const list    = document.getElementById('ctx-list');
  const reasonEl = document.getElementById('ctx-reasoning');
  reasonEl.textContent = '↻ ' + reason;

  // Remove items (animate out) — skip pinned files
  for (const f of toRemove) {{
    if (pinnedPaths.has(f.path)) continue;        // never remove pinned
    if (f.path.startsWith('__raw__/')) continue;  // never remove user-added docs
    for (const el of list.querySelectorAll('.ctx-item')) {{
      if (el.dataset.path === f.path) {{
        el.classList.add('ctx-removing');
        setTimeout(() => el.remove(), 320);
        break;
      }}
    }}
    activeContext = activeContext.filter(c => c.path !== f.path);
  }}

  // Add items (animate in)
  for (const f of toAdd) {{
    if (!activeContext.find(c => c.path === f.path)) {{
      activeContext.push(f);
      const el = makeCtxItem(f.path, f.type);
      el.classList.add('ctx-adding');
      list.appendChild(el);
    }}
  }}

  // Update count
  const countEl = document.getElementById('ctx-count');
  const m = countEl.textContent.match(/of (\\d+)/);
  const total = m ? m[1] : '?';
  countEl.textContent = activeContext.length + ' of ' + total + ' candidate' + (total > 1 ? 's' : '');
}}

// ── Top of Mind tab ────────────────────────────────────────────────────────
function loadTopOfMind() {{
  const panel = document.getElementById('panel-top-of-mind');
  if (!panel || !panel.classList.contains('active')) return;
  fetch('/api/top-of-mind').then(r => r.json()).then(renderTopOfMind).catch(() => {{}});
}}

function escHtml(s) {{
  return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                      .replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}}

function renderTopOfMind(data) {{
  const events    = data.events    || [];
  const pinned    = data.pinned    || [];
  const deadlines = data.deadlines || [];

  // High-stakes events grouped by day, tagged with why-they-surface
  let evHtml = '';
  if (events.length === 0) {{
    evHtml = '<div class="tom-empty">Nothing executive-grade scheduled in the next ' + (data.days_ahead || 14) + ' days.</div>';
  }} else {{
    let lastDay = '';
    evHtml = '<div class="tom-list">' + events.map(e => {{
      let header = '';
      if (e.date !== lastDay) {{
        header = `<div class="tom-day-header">${{escHtml(e.date)}}</div>`;
        lastDay = e.date;
      }}
      const tags = (e.tags || []).slice(0,3).map(t =>
        `<span class="tom-tag">${{escHtml(t)}}</span>`
      ).join('');
      const loc = e.location ? `<div class="tom-item-aux">${{escHtml(e.location)}}</div>` : '';
      const org = e.organizer ? `<span style="color:#aaa;font-size:11px"> · ${{escHtml(e.organizer)}}</span>` : '';
      // "💬 chat" → opens chat tab with a pre-prep question for this meeting.
      // No paths pinned — the calendar agenda is already injected into the
      // system prompt for any chat turn, so the model has full event context.
      const eventQuestion = `Help me prep for "${{e.summary}}" on ${{e.date}} at ${{e.time}}. What should I know going in, and what is at stake?`;
      return header + `
        <div class="tom-item">
          <div class="tom-item-row">
            <div class="tom-item-meta">${{escHtml(e.time)}}</div>
            <div class="tom-item-main">${{escHtml(e.summary)}}${{org}}</div>
          </div>
          ${{tags ? `<div class="tom-tags">${{tags}}</div>` : ''}}
          ${{loc}}
          <button class="tom-chat-btn" data-chat-q="${{escHtml(eventQuestion)}}" onclick="startChatFromTile(this)">💬 chat about this</button>
        </div>`;
    }}).join('') + '</div>';
  }}

  // Pinned chat answers — click to restore the conversation in the Chat tab
  let pnHtml = '';
  if (pinned.length === 0) {{
    pnHtml = '<div class="tom-empty">No pinned answers yet. Pin one with the 📌 below any chat reply.</div>';
  }} else {{
    pnHtml = '<div class="tom-list">' + pinned.map(p => {{
      const ageStr = _agoStr(p.pinned_at);
      return `
        <div class="tom-item" onclick="restorePinnedConversation('${{escHtml(p.pin_id)}}')" style="cursor:pointer">
          <div class="tom-item-main">
            <div style="font-weight:600;font-size:12.5px;margin-bottom:3px">${{escHtml(p.title)}}</div>
            ${{p.assistant_snippet ? `<div class="tom-item-aux" style="font-style:italic;color:#666;line-height:1.45">${{escHtml(p.assistant_snippet.slice(0, 200))}}${{p.assistant_snippet.length > 200 ? '…' : ''}}</div>` : ''}}
            <div class="tom-item-aux" style="margin-top:5px;color:#aaa">📌 ${{ageStr}} · click to resume →</div>
          </div>
        </div>`;
    }}).join('') + '</div>';
  }}

  // Deadlines from recent emails — AI-extracted, persisted to signals/deadlines.json
  const dlMeta = data.deadlines_meta || {{}};
  const ageMin = dlMeta.age_seconds != null ? Math.round(dlMeta.age_seconds / 60) : null;
  let metaLine = '';
  if (!dlMeta.extracted_at) {{
    metaLine = `<span style="color:#aaa">never extracted — <a href="#" onclick="refreshSignals(event)" style="color:#34A853;text-decoration:underline">run now →</a></span>`;
  }} else {{
    const ageStr = ageMin == null ? '?' : (ageMin < 60 ? `${{ageMin}}m ago` : `${{Math.round(ageMin/60)}}h ago`);
    metaLine = `<span style="color:#aaa">extracted ${{ageStr}}</span> · <a href="#" onclick="refreshSignals(event)" style="color:#34A853;text-decoration:underline">refresh →</a>`;
  }}

  let dHtml = '';
  if (deadlines.length === 0) {{
    dHtml = '<div class="tom-empty">No outstanding deadlines.</div>';
  }} else {{
    dHtml = '<div class="tom-list">' + deadlines.map(d => {{
      const urg = d.urgency || 'medium';
      const urgColor = urg === 'high' ? '#c06040' : urg === 'low' ? '#888' : '#4285F4';
      const dateStr = d.deadline || '—';
      const conf = d.confidence ? ` · ${{Math.round(d.confidence*100)}}%` : '';
      // "💬 chat" → fresh chat with the source email pinned in context.
      const srcRel = d.source_rel || '';
      const dlQuestion = `What is the action and context for: "${{d.subject}}"? Deadline ${{dateStr}}.`;
      const previewPath = escHtml(srcRel).replace(/'/g, "\\\\'");
      return `
        <div class="tom-item">
          <div class="tom-item-main" onclick="openFilePreview('${{previewPath}}')" style="cursor:pointer">
            <div style="display:flex;align-items:baseline;gap:8px;margin-bottom:3px">
              <span style="font-weight:700;font-size:12px;color:${{urgColor}}">${{escHtml(dateStr)}}</span>
              <span style="font-size:9.5px;font-weight:600;text-transform:uppercase;letter-spacing:0.5px;padding:1px 6px;border-radius:8px;background:rgba(0,0,0,.05);color:${{urgColor}}">${{escHtml(urg)}}</span>
            </div>
            <div style="font-weight:600;font-size:12px">${{escHtml(d.action)}}</div>
            <div class="tom-item-aux" style="margin-top:3px">${{escHtml(d.subject)}}${{conf}}</div>
          </div>
          <button class="tom-chat-btn"
                  data-chat-q="${{escHtml(dlQuestion)}}"
                  data-chat-paths="${{escHtml(srcRel)}}"
                  onclick="startChatFromTile(this)">💬 chat about this</button>
        </div>`;
    }}).join('') + '</div>';
  }}

  document.getElementById('tom-cols').innerHTML = `
    <div class="tom-col events">
      <div class="tom-col-stripe"></div>
      <div class="tom-col-num">Executive Calendar</div>
      <div class="tom-col-title">High-stakes events</div>
      <div class="tom-col-sub">Decisions, reviews, exec forums · next ${{data.days_ahead || 14}} days</div>
      <div class="tom-col-count">${{events.length}} surfaced</div>
      <div style="margin-top:14px">${{evHtml}}</div>
    </div>
    <div class="tom-col proposals">
      <div class="tom-col-stripe"></div>
      <div class="tom-col-num">Bookmarks</div>
      <div class="tom-col-title">Pinned</div>
      <div class="tom-col-sub">Saved chat answers · click to resume the conversation</div>
      <div class="tom-col-count">${{pinned.length}} saved</div>
      <div style="margin-top:14px">${{pnHtml}}</div>
    </div>
    <div class="tom-col questions">
      <div class="tom-col-stripe"></div>
      <div class="tom-col-num">Inbox signals</div>
      <div class="tom-col-title">Deadlines from emails</div>
      <div class="tom-col-sub">AI-extracted: relative dates resolved · already-replied + past filtered out</div>
      <div class="tom-col-count">${{deadlines.length}} active · <span style="font-weight:400">${{metaLine}}</span></div>
      <div style="margin-top:14px">${{dHtml}}</div>
    </div>`;
}}

function refreshSignals(ev) {{
  if (ev) ev.preventDefault();
  const tile = document.querySelector('.tom-col.questions .tom-col-count');
  if (tile) tile.innerHTML = tile.innerHTML.split(' · ')[0] + ' · <span style="color:#888">refreshing…</span>';
  fetch('/api/signals/refresh', {{
    method:  'POST',
    headers: {{'Content-Type': 'application/json'}},
    body:    JSON.stringify({{days_back: 14}}),
  }}).then(r => r.json()).then(j => {{
    if (j.status === 'started') {{
      // Poll Top of Mind a few times until the signals file's mtime updates.
      let tries = 0;
      const poll = () => {{
        if (++tries > 40) return;       // 40 × 3s = 2 min cap
        loadTopOfMind();
        const t = document.querySelector('.tom-col.questions .tom-col-count');
        if (t && t.innerHTML.includes('refreshing')) setTimeout(poll, 3000);
      }};
      setTimeout(poll, 4000);
    }}
  }}).catch(() => {{}});
}}

// ── Health tab ─────────────────────────────────────────────────────────────
function fmt(n) {{ return n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n||0); }}

function loadHealth() {{
  if (!document.getElementById('panel-health').classList.contains('active')) return;
  fetch('/api/stats').then(r=>r.json()).then(data => {{
    renderHealth(data);
  }}).catch(() => {{}});
}}

async function renderHealth(data) {{
  const c = data.compile || {{}}, d = data.dream || {{}}, r = data.retrieve || {{}};

  // Compile pillar
  const topics = c.wiki_topics || {{}};
  const maxP   = Math.max(...Object.values(topics), 1);
  const topicBars = Object.entries(topics).sort((a,b)=>b[1]-a[1]).map(([n,v]) => {{
    const pct = Math.round(v/maxP*100), col = TOPIC_COLORS[n]||'#ccc';
    return `<div class="topic-row"><div class="topic-name">${{n}}</div>
      <div class="topic-bar-wrap"><div class="topic-bar" style="width:${{pct}}%;background:${{col}}"></div></div>
      <div class="topic-count">${{v}}</div></div>`;
  }}).join('');

  const memRows = Object.entries(c.mem_folders||{{}}).slice(0,6).map(([k,v]) =>
    `<tr><td>${{k}}</td><td>${{v}}</td></tr>`).join('');

  // Recent activity — pulled live from /api/recent-activity (mtime-based)
  let recent = '';
  try {{
    const ra = await fetch('/api/recent-activity').then(r=>r.json());
    const fmtAgo = (ts) => {{
      const ago = (Date.now()/1000) - ts;
      if (ago < 60)    return 'just now';
      if (ago < 3600)  return Math.round(ago/60) + 'm ago';
      if (ago < 86400) return Math.round(ago/3600) + 'h ago';
      return Math.round(ago/86400) + 'd ago';
    }};
    recent = (ra.items||[]).slice(0,8).map(it => `
      <div style="padding:5px 0;border-bottom:1px solid #f5f5f5;font-size:11px;cursor:pointer"
           onclick="openFilePreview('${{it.rel.replace(/'/g, "\\\\'")}}')">
        <div style="display:flex;gap:6px;align-items:center">
          <span style="color:#444;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{it.name}}</span>
          <span style="color:#bbb;flex-shrink:0;font-size:10px">${{fmtAgo(it.modified)}}</span></div>
        <div style="color:#aaa;font-size:10px">${{it.folder}}</div></div>`).join('');
  }} catch(e) {{}}

  // Dream pillar
  const lr = d.last_run || {{}};
  const histBars = (d.sleep_history||[]).map(h => {{
    const pct = Math.round(Math.min(h.duration_min/30*100,100));
    return `<div class="run-bar-wrap"><div class="run-bar ${{h.phases<5?'partial':''}}"
      style="height:${{Math.max(pct,8)}}%" title="${{h.date}} · ${{h.duration_min}}m · ${{h.phases}}/5"></div></div>`;
  }}).join('');

  const PICONS = {{decay:'⏳',episodic_harvest:'🧲',graph_enrichment:'🔗',compression:'🗜️',contradiction_resolution:'⚖️'}};
  const phaseRows = (d.phase_details||[]).map(ph => {{
    const icon = PICONS[ph.phase]||'·', lbl = ph.phase.replace(/_/g,' ');
    const val = ph.edges_updated??ph.files_scanned??ph.new_edges??ph.files_compressed??ph.resolved??'';
    const met = ph.edges_updated!==undefined?'edges':ph.files_scanned!==undefined?'files':
                ph.new_edges!==undefined?'edges':ph.files_compressed!==undefined?'compressed':
                ph.resolved!==undefined?'resolved':'';
    return `<div class="phase-row"><div class="phase-name">${{icon}} ${{lbl}}</div>
      <div class="phase-detail">${{val!==''&&met?val+' '+met:''}}${{ph.communities?' · '+ph.communities+' communities':''}}</div></div>`;
  }}).join('');

  const tiers = d.tier_distribution || {{}};
  const tierTotal = Math.max(Object.values(tiers).reduce((a,b)=>a+b,0),1);
  const TO = ['working','episodic','semantic','crystallised'];
  const tierBars   = TO.map(t=>`<div class="tier-bar tier-${{t}}" style="flex:${{Math.round((tiers[t]||0)/tierTotal*100)}}"></div>`).join('');
  const tierLegend = TO.map(t=>`<div class="tier-item"><div class="tier-dot tier-${{t}}"></div>${{t}} ${{tiers[t]||0}}</div>`).join('');

  const health = d.health || {{}};
  const covPct = Math.round((health.coverage_score||0)*100);
  // Watcher chip — fetch async
  let watcherChip = '';
  try {{
    const wr = await fetch('/api/watcher-status');
    const ws = await wr.json();
    if (ws.inbox) {{
      const inbox = ws.inbox.split('/').pop();
      const newCount = ws.files_new || 0;
      const skipped  = ws.skipped || 0;
      const tip = `Monitoring ${{ws.inbox}}\\nIngested: ${{newCount}}\\nDropped (marketing): ${{skipped}}`;
      const label = ws.running
        ? `&#128065; ${{inbox}} · ${{newCount}} in${{skipped ? ' · ' + skipped + ' marketing dropped' : ''}}`
        : `&#128065; Watcher idle`;
      watcherChip = `<div class="alert-chip ${{ws.running?'ok':'warn'}}" title="${{tip}}">${{label}}</div>`;
    }}
  }} catch(e) {{}}

  const alerts = [
    d.contradictions_pending>0?`<div class="alert-chip warn" onclick="openHealthDetail('contradictions')">&#9888; ${{d.contradictions_pending}} contradictions</div>`:'',
    d.proposals_pending>0?`<div class="alert-chip info" onclick="openHealthDetail('proposals')">&#128221; ${{d.proposals_pending}} proposals</div>`:'',
    d.open_questions>0?`<div class="alert-chip info" onclick="openHealthDetail('open_questions')">&#10067; ${{d.open_questions}} open questions</div>`:'',
    watcherChip,
    !d.contradictions_pending&&!d.open_questions&&!d.proposals_pending?`<div class="alert-chip ok">&#10003; Clean</div>`:'',
  ].filter(Boolean).join('');

  // Retrieve pillar
  const gs = r.graph_stats||{{}};
  const typeRows = (gs.top_types||[]).map(([t,n])=>`<tr><td>${{t}}</td><td>${{n}}</td></tr>`).join('');
  const commList = (r.communities||[]).map(c=>`<div class="community-chip"><span class="chip-label">${{c.label}}</span><span class="chip-size">${{c.size}} nodes</span></div>`).join('');
  const modes = [
    {{col:'#D97757',lbl:'Keyword scan (30+)', desc:'BM25-style · proper-noun boost · stemming · synonym expansion · fuzzy PN matching'}},
    {{col:'#4285F4',lbl:'Graph spread',       desc:'Spreading activation · multi-hop entity graph · surfaces related files'}},
    {{col:'#34A853',lbl:'Wiki scan (QMD)',     desc:'QMD BM25 on 2000+ compiled wiki pages · falls back to _index.md token scan'}},
    {{col:'#a855f7',lbl:'Haiku curator',      desc:'LLM selects ≤10 files from ~40 candidates · reasoning shown in sidebar'}},
    {{col:'#0891b2',lbl:'Haiku monitor',      desc:'Post-response context update · adds/removes files for next turn'}},
  ].map(m=>`<div class="mode-row"><div class="mode-accent" style="background:${{m.col}}"></div>
    <div><div class="mode-label">${{m.lbl}}</div><div class="mode-desc">${{m.desc}}</div></div></div>`).join('');

  document.getElementById('pillars').innerHTML = `
  <div class="pillar pillar-1">
    <div class="pillar-stripe"></div>
    <div class="pillar-num">Pillar 01</div>
    <div class="pillar-title-text">Compile</div>
    <div class="pillar-sub">Raw sources → wiki pages + memory files. One pass per document.</div>
    <div class="section"><div class="section-label">Volume</div>
      <div class="big-stats">
        <div class="big-stat"><div class="val">${{fmt(c.wiki_total)}}</div><div class="lbl">Wiki pages</div></div>
        <div class="big-stat"><div class="val">${{fmt(c.mem_total)}}</div><div class="lbl">Memory files</div></div>
        <div class="big-stat"><div class="val">${{Object.keys(topics).length}}</div><div class="lbl">Topics</div></div>
      </div></div>
    <div class="section"><div class="section-label">Wiki topics</div>
      <div class="topic-bars">${{topicBars||'<div class="loading">No topics</div>'}}</div></div>
    <div class="section"><div class="section-label">Memory folders</div>
      <table class="mem-table"><tbody>${{memRows||'<tr><td>—</td></tr>'}}</tbody></table></div>
    <div class="section"><div class="section-label">Recent activity</div>${{recent||'<div class="loading">No data</div>'}}</div>
  </div>

  <div class="pillar pillar-2">
    <div class="pillar-stripe"></div>
    <div class="pillar-num">Pillar 02</div>
    <div class="pillar-title-text">Dream</div>
    <div class="pillar-sub">Nightly reconsolidation — dedup, contradictions, tier promotion.</div>
    <div class="section"><div class="section-label">Last run</div>
      <div class="big-stats">
        <div class="big-stat"><div class="val" style="font-size:16px">${{lr.date?lr.date.slice(5,16):'—'}}</div><div class="lbl">Last dream</div></div>
        <div class="big-stat"><div class="val">${{lr.duration_min||'—'}}<span style="font-size:12px;color:#aaa">m</span></div><div class="lbl">Duration</div></div>
        <div class="big-stat"><div class="val">${{lr.phases_completed||'—'}}<span style="font-size:12px;color:#aaa">/5</span></div><div class="lbl">Phases</div></div>
      </div></div>
    <div class="section"><div class="section-label">Run history</div>
      <div class="run-history">${{histBars||'<div class="loading">No runs yet</div>'}}</div></div>
    <div class="section"><div class="section-label">Last phases</div>
      <div class="phases">${{phaseRows||'<div class="loading">No data</div>'}}</div></div>
    <div class="section"><div class="section-label">Memory tiers</div>
      <div class="tier-bars">${{tierBars}}</div>
      <div class="tier-legend">${{tierLegend}}</div></div>
    <div class="section"><div class="section-label">Resolve</div>
      <div style="display:flex;flex-direction:column;gap:6px">
        <button onclick="openHealthDetail('contradictions')"
                style="display:flex;align-items:center;justify-content:space-between;background:${{d.contradictions_pending>0?'#fff3e0':'#f5f5f5'}};border:1px solid ${{d.contradictions_pending>0?'#fbbc04':'#e8e8e8'}};border-radius:8px;padding:9px 12px;cursor:pointer;font-size:12px;color:#1a1a1a;text-align:left;width:100%">
          <span>&#9888; Contradictions</span>
          <span style="font-weight:700;color:${{d.contradictions_pending>0?'#a07800':'#aaa'}}">${{d.contradictions_pending||0}} pending →</span>
        </button>
        <button onclick="openHealthDetail('open_questions')"
                style="display:flex;align-items:center;justify-content:space-between;background:${{d.open_questions>0?'#ebf5ff':'#f5f5f5'}};border:1px solid ${{d.open_questions>0?'#4285F4':'#e8e8e8'}};border-radius:8px;padding:9px 12px;cursor:pointer;font-size:12px;color:#1a1a1a;text-align:left;width:100%">
          <span>&#10067; Open questions</span>
          <span style="font-weight:700;color:${{d.open_questions>0?'#4285F4':'#aaa'}}">${{d.open_questions||0}} pending →</span>
        </button>
        <button onclick="openHealthDetail('proposals')"
                style="display:flex;align-items:center;justify-content:space-between;background:${{d.proposals_pending>0?'#fff5ed':'#f5f5f5'}};border:1px solid ${{d.proposals_pending>0?'#D97757':'#e8e8e8'}};border-radius:8px;padding:9px 12px;cursor:pointer;font-size:12px;color:#1a1a1a;text-align:left;width:100%"
                title="Memory-write proposals from chat-session harvesting + consolidation. Save / skip via the modal.">
          <span>&#128221; Pending proposals</span>
          <span style="font-weight:700;color:${{d.proposals_pending>0?'#c06040':'#aaa'}}">${{d.proposals_pending||0}} pending →</span>
        </button>
        <button onclick="cleanupResolved(this)" id="cleanup-btn"
                style="display:flex;align-items:center;justify-content:space-between;background:#f5f5f5;border:1px solid #e8e8e8;border-radius:8px;padding:9px 12px;cursor:pointer;font-size:12px;color:#1a1a1a;text-align:left;width:100%"
                title="Persist all resolved contradictions to the rejection registry and purge bad edges from the graph">
          <span>&#129529; Apply resolutions to graph</span>
          <span style="font-weight:600;color:#888;font-size:11px">Run cleanup</span>
        </button>
      </div></div>
    <div class="section"><div class="section-label">Health</div>
      <div class="meter"><div class="meter-label">Coverage</div>
        <div class="meter-track"><div class="meter-fill" style="width:${{covPct}}%"></div></div>
        <div class="meter-val">${{covPct}}</div></div>
      <div style="font-size:10px;color:#bbb;margin-bottom:6px">Confidence ${{health.avg_confidence||'—'}} · Edge weight ${{health.avg_edge_weight||'—'}}</div>
      <div class="alert-row">${{alerts}}</div></div>
  </div>

  <div class="pillar pillar-3">
    <div class="pillar-stripe"></div>
    <div class="pillar-num">Pillar 03</div>
    <div class="pillar-title-text">Retrieve</div>
    <div class="pillar-sub">Zero-latency context assembly at query time.</div>
    <div class="section"><div class="section-label">Knowledge graph</div>
      <div class="big-stats">
        <div class="big-stat"><div class="val">${{fmt(gs.entities)}}</div><div class="lbl">Entities</div></div>
        <div class="big-stat"><div class="val">${{fmt(gs.edges)}}</div><div class="lbl">Edges</div></div>
        <div class="big-stat"><div class="val">${{r.crystallised_count||0}}</div><div class="lbl">Crystallised</div></div>
      </div></div>
    <div class="section"><div class="section-label">Retrieval pipeline</div>
      <div>${{modes}}</div></div>
    <div class="section"><div class="section-label">Top entity types</div>
      <table class="type-table"><tbody>${{typeRows||'<tr><td>No graph data</td></tr>'}}</tbody></table></div>
    <div class="section"><div class="section-label">Communities</div>
      <div class="community-list">${{commList||'<div class="loading">No communities</div>'}}</div></div>
  </div>`;
}}

let _hdWhat = null;

async function openHealthDetail(what) {{
  _hdWhat = what;
  const overlay  = document.getElementById('health-detail-overlay');
  const titleEl  = document.getElementById('health-detail-title');
  const bodyEl   = document.getElementById('health-detail-body');

  const titles = {{
    contradictions: 'Contradictions',
    open_questions: 'Open Questions',
    proposals:      'Pending memory proposals',
  }};
  titleEl.textContent = titles[what] || 'Detail';
  bodyEl.innerHTML    = '<div class="fp-loading">Loading…</div>';
  overlay.classList.add('open');

  await _renderHealthDetail(what, bodyEl);
}}

async function _renderHealthDetail(what, bodyEl) {{
  try {{
    const res  = await fetch('/api/health-detail?what=' + what);
    const data = await res.json();

    if (data.error) {{ bodyEl.innerHTML = '<p style="color:#e00">&#9888; ' + data.error + '</p>'; return; }}

    const items = data.items || [];
    if (!items.length) {{
      bodyEl.innerHTML = '<p style="color:#aaa;font-style:italic;padding:8px 0">None pending.</p>';
      return;
    }}

    const total = data.total || items.length;
    let html = `<p style="font-size:11px;color:#aaa;margin-bottom:16px">${{total}} pending</p>`;

    if (what === 'contradictions') {{
      const sevColor = {{ high: '#e53e3e', medium: '#dd6b20', low: '#718096' }};
      html += items.map(it => {{
        const sa = it.claim_A || {{}};
        const sb = it.claim_B || {{}};
        const sev = it.severity || 'medium';
        const sc  = sevColor[sev] || '#718096';
        return `
        <div class="hd-card" data-id="${{it.id}}" data-what="contradictions">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:10px">
            <span style="font-size:10px;font-weight:700;color:${{sc}};background:${{sc}}18;padding:2px 8px;border-radius:10px;text-transform:uppercase">${{sev}}</span>
            <span style="font-size:10px;color:#bbb">${{it.type || 'factual conflict'}}</span>
          </div>
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:12px">
            <div class="hd-claim claim-a">
              <div class="hd-claim-label">Claim A</div>
              <div class="hd-claim-text">${{sa.statement || '—'}}</div>
              ${{sa.source ? `<div class="hd-claim-meta">Source: ${{sa.source.split('/').pop()}}</div>` : ''}}
              ${{sa.weight ? `<div class="hd-claim-meta">Confidence: ${{Math.round(sa.weight*100)}}%</div>` : ''}}
            </div>
            <div class="hd-claim claim-b">
              <div class="hd-claim-label">Claim B</div>
              <div class="hd-claim-text">${{sb.statement || '—'}}</div>
              ${{sb.source ? `<div class="hd-claim-meta">Source: ${{sb.source.split('/').pop()}}</div>` : ''}}
              ${{sb.weight ? `<div class="hd-claim-meta">Confidence: ${{Math.round(sb.weight*100)}}%</div>` : ''}}
            </div>
          </div>
          <div class="hd-actions">
            <button class="hd-btn hd-btn-a" onclick="resolveItem('contradictions','${{it.id}}','resolved_A',this)">✓ A is correct</button>
            <button class="hd-btn hd-btn-b" onclick="resolveItem('contradictions','${{it.id}}','resolved_B',this)">✓ B is correct</button>
            <button class="hd-btn hd-btn-both-true" onclick="resolveItem('contradictions','${{it.id}}','both_true',this)" title="Both claims are valid (e.g. dual reporting, historical change)">✓✓ Both true</button>
            <button class="hd-btn hd-btn-both-false" onclick="resolveItem('contradictions','${{it.id}}','both_false',this)" title="Neither claim is correct (the truth is something else)">✕✕ Both false</button>
            <button class="hd-btn hd-btn-dismiss" onclick="resolveItem('contradictions','${{it.id}}','dismissed',this)">— Dismiss</button>
          </div>
        </div>`;
      }}).join('');
    }} else if (what === 'open_questions') {{
      const priColor = {{ high: '#e53e3e', medium: '#dd6b20', low: '#718096' }};
      html += items.map((q, i) => {{
        const pri = q.priority || 'medium';
        const pc  = priColor[pri] || '#718096';
        return `
        <div class="hd-card" data-id="${{q.id}}" data-what="open_questions">
          <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:10px">
            <span style="font-size:10px;font-weight:700;color:${{pc}};background:${{pc}}18;padding:2px 8px;border-radius:10px;text-transform:uppercase;white-space:nowrap">${{pri}}</span>
            <div style="flex:1;font-size:13px;color:#1a1a1a;line-height:1.5">${{q.text || '—'}}</div>
          </div>
          ${{q.source ? `<div style="font-size:10px;color:#aaa;margin-bottom:10px">Source: ${{q.source.split('/').pop()}}</div>` : ''}}
          <div class="hd-actions">
            <button class="hd-btn hd-btn-resolve" onclick="resolveItem('open_questions','${{q.id}}','answered',this)">✓ Mark resolved</button>
            <button class="hd-btn hd-btn-dismiss" onclick="resolveItem('open_questions','${{q.id}}','dismissed',this)">✕ Dismiss</button>
          </div>
        </div>`;
      }}).join('');
    }} else if (what === 'proposals') {{
      // Pending memory writes from chat-session harvest + consolidation. Each
      // card lets the user save (apply to the canonical path), skip, or open
      // the source session for context.
      html += items.map(p => {{
        const op = p.operation || 'update';
        const opColor = op === 'create' ? '#34A853' : op === 'delete' ? '#c06040' : '#4285F4';
        const sal = (p.salience||0).toFixed(2);
        const safePath = (p.path || '').replace(/'/g, "\\\\'");
        const harvest  = (p.harvest_filename || '').replace(/'/g, "\\\\'");
        return `
        <div class="hd-card" data-uid="${{p.uid}}" data-what="proposals">
          <div style="display:flex;align-items:flex-start;gap:10px;margin-bottom:10px;flex-wrap:wrap">
            <span style="font-size:10px;font-weight:700;color:${{opColor}};background:${{opColor}}18;padding:2px 8px;border-radius:10px;text-transform:uppercase;white-space:nowrap">${{op}}</span>
            <span style="font-size:11px;color:#666;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;flex:1;word-break:break-all;cursor:pointer" onclick="openFilePreview('${{safePath}}')">${{p.path || '—'}}</span>
            <span style="font-size:10px;color:#aaa">salience ${{sal}}</span>
          </div>
          <div style="font-size:13px;color:#1a1a1a;line-height:1.5;margin-bottom:8px">${{p.reason || '—'}}</div>
          <div style="font-size:10px;color:#aaa;margin-bottom:10px">
            ${{p.source || ''}}${{p.ts ? ' · ' + p.ts : ''}}
            ${{harvest ? ` · <a href="#" onclick="event.preventDefault();openFilePreview('${{harvest}}')" style="color:#4285F4;text-decoration:underline">source session</a>` : ''}}
          </div>
          <div class="hd-actions">
            <button class="hd-btn hd-btn-a" onclick="resolveItem('proposals','${{p.uid}}','saved',this)">✓ Save</button>
            <button class="hd-btn hd-btn-dismiss" onclick="resolveItem('proposals','${{p.uid}}','skipped',this)">✕ Skip</button>
          </div>
        </div>`;
      }}).join('');
    }}

    bodyEl.innerHTML = html;
  }} catch (err) {{
    bodyEl.innerHTML = '<p style="color:#e00">&#9888; ' + err.message + '</p>';
  }}
}}

async function cleanupResolved(btn) {{
  const original = btn.innerHTML;
  btn.disabled = true;
  btn.innerHTML = '<span>&#129529; Cleaning up…</span>';
  try {{
    const res  = await fetch('/api/cleanup-resolved', {{ method: 'POST' }});
    const data = await res.json();
    if (data.ok) {{
      _showToast(`✓ Registered ${{data.backfilled_registry}} resolutions · purged ${{data.graph_edges_purged}} edges · ${{data.registry_total_truths}} ground truths active`);
      btn.innerHTML = `<span>&#10003; Cleaned up</span><span style="font-weight:600;color:#15803d;font-size:11px">${{data.graph_edges_purged}} edges purged</span>`;
      setTimeout(() => {{ btn.innerHTML = original; btn.disabled = false; }}, 3500);
    }} else {{
      btn.innerHTML = '<span>&#9888; Error</span>';
      btn.disabled = false;
    }}
  }} catch(e) {{
    btn.innerHTML = '<span>&#9888; Error</span>';
    btn.disabled = false;
  }}
}}

function _showToast(msg) {{
  let t = document.getElementById('hd-toast');
  if (!t) {{
    t = document.createElement('div');
    t.id = 'hd-toast';
    t.className = 'hd-toast';
    document.body.appendChild(t);
  }}
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._tm);
  t._tm = setTimeout(() => t.classList.remove('show'), 3500);
}}

async function resolveItem(what, id, resolution, btn) {{
  const endpoint =
        what === 'contradictions' ? '/api/resolve-contradiction'
      : what === 'proposals'      ? '/api/resolve-proposal'
      : '/api/resolve-question';
  btn.disabled = true;
  btn.textContent = '…';
  try {{
    const res  = await fetch(endpoint, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify({{ id, uid: id, resolution, status: resolution }})
    }});
    const data = await res.json();
    if (data.ok) {{
      const cascaded = data.cascaded || 0;
      const card = btn.closest('.hd-card');

      if (cascaded > 0) {{
        // Cascade fired — re-render the modal so newly-resolved cards disappear too
        _showToast(`✓ Resolved + ${{cascaded}} cascaded auto-resolution${{cascaded===1?'':'s'}}`);
        const bodyEl = document.getElementById('health-detail-body');
        bodyEl.innerHTML = '<div class="fp-loading">Refreshing…</div>';
        await _renderHealthDetail(what, bodyEl);
      }} else {{
        // Just fade this card out
        if (card) {{
          card.style.opacity = '0';
          card.style.transition = 'opacity 0.3s';
          setTimeout(() => {{
            card.remove();
            const remaining = document.querySelectorAll('.hd-card').length;
            const p = document.querySelector('#health-detail-body > p');
            if (p) p.textContent = remaining + ' pending';
            if (!remaining) {{
              document.querySelector('#health-detail-body').innerHTML =
                '<p style="color:#aaa;font-style:italic;padding:8px 0">All resolved.</p>';
            }}
          }}, 300);
        }}
      }}
    }} else {{
      btn.disabled = false;
      btn.textContent = '⚠ retry';
    }}
  }} catch(e) {{
    btn.disabled = false;
    btn.textContent = '⚠ retry';
  }}
}}

function closeHealthDetail(e) {{
  if (!e || e.target === document.getElementById('health-detail-overlay')) {{
    document.getElementById('health-detail-overlay').classList.remove('open');
  }}
}}

// ── SPA tab switching ─────────────────────────────────────────────────────
// Anchors keep their hrefs (so right-click → open in new tab still works)
// but normal clicks toggle panels in place — chat state, in-flight stream,
// rawDocs, pinned context all survive navigation between tabs.
const TAB_PATHS = {{ chat: '/', 'top-of-mind': '/top-of-mind', health: '/health' }};

function switchTab(ev, name) {{
  if (ev) {{
    if (ev.metaKey || ev.ctrlKey || ev.shiftKey || ev.button === 1) return true;
    ev.preventDefault();
  }}
  _applyTab(name);
  const path = TAB_PATHS[name] || '/';
  if (location.pathname !== path) history.pushState({{tab: name}}, '', path);
  return false;
}}

function _applyTab(name) {{
  document.querySelectorAll('.tab').forEach(el => el.classList.toggle('active', el.dataset.tab === name));
  document.querySelectorAll('.tab-panel').forEach(el => {{
    el.classList.toggle('active', el.id === 'panel-' + name);
  }});
  // Trigger per-tab data load (each handler self-skips when not active).
  if (name === 'health')      loadHealth();
  if (name === 'top-of-mind') loadTopOfMind();
}}

window.addEventListener('popstate', (e) => {{
  const name = (e.state && e.state.tab) || _tabFromPath(location.pathname);
  _applyTab(name);
}});

function _tabFromPath(p) {{
  if (p === '/health')       return 'health';
  if (p === '/top-of-mind')  return 'top-of-mind';
  return 'chat';
}}

// Always-on refresh intervals — the load functions self-skip when their
// panel isn't active, so this is cheap and means newly-switched-to tabs
// stay fresh without any extra wiring.
setInterval(loadHealth,     30000);
setInterval(loadTopOfMind,  60000);
// Initial load for whichever tab is active at page-load time.
loadHealth();
loadTopOfMind();

// Drag-and-drop on the upload zone
document.addEventListener('DOMContentLoaded', () => {{
  const zone = document.getElementById('raw-doc-dropzone');
  if (!zone) return;
  zone.addEventListener('dragover', e => {{ e.preventDefault(); zone.style.borderColor = '#D97757'; }});
  zone.addEventListener('dragleave', () => {{ zone.style.borderColor = ''; }});
  zone.addEventListener('drop', e => {{
    e.preventDefault();
    zone.style.borderColor = '';
    const file = e.dataTransfer.files[0];
    if (!file) return;
    const input = document.getElementById('raw-doc-file');
    const dt = new DataTransfer();
    dt.items.add(file);
    input.files = dt.files;
    handleFileUpload(input);
  }});
}});
</script>
</body>
</html>"""


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("ENGRAM_PORT", 7090))
    cfg  = get_cfg()
    print(f"  engram running at http://localhost:{port}", flush=True)
    print(f"  Chat → http://localhost:{port}/", flush=True)
    print(f"  Top of Mind → http://localhost:{port}/top-of-mind", flush=True)
    print(f"  Health → http://localhost:{port}/health", flush=True)
    print(f"  Memory: {cfg.memory_path}", flush=True)
    print(f"  Wiki:   {cfg.wiki_path}", flush=True)

    # Auto-start inbox watcher if inbox is configured
    try:
        _start_watcher(cfg)
    except Exception as e:
        print(f"[watcher] failed to start: {e}", flush=True)

    # Auto-start the nightly dream-cycle scheduler
    try:
        _start_sleep_scheduler(cfg)
    except Exception as e:
        print(f"[sleep] scheduler failed to start: {e}", flush=True)

    app.run(host="0.0.0.0", port=port, debug=False)
