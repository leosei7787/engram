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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from flask import Flask, jsonify, request, Response, stream_with_context
from engram.retrieval.config import load_config, EngramConfig
from engram.retrieval.pipeline import memory_scan
from engram.retrieval.curator import build_candidates, curate_context, monitor_context, detect_drift

app = Flask(__name__)
_cfg: EngramConfig | None = None


def get_cfg() -> EngramConfig:
    global _cfg
    if _cfg is None:
        _cfg = load_config()
    return _cfg


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


def _stream_cli(messages: list, system_prompt: str, cli_bin: str, model: str | None = None):
    """
    Call Claude CLI in print mode and yield response text in small chunks.

    Uses `claude -p <msg> --output-format stream-json --verbose [--system-prompt ...] [--model ...]`.

    The CLI in -p mode returns batched NDJSON events (not incremental token deltas).
    We parse the `assistant` message event and emit the text in ~10-word chunks so
    the typing-indicator UX stays responsive. Falls back to `result` event if needed.

    Yields plain text strings (no SSE framing — caller wraps in data: ... \\n\\n).
    """
    user_msg = _format_messages_for_cli(messages)

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

        full_text   = ""
        result_text = ""

        for line in proc.stdout:
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

    def generate():
        cfg = get_cfg()

        # ── Phase 0: Announce scanning ────────────────────────────────────────
        yield f"data: {json.dumps({'context': {'phase': 'scanning', 'candidates_total': 0, 'selected': [], 'reasoning': ''}})}\n\n"

        # ── Phase 1: Wide memory scan (up to 30 direct + graph + wiki) ────────
        try:
            scan = memory_scan(query, cfg, max_files=30)
        except Exception as e:
            scan = {"direct": [], "graph": [], "wiki": [], "graph_context": "", "suggestions": []}
            print(f"[chat] memory_scan error: {e}", flush=True)

        # ── Phase 2: Build candidate list with snippets ───────────────────────
        try:
            all_candidates = build_candidates(scan, cfg, snippet_chars=250)
        except Exception as e:
            all_candidates = []
            print(f"[chat] build_candidates error: {e}", flush=True)

        n_candidates = len(all_candidates)

        # ── Phase 3: Drift detection — skip Haiku if topic hasn't changed ──────
        has_drift = detect_drift(query, messages)

        if has_drift:
            # Announce curating (sidebar shows candidate count immediately)
            yield f"data: {json.dumps({'context': {'phase': 'curating', 'candidates_total': n_candidates, 'selected': [], 'reasoning': ''}})}\n\n"

            # Haiku curator selects which files enter context
            try:
                selected, reasoning = curate_context(query, all_candidates, cfg, max_files=10)
            except Exception as e:
                selected  = all_candidates[:10]
                reasoning = "Top-N by scan score (fallback)"
                print(f"[chat] curator error: {e}", flush=True)
        else:
            # No drift — skip Haiku, use top-N by scan order (fast path)
            selected  = all_candidates[:10]
            reasoning = "Same topic — context reused"

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

        base   = cfg.base_path
        budget = cfg.retrieval.context_budget.max_total_chars
        trunc  = cfg.retrieval.context_budget.wiki_page_truncate_chars
        used   = 0

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
                    cap   = min(8000, budget - used)
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
        system_parts.append(
            "\n\n---\n"
            "# Context management\n"
            "If you cannot answer because you need more specific context not in the files above, "
            "output exactly this on its own line (nothing else on that line):\n"
            "REQUEST_CONTEXT: <focused one-sentence query describing what you need>\n"
            "The system will automatically fetch and inject the missing context and re-run your response."
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
                return
            cli_model = getattr(chat_cfg, "cli_model", None) or None
            try:
                for text in _stream_cli(messages, system_prompt, cli_bin, model=cli_model):
                    assistant_text += text
                    yield f"data: {json.dumps({'token': text})}\n\n"
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
                        assistant_text += text
                        yield f"data: {json.dumps({'token': text})}\n\n"
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
                        for text in _stream_cli(messages, system_prompt, cli_bin, model=cli_model):
                            assistant_text += text
                            yield f"data: {json.dumps({'token': text})}\n\n"
                    else:
                        with client.messages.stream(
                            model=cfg.models.primary, max_tokens=4096,
                            system=system_prompt, messages=messages,
                        ) as stream:
                            for text in stream.text_stream:
                                assistant_text += text
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

        yield "data: [DONE]\n\n"

    return Response(stream_with_context(generate()),
                    mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ─── Stats API ────────────────────────────────────────────────────────────────

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
            open_q = len([q for q in qs if q.get("status") not in ("resolved", "dismissed")])
        ct_f = memory_path / "contradictions.json"
        if ct_f.exists():
            ctd  = json.loads(ct_f.read_text())
            cs   = ctd if isinstance(ctd, list) else ctd.get("contradictions", [])
            contradictions_pending = len([c for c in cs if c.get("status") not in ("resolved", "dismissed")])
    except Exception as e:
        print(f"[stats] health: {e}", flush=True)

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
    """Return raw text content of a memory or wiki file for preview."""
    cfg  = get_cfg()
    path = request.args.get("path", "").strip()
    if not path or path.startswith("__raw__"):
        return jsonify({"error": "no path"}), 400

    p = Path(path) if Path(path).is_absolute() else cfg.memory_path / path
    if not p.exists():
        p2 = cfg.wiki_path / path
        if p2.exists():
            p = p2

    if not p.exists() or not p.is_file():
        return jsonify({"error": "not found"}), 404

    # Safety: must be inside memory_path or wiki_path
    try:
        p.resolve().relative_to(cfg.memory_path.resolve())
    except ValueError:
        try:
            p.resolve().relative_to(cfg.wiki_path.resolve())
        except ValueError:
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


# ─── Main page ────────────────────────────────────────────────────────────────

@app.route("/")
@app.route("/health")
def index():
    cfg          = get_cfg()
    active_tab   = "health" if request.path == "/health" else "chat"
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
.ctx-add-bar {{ padding: 8px 12px; border-top: 1px solid #efefef; }}
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
.rc-pill {{ display: inline-flex; align-items: center; gap: 5px; padding: 3px 10px;
             background: rgba(66,133,244,.08); border: 1px solid rgba(66,133,244,.18);
             border-radius: 20px; font-size: 11px; color: #4285F4;
             margin-bottom: 6px; max-width: 100%; overflow: hidden;
             text-overflow: ellipsis; white-space: nowrap; }}

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

/* Chat main */
.chat-main {{ flex: 1; display: flex; flex-direction: column; overflow: hidden; }}
.messages {{ flex: 1; overflow-y: auto; padding: 24px 0; }}
.message {{ width: 100%; margin: 0 0 20px; padding: 0 28px; }}
.message.user   {{ display: flex; justify-content: flex-end; }}
.message.assistant {{ display: flex; justify-content: flex-start; }}
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
.alert-chip {{ padding: 3px 10px; border-radius: 20px; font-size: 10px; font-weight: 600; }}
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
    <a class="tab {'active' if active_tab == 'chat' else ''}" href="/" id="tab-chat">Chat</a>
    <a class="tab {'active' if active_tab == 'health' else ''}" href="/health" id="tab-health">Engram Health</a>
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
      </div>
      <div class="ctx-legend">
        <div class="ctx-legend-item"><div class="ctx-dot memory"></div>memory</div>
        <div class="ctx-legend-item"><div class="ctx-dot graph"></div>graph</div>
        <div class="ctx-legend-item"><div class="ctx-dot wiki"></div>wiki</div>
        <div class="ctx-legend-item"><div class="ctx-dot" style="background:#a855f7"></div>raw</div>
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
    <div class="ctx-modal-sub">Paste content or a file path. Injected immediately as a raw-tier item; compiled into memory in the background.</div>
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
function sendMessage() {{
  if (streaming) return;
  const text = input.value.trim();
  if (!text) return;

  document.getElementById('empty-state')?.remove();
  messages.push({{role:'user', content: text}});
  appendBubble('user', text);
  input.value = ''; input.style.height = 'auto';

  const assistantEl = appendBubble('assistant', '');
  assistantEl.querySelector('.bubble').classList.add('typing');
  document.getElementById('send-btn').disabled = true;
  streaming = true;

  // Reset context sidebar
  activeContext = [];
  document.getElementById('ctx-list').innerHTML = '<div class="ctx-empty">Scanning memory…</div>';
  document.getElementById('ctx-count').textContent = 'Scanning memory…';
  document.getElementById('ctx-reasoning').textContent = '';

  const evtSrc = new EventSource('/api/chat?' + new URLSearchParams({{
    _body: JSON.stringify({{messages}})
  }}));

  // Use fetch + ReadableStream instead (EventSource doesn't support POST)
  streaming = true;
  fetch('/api/chat', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json'}},
    body: JSON.stringify({{messages, raw_docs: rawDocs, pinned: [...pinnedPaths]}})
  }}).then(res => {{
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = '', assistantText = '';

    function pump() {{
      reader.read().then(({{done, value}}) => {{
        if (done) {{
          assistantEl.querySelector('.bubble').classList.remove('typing');
          messages.push({{role:'assistant', content: assistantText}});
          document.getElementById('send-btn').disabled = false;
          streaming = false;
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
            if (j.clear_response) {{
              // Model is about to retry with more context — clear the bubble
              assistantText = '';
              const bubble = assistantEl.querySelector('.bubble');
              bubble.textContent = '';
              bubble.classList.add('typing');
            }}
            if (j.request_context) {{
              // Show a small search pill above the bubble
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
              // Keep typing cursor without resetting animation
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
    document.getElementById('send-btn').disabled = false;
    streaming = false;
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

// ── Raw doc modal ──────────────────────────────────────────────────────────
function openRawDocModal() {{
  document.getElementById('raw-doc-modal').style.display = 'flex';
  document.getElementById('raw-doc-content').focus();
}}

function closeRawDocModal(e) {{
  if (!e || e.target === document.getElementById('raw-doc-modal')) {{
    document.getElementById('raw-doc-modal').style.display = 'none';
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

  // phase === 'ready'
  activeContext = sel;
  const n = sel.length;
  countEl.textContent  = n + ' of ' + total + ' candidate' + (total !== 1 ? 's' : '');
  reasonEl.textContent = reason;

  list.innerHTML = '';
  for (const f of sel) {{
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
    if (pinnedPaths.has(f.path)) continue;   // never remove pinned
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

// ── Health tab ─────────────────────────────────────────────────────────────
function fmt(n) {{ return n >= 1000 ? (n/1000).toFixed(1)+'k' : String(n||0); }}

function loadHealth() {{
  if (!document.getElementById('panel-health').classList.contains('active')) return;
  fetch('/api/stats').then(r=>r.json()).then(data => {{
    renderHealth(data);
  }}).catch(() => {{}});
}}

function renderHealth(data) {{
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

  const recent = (c.wiki_recent||[]).slice(0,5).map(r => {{
    const b = [r.pages_created?`<span class="act-badge new">+${{r.pages_created}}</span>`:'',
               r.pages_updated?`<span class="act-badge upd">~${{r.pages_updated}}</span>`:''].filter(Boolean).join(' ');
    return `<div style="padding:5px 0;border-bottom:1px solid #f5f5f5;font-size:11px">
      <div style="display:flex;gap:6px;align-items:center">
        <span style="color:#444;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${{r.file}} ${{b}}</span>
        <span style="color:#bbb;flex-shrink:0">${{r.date}}</span></div>
      <div style="color:#aaa;font-size:10px">${{r.summary||''}}</div></div>`;
  }}).join('');

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
  const alerts = [
    d.contradictions_pending>0?`<div class="alert-chip warn">⚠ ${{d.contradictions_pending}} contradictions</div>`:'',
    d.open_questions>0?`<div class="alert-chip info">❓ ${{d.open_questions}} open questions</div>`:'',
    !d.contradictions_pending&&!d.open_questions?`<div class="alert-chip ok">✓ Clean</div>`:'',
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

// Load health on page if on /health
if (document.getElementById('panel-health').classList.contains('active')) {{
  loadHealth();
  setInterval(loadHealth, 30000);
}}
</script>
</body>
</html>"""


# ─── Entry point ──────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("ENGRAM_PORT", 7090))
    cfg  = get_cfg()
    print(f"  engram running at http://localhost:{port}", flush=True)
    print(f"  Chat → http://localhost:{port}/", flush=True)
    print(f"  Health → http://localhost:{port}/health", flush=True)
    print(f"  Memory: {cfg.memory_path}", flush=True)
    print(f"  Wiki:   {cfg.wiki_path}", flush=True)
    app.run(host="0.0.0.0", port=port, debug=False)
