"""
engram.retrieval.curator — LLM-based context curation
======================================================

Two-pass Haiku-powered context management:

  Pass 1 — curate_context()
    Given ~30-50 candidate files from a wide scan, uses Haiku to select
    the most relevant files (≤ max_files) to load into the context window.
    Returns the selected candidates + a one-sentence reasoning string.

  Pass 2 — monitor_context()
    After the assistant has responded, re-evaluates the active context
    against the full exchange. Returns an add/remove/none action dict.

Both passes use the configured backend (CLI or API) with the haiku model.
On failure or timeout, both fall back gracefully (top-N / no-change).
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Optional

from .config import EngramConfig


# ─── Snippet helpers ──────────────────────────────────────────────────────────

def _snippet(path: str, base_path: Path, chars: int = 250) -> str:
    """
    Read the first `chars` characters of a file for candidate preview.
    Strips frontmatter dashes and blank lines for content density.
    """
    try:
        p = Path(path)
        if not p.is_absolute():
            p = base_path / path
        text = p.read_text(errors="ignore")
        lines = [
            ln.strip()
            for ln in text.splitlines()
            if ln.strip() and not ln.strip().startswith("---")
        ]
        return " ".join(lines)[:chars]
    except Exception:
        return ""


def build_candidates(
    scan: dict,
    cfg: EngramConfig,
    snippet_chars: int = 250,
) -> list[dict]:
    """
    Build a unified candidate list from memory_scan() output, with snippets.

    Each candidate:
        {
            "path":    str,   # relative (memory/graph) or absolute (wiki)
            "type":    str,   # "memory" | "graph" | "wiki"
            "snippet": str,   # first ~250 chars of content
        }

    Deduplicates by path; preserves insertion order (direct → graph → wiki).
    """
    base = cfg.base_path
    seen: set[str] = set()
    candidates: list[dict] = []

    def _add(paths: list[str], kind: str) -> None:
        for p in paths:
            if p not in seen:
                seen.add(p)
                candidates.append({
                    "path":    p,
                    "type":    kind,
                    "snippet": _snippet(p, base, snippet_chars),
                })

    _add(scan.get("direct", []), "memory")
    _add(scan.get("graph",  []), "graph")
    _add(scan.get("wiki",   []), "wiki")

    return candidates


# ─── Haiku backend ────────────────────────────────────────────────────────────

def _call_haiku(prompt: str, cfg: EngramConfig, timeout: int = 25) -> str:
    """
    Call Haiku with a prompt and return the plain-text response.

    Routes to CLI or API depending on cfg.chat.backend.
    Returns empty string on any failure (caller handles fallback).
    """
    chat_cfg   = getattr(cfg, "chat", None)
    backend    = getattr(chat_cfg, "backend", "api") if chat_cfg else "api"
    haiku_model = getattr(cfg.models, "haiku", None) or "claude-haiku-4-5"

    if backend == "cli":
        cli_bin = getattr(chat_cfg, "cli_bin", None) or shutil.which("claude")
        if not cli_bin:
            return ""
        try:
            result = subprocess.run(
                [cli_bin, "-p", prompt, "--model", haiku_model],
                capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                return result.stdout.strip()
            return ""
        except (subprocess.TimeoutExpired, OSError, Exception):
            return ""

    # API backend
    import os
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return ""
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        msg = client.messages.create(
            model      = haiku_model,
            max_tokens = 512,
            messages   = [{"role": "user", "content": prompt}],
        )
        return (msg.content[0].text or "").strip()
    except Exception:
        return ""


def _parse_json(text: str) -> dict:
    """
    Extract and parse a JSON object from a response that may have prose wrapping.
    Returns {} on failure.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)?\}', text, re.DOTALL)
    if m:
        try:
            return json.loads(m.group(0))
        except json.JSONDecodeError:
            pass
    return {}


# ─── Pass 1: Curate ───────────────────────────────────────────────────────────

def curate_context(
    query: str,
    candidates: list[dict],
    cfg: EngramConfig,
    max_files: int = 10,
) -> tuple[list[dict], str]:
    """
    Use Haiku to select the most relevant candidates for the context window.

    Args:
        query:      The user's natural-language query.
        candidates: All candidates from build_candidates() — typically 20-50.
        cfg:        EngramConfig (for backend + model info).
        max_files:  Maximum files to select.

    Returns:
        (selected_candidates, reasoning_str)

    On failure, falls back to top-N by scan order.
    """
    if not candidates:
        return [], "No candidates found"

    if len(candidates) <= max_files:
        return candidates, f"All {len(candidates)} candidates fit"

    # Pre-filter: send at most 25 candidates to Haiku (scan order = relevance order)
    HAIKU_CAP = 25
    haiku_cands = candidates[:HAIKU_CAP]

    # Build concise candidate list for the prompt
    lines: list[str] = []
    for i, c in enumerate(haiku_cands):
        name    = Path(c["path"]).stem.replace("_", " ")
        snippet = c["snippet"][:100].replace("\n", " ")
        lines.append(f"[{i}] {name}: {snippet}")

    prompt = (
        f'Context curator for AcmeCorp CPO assistant.\n'
        f'Query: "{query}"\n\n'
        f'Candidates (select ≤{max_files}):\n'
        + "\n".join(lines) +
        f'\n\nPick the MOST relevant. Prefer: named entities in query, active deals, recent decisions.\n'
        f'JSON only: {{"selected": [0,2,5], "reasoning": "one sentence"}}'
    )

    raw    = _call_haiku(prompt, cfg, timeout=30)
    result = _parse_json(raw)

    idxs      = result.get("selected", [])
    reasoning = result.get("reasoning", "") or "Selected by relevance"

    # Validate against haiku_cands (the prefix we actually sent)
    valid_idxs = [
        i for i in idxs
        if isinstance(i, int) and 0 <= i < len(haiku_cands)
    ]

    if not valid_idxs:
        # Fallback: top-N by scan order
        print(f"[curator] curation fallback (raw={raw[:120]!r})", flush=True)
        return candidates[:max_files], "Top-N by scan score (curator fallback)"

    selected = [haiku_cands[i] for i in valid_idxs[:max_files]]
    print(
        f"[curator] selected {len(selected)}/{len(candidates)} candidates — {reasoning[:80]}",
        flush=True,
    )
    return selected, reasoning


# ─── Pass 2: Monitor ─────────────────────────────────────────────────────────

def monitor_context(
    messages: list[dict],
    active_context: list[dict],
    all_candidates: list[dict],
    cfg: EngramConfig,
) -> dict:
    """
    After the assistant responds, decide if context should be updated.

    Args:
        messages:       Full conversation so far (including latest assistant turn).
        active_context: Currently loaded candidates.
        all_candidates: All candidates from the wide scan.
        cfg:            EngramConfig.

    Returns:
        {"action": "none"}
        or
        {"action": "update", "add": [candidate dicts], "remove": [candidate dicts], "reason": str}
    """
    if not all_candidates or not messages:
        return {"action": "none"}

    last_user = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "user"), ""
    )
    last_asst = next(
        (m["content"] for m in reversed(messages) if m.get("role") == "assistant"), ""
    )

    # Nothing to swap into/out of
    active_paths = {c["path"] for c in active_context}
    available    = [c for c in all_candidates if c["path"] not in active_paths]

    if not available and not active_context:
        return {"action": "none"}

    active_lines = [
        f"[active:{i}] {Path(c['path']).stem.replace('_', ' ')}"
        for i, c in enumerate(active_context)
    ]
    avail_lines = [
        f"[avail:{i}] ({c['type']}) {Path(c['path']).stem.replace('_', ' ')}: "
        f"{c['snippet'][:100].replace(chr(10), ' ')}"
        for i, c in enumerate(available)
    ]

    prompt = (
        "You are a context monitor for a CPO knowledge assistant.\n\n"
        f"Last user message: \"{last_user[:300]}\"\n"
        f"Last assistant response (excerpt): \"{last_asst[:400]}\"\n\n"
        f"Active context ({len(active_context)} files):\n"
        + ("\n".join(active_lines) or "(none)") +
        f"\n\nAvailable but not loaded ({len(available)} files):\n"
        + ("\n".join(avail_lines[:15]) or "(none)") +
        "\n\nShould the context change for the NEXT turn? Only suggest changes if clearly beneficial — "
        "the response just referenced something not in context, or a loaded file is clearly irrelevant.\n\n"
        "Reply ONLY with valid JSON on one line:\n"
        '- No change: {"action": "none"}\n'
        '- Update:    {"action": "update", "add": [avail indexes], "remove": [active indexes], "reason": "brief"}'
    )

    raw    = _call_haiku(prompt, cfg, timeout=20)
    result = _parse_json(raw)

    if result.get("action") != "update":
        return {"action": "none"}

    add_idxs    = [i for i in (result.get("add") or [])    if isinstance(i, int) and 0 <= i < len(available)]
    remove_idxs = [i for i in (result.get("remove") or []) if isinstance(i, int) and 0 <= i < len(active_context)]

    if not add_idxs and not remove_idxs:
        return {"action": "none"}

    reason = result.get("reason", "Context updated for next turn")
    print(
        f"[curator/monitor] update — add {len(add_idxs)}, remove {len(remove_idxs)}: {reason[:80]}",
        flush=True,
    )
    return {
        "action": "update",
        "add":    [available[i]     for i in add_idxs],
        "remove": [active_context[i] for i in remove_idxs],
        "reason": reason,
    }
