"""
engram.retrieval.curator — LLM-based context curation
======================================================

Two-pass Haiku-powered context management:

  Pass 1 — curate_context()
    Given ~30-50 candidate files from a wide scan, uses Haiku to select
    the most relevant files (≤ max_files) to load into the context window.
    Loads session_priming.json and open_questions.json as priors so the
    curator is aware of recurring topics and unresolved threads.
    Returns the selected candidates + a one-sentence reasoning string.

  Pass 2 — monitor_context()
    After the assistant has responded, re-evaluates the active context
    against the full exchange. Returns an add/remove/none action dict.

Drift detection (detect_drift):
    Compares the current query to the previous user turn. If entity-overlap
    exceeds DRIFT_SKIP_THRESHOLD, the curator pass is skipped (no Haiku call)
    and the previous context is reused. Only meaningful topic shifts trigger
    a full re-curation.

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
from .tokenizer import query_tokens


# ─── Drift detection ──────────────────────────────────────────────────────────

# If token overlap between new query and previous user turn exceeds this
# fraction, skip the Haiku curation pass (context hasn't drifted).
DRIFT_SKIP_THRESHOLD: float = 0.20

# Common English stop words excluded from drift comparison
_STOP = frozenset({
    "what", "who", "is", "are", "the", "a", "an", "in", "on", "at", "of",
    "to", "for", "and", "or", "but", "from", "with", "about", "this", "that",
    "can", "will", "how", "me", "tell", "give", "let", "know", "its", "my",
    "your", "our", "their", "there", "here", "be", "do", "have", "has", "had",
    "was", "were", "am", "get", "got", "any", "which", "when", "where", "why",
    "if", "then", "so", "as", "by", "up", "out", "more", "please", "update",
    "status", "latest", "current", "new", "old", "between", "each", "all",
})


def _drift_tokens(text: str) -> frozenset[str]:
    """
    Broad tokenizer for drift detection. Keeps 2-char tokens (e.g. 'vw', 'gm')
    and strips only the most common stop words. More inclusive than query_tokens.
    """
    import re
    words = re.findall(r"[a-zA-Z][a-zA-Z0-9']{0,}", text.lower())
    return frozenset(w for w in words if len(w) >= 2 and w not in _STOP)


def detect_drift(query: str, messages: list[dict]) -> bool:
    """
    Return True if the conversation has drifted enough to warrant re-curation.

    Uses Jaccard similarity of query tokens vs the previous user turn.
    Drift  is detected when similarity < DRIFT_SKIP_THRESHOLD, or when
    this is the first message in the conversation.

    Args:
        query:    The current user query.
        messages: Full message list (current query is last).

    Returns:
        True  → topic shifted, run full Haiku curation.
        False → same topic, skip Haiku and reuse previous context.
    """
    # First message: always curate
    prev_user_msgs = [
        m["content"] for m in messages[:-1]
        if m.get("role") == "user" and m.get("content")
    ]
    if not prev_user_msgs:
        return True

    prev_query = prev_user_msgs[-1]
    curr_toks  = _drift_tokens(query)
    prev_toks  = _drift_tokens(prev_query)

    union = curr_toks | prev_toks
    if not union:
        return True

    jaccard = len(curr_toks & prev_toks) / len(union)
    drifted = jaccard < DRIFT_SKIP_THRESHOLD

    if not drifted:
        print(
            f"[curator/drift] no drift (jaccard={jaccard:.2f}) — skipping Haiku curation",
            flush=True,
        )
    else:
        print(
            f"[curator/drift] topic shifted (jaccard={jaccard:.2f}) — running curation",
            flush=True,
        )

    return drifted


# ─── Session priors ───────────────────────────────────────────────────────────

def _load_session_priming(memory_path: Path, top_n: int = 6) -> str:
    """
    Load top-N primed entity names from session_priming.json.

    Returns a short string like "Recent focus: VW, AcmeTech, Alice Chen"
    or empty string if not available.
    """
    candidates = [
        memory_path / "priming" / "session_priming.json",
        memory_path / "session_priming.json",
    ]
    for p in candidates:
        if not p.exists():
            continue
        try:
            data = json.loads(p.read_text())
            # Format: {"default": {"nodes": {"entity_id": strength, ...}}}
            # or just {"entity_id": strength}
            nodes: dict = {}
            if isinstance(data, dict):
                first = next(iter(data.values()), {})
                if isinstance(first, dict) and "nodes" in first:
                    nodes = first["nodes"]  # session_id → PrimingVector dict
                elif all(isinstance(v, (int, float)) for v in data.values()):
                    nodes = data
                else:
                    # Try each value as a PrimingVector dict
                    for v in data.values():
                        if isinstance(v, dict) and "nodes" in v:
                            nodes.update(v["nodes"])

            if not nodes:
                return ""

            # Sort by strength, take top-N
            top = sorted(nodes.items(), key=lambda x: -float(x[1]))[:top_n]

            # Resolve entity IDs to names if graph is accessible
            graph_file = memory_path / "graph.json"
            if graph_file.exists():
                try:
                    g = json.loads(graph_file.read_text(errors="ignore"))
                    entities = g.get("entities", {})
                    names = [
                        entities.get(eid, {}).get("name", eid)
                        for eid, _ in top
                        if entities.get(eid, {}).get("name")
                    ]
                    if names:
                        return "Recent session focus: " + ", ".join(names[:top_n])
                except Exception:
                    pass

            # Fallback: just use entity IDs
            ids = [eid for eid, _ in top]
            return "Recent session focus: " + ", ".join(ids[:top_n])

        except Exception:
            continue

    return ""


def _load_open_questions(memory_path: Path, top_n: int = 4) -> str:
    """
    Load top-N unresolved open questions from open_questions.json.

    Returns a bulleted string or empty string if none.
    """
    p = memory_path / "open_questions.json"
    if not p.exists():
        return ""
    try:
        data = json.loads(p.read_text())
        qs = data if isinstance(data, list) else data.get("questions", [])
        open_qs = [
            q for q in qs
            if q.get("status") not in ("resolved", "dismissed", "closed")
            and q.get("text")
        ]
        if not open_qs:
            return ""
        # Sort by salience if available
        open_qs.sort(key=lambda q: -float(q.get("salience", 0.5)))
        texts = [q["text"][:120] for q in open_qs[:top_n]]
        return "Unresolved open questions:\n" + "\n".join(f"  • {t}" for t in texts)
    except Exception:
        return ""


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

    def _add(paths: list[str], kind: str, source_tier: str = "") -> None:
        for p in paths:
            if p not in seen:
                seen.add(p)
                candidates.append({
                    "path":        p,
                    "type":        kind,
                    "source_tier": source_tier,
                    "snippet":     _snippet(p, base, snippet_chars),
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

    # Load session priors (priming + open questions)
    memory_path = getattr(cfg, "memory_path", None)
    priming_ctx = _load_session_priming(memory_path) if memory_path else ""
    oq_ctx      = _load_open_questions(memory_path)  if memory_path else ""

    # Build concise candidate list for the prompt
    lines: list[str] = []
    for i, c in enumerate(haiku_cands):
        name    = Path(c["path"]).stem.replace("_", " ")
        snippet = c["snippet"][:100].replace("\n", " ")
        tier    = c.get("source_tier", "")
        tier_tag = f" [{tier}]" if tier else ""
        lines.append(f"[{i}]{tier_tag} {name}: {snippet}")

    prior_block = ""
    if priming_ctx:
        prior_block += priming_ctx + "\n"
    if oq_ctx:
        prior_block += oq_ctx + "\n"

    prompt = (
        f'Context curator for a CPO knowledge assistant.\n'
        + (f'{prior_block}\n' if prior_block else "")
        + f'Query: "{query}"\n\n'
        f'Candidates (select ≤{max_files}):\n'
        + "\n".join(lines) +
        f'\n\nPick the MOST relevant. Prefer: named entities in query, active deals, '
        f'recent decisions, files matching open questions.\n'
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
