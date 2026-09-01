#!/usr/bin/env python3
"""
test-creative-prompts.py — A/B test the "creative-thinking pack" hypothesis.

For each test prompt we run TWO variants through Sonnet 4.5:

  A) baseline       — standard engram retrieval (wiki + memory + graph_context),
                       exactly as the dashboard chat builds it (minus calendar /
                       always-load / raw-docs to keep the test on-topic).

  B) creative pack  — baseline + a "frictions appendix" containing:
                       • historical contradictions (paired claims), recency-ranked
                       • closed open-questions, recency-ranked
                       • weak-edge bridges from graph.json (low-weight edges
                         between high-degree entities = structurally noteworthy
                         uncertain connections)

Output is written to ./creative-prompts-results.md as a side-by-side comparison.

Run:
    export ANTHROPIC_API_KEY=...      # same key the dashboard uses
    python scripts/test-creative-prompts.py

Args:
    --prompts FILE     newline-separated prompts file (default: built-in)
    --out FILE         output markdown (default: creative-prompts-results.md)
    --model NAME       override Sonnet model (default: cfg.models.primary)
    --max-files N      retrieval cap per arm (default: 12)
    --pack-size N      items per creative-pack section (default: 6)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

# Make `engram` importable when run from repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from engram.retrieval.config import load_config
from engram.retrieval.pipeline import memory_scan
from engram.retrieval.curator import build_candidates


# ── Default prompts: Leo's last 3 messages in this conversation ────────────────
DEFAULT_PROMPTS = [
    "I want to explore JEPA to create a view of abstract concept on top of the "
    "wiki and graph. What would that take and how could we test this?",

    "Is wiki + weighted graph the best way to learn about 'company knowledge "
    "and concepts'?",

    "I was exploring if there would be another way to 'represent' that "
    "knowledge to help 'creative thinking'.",
]


# ─── System-prompt assembly (shared by both arms) ─────────────────────────────

def _identity_preamble(cfg) -> str:
    idc = cfg.identity
    parts = []
    if idc.user_name or idc.user_role:
        s = f"You are an AI assistant for {idc.user_name}"
        if idc.user_role:  s += f", {idc.user_role}"
        if idc.org_name:   s += f" at {idc.org_name}"
        parts.append(s + ".")
    sp = cfg.system_prompt
    if sp.user_tone:
        parts.append(f"Tone: {sp.user_tone}")
    return "\n".join(parts)


def _load_files(selected: list[dict], cfg, budget: int = 60_000) -> str:
    """Concatenate the selected files into a single context block."""
    base  = cfg.base_path
    trunc = cfg.retrieval.context_budget.wiki_page_truncate_chars
    used  = 0
    out: list[str] = []
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
        except Exception:
            continue
        if c["type"] == "wiki":
            cap   = min(trunc, budget - used)
            label = f"Wiki: {p.stem}"
        else:
            cap   = min(5000, budget - used)
            label = f"Context: {c['path']}"
        if len(content) > cap:
            content = content[:cap] + "\n…(truncated)"
        out.append(f"\n\n---\n# {label}\n\n{content}")
        used += len(content)
    return "".join(out)


def build_baseline_system(query: str, cfg, max_files: int) -> tuple[str, dict]:
    """Standard engram chat system prompt (no creative pack)."""
    scan       = memory_scan(query, cfg, max_files=max_files)
    candidates = build_candidates(scan, cfg, snippet_chars=400)
    selected   = candidates[:max_files]

    parts = [_identity_preamble(cfg)]
    parts.append(_load_files(selected, cfg))
    if scan.get("graph_context"):
        parts.append("\n\n" + scan["graph_context"])
    return "\n".join(parts), {"scan": scan, "selected": selected}


# ─── Creative-pack assembly ───────────────────────────────────────────────────

_BOILERPLATE_RE = re.compile(r"\*\*(Status|Review):\*\*.*?(\n|$)", re.DOTALL)

def _clean_question_text(raw: str) -> str:
    t = _BOILERPLATE_RE.sub("", raw or "").strip()
    return t or raw or ""


def _historical_contradictions(mem: Path, n: int) -> list[dict]:
    """
    Recency-ranked contradictions, deduped by (subject, relation).

    Without dedup the pack gets dominated by repeated disputes about the
    same fact (e.g. "Leo reports_to X" eight times with different X). We
    want creative-substrate variety, not the same tension echoed.
    """
    p = mem / "contradictions.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(errors="ignore"))
    items = d if isinstance(d, list) else d.get("contradictions", [])

    def _key(it: dict) -> str:
        return (it.get("created_at")
                or (it.get("claim_B") or {}).get("date")
                or (it.get("claim_A") or {}).get("date") or "")
    items.sort(key=_key, reverse=True)

    # Dedupe by (subject, relation) — extracted from claim_B.statement, falling
    # back to claim_A. Statements look like "<Subject> <relation> <Object>".
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for it in items:
        stmt = ((it.get("claim_B") or {}).get("statement")
                or (it.get("claim_A") or {}).get("statement") or "")
        toks = stmt.split()
        if len(toks) >= 3:
            # subject = first 1-3 words up to a known relation token
            for i in range(1, min(4, len(toks))):
                if "_" in toks[i] or toks[i].lower() in {"is", "has", "was", "were", "owns", "reports", "leads", "manages"}:
                    subj, rel = " ".join(toks[:i]), toks[i]
                    break
            else:
                subj, rel = toks[0], toks[1]
        else:
            subj, rel = stmt, ""
        key = (subj.lower(), rel.lower())
        if key in seen:
            continue
        seen.add(key)
        out.append(it)
        if len(out) >= n:
            break
    return out


_QUESTION_HINTS = (
    "?", "need to", "unclear", "confusion", "confirm", "does ", "should ",
    "what ", "how ", "why ", "when ", "who ", "where ",
)

def _looks_like_a_question(text: str) -> bool:
    """
    The harvester scrapes a lot of markdown sections as 'questions'. Keep
    only ones that read as genuine inquiry: explicit '?', or an inquiry
    verb/phrase, and not just a header line.
    """
    t = (text or "").strip()
    if not t:
        return False
    # Strip leading markdown markers and check the first real content
    first_line = next((ln for ln in t.splitlines() if ln.strip()), "")
    if first_line.lstrip("#-*> ").strip() != first_line.strip():
        # Keep if the *rest* of the text has question markers
        body = "\n".join(t.splitlines()[1:]).lower()
    else:
        body = t.lower()
    return any(h in body for h in _QUESTION_HINTS)


def _historical_questions(mem: Path, n: int) -> list[dict]:
    p = mem / "open_questions.json"
    if not p.exists():
        return []
    d = json.loads(p.read_text(errors="ignore"))
    qs = d if isinstance(d, list) else d.get("questions", [])
    qs = [q for q in qs
          if _clean_question_text(q.get("text", "")).strip()
          and _looks_like_a_question(_clean_question_text(q.get("text", "")))]
    qs.sort(key=lambda q: q.get("created_at", ""), reverse=True)
    return qs[:n]


def _weak_edge_bridges(mem: Path, n: int, weight_max: float = 0.4) -> list[dict]:
    """
    Find graph edges with low weight between entities that are themselves
    well-connected (degree >= 3). These are 'structurally noteworthy'
    uncertain connections — exactly what we want for creative substrate.
    """
    p = mem / "graph.json"
    if not p.exists():
        return []
    g = json.loads(p.read_text(errors="ignore"))
    edges    = g.get("edges", [])
    entities = g.get("entities", {})

    # Compute degree per entity
    deg: Counter = Counter()
    for e in edges:
        deg[e.get("from", "")] += 1
        deg[e.get("to",   "")] += 1

    weak = []
    for e in edges:
        w = float(e.get("weight", 1.0))
        if w > weight_max:
            continue
        a, b = e.get("from", ""), e.get("to", "")
        if deg.get(a, 0) < 3 or deg.get(b, 0) < 3:
            continue
        ent_a = entities.get(a, {}) or {}
        ent_b = entities.get(b, {}) or {}
        weak.append({
            "from": ent_a.get("name", a),
            "to":   ent_b.get("name", b),
            "type": e.get("type", "related_to"),
            "weight":     round(w, 3),
            "confidence": round(float(e.get("confidence", 0.0)), 3),
            "deg_a": deg[a], "deg_b": deg[b],
            "sources": (e.get("sources") or [])[:2],
        })
    # Surface the most informative ones first: low weight × high combined degree
    weak.sort(key=lambda x: (x["weight"], -(x["deg_a"] + x["deg_b"])))
    return weak[:n]


def render_creative_pack(mem: Path, pack_size: int) -> str:
    contras = _historical_contradictions(mem, pack_size)
    quests  = _historical_questions(mem, pack_size)
    bridges = _weak_edge_bridges(mem, pack_size)

    lines: list[str] = []
    lines.append("\n\n---\n# Frictions appendix — creative-thinking substrate\n")
    lines.append(
        "Below are tensions, questions, and uncertain connections drawn from "
        "the user's own knowledge graph. They are **not facts to recite**. "
        "Treat them as friction points: tensions hint at alternate frames; "
        "questions point at unexplored angles; weak edges suggest unexpected "
        "analogies. Reach for them when the prompt invites lateral thinking. "
        "Cite them only when they sharpen your answer.\n"
    )

    lines.append("\n## Historical tensions (paired claims the system has flagged)\n")
    if not contras:
        lines.append("_(none)_")
    for c in contras:
        a = c.get("claim_A", {}); b = c.get("claim_B", {})
        lines.append(
            f"- **Tension** (`{c.get('severity', 'medium')}`, status: "
            f"`{c.get('status', 'unresolved')}`)\n"
            f"  - A: *{a.get('statement', '')}* "
            f"(source: `{a.get('source', '?')}`, weight {a.get('weight', '?')})\n"
            f"  - B: *{b.get('statement', '')}* "
            f"(source: `{b.get('source', '?')}`, weight {b.get('weight', '?')})"
        )

    lines.append("\n## Open questions (raised, possibly closed — kept as inquiry)\n")
    if not quests:
        lines.append("_(none)_")
    for q in quests:
        raw = _clean_question_text(q.get("text", ""))
        # Pick the most question-like line (one containing an inquiry hint),
        # falling back to first non-header line. Avoids surfacing markdown
        # header noise like "## People Mentioned" as the question.
        cand_lines = [ln.strip().lstrip("-*> ").strip()
                      for ln in raw.splitlines()
                      if ln.strip() and not ln.lstrip().startswith("#")]
        best = next(
            (ln for ln in cand_lines if any(h in ln.lower() for h in _QUESTION_HINTS)),
            cand_lines[0] if cand_lines else raw,
        )
        lines.append(
            f"- *{best[:240]}* "
            f"(from `{q.get('created_from', '?')}`, priority "
            f"`{q.get('priority', '?')}`)"
        )

    lines.append("\n## Weak-edge bridges (low-confidence connections in the graph)\n")
    if not bridges:
        lines.append("_(none)_")
    for br in bridges:
        lines.append(
            f"- **{br['from']}** —[`{br['type']}`, weight {br['weight']}]→ "
            f"**{br['to']}**  "
            f"(degrees {br['deg_a']}/{br['deg_b']}; "
            f"sources: {', '.join(br['sources']) or '—'})"
        )

    return "\n".join(lines) + "\n"


def build_creative_system(query: str, cfg, max_files: int, pack_size: int) -> tuple[str, dict]:
    base_sys, meta = build_baseline_system(query, cfg, max_files)
    pack = render_creative_pack(cfg.memory_path, pack_size)
    return base_sys + pack, meta


# ─── LLM call ─────────────────────────────────────────────────────────────────

def call_sonnet(system: str, user_msg: str, model: str, api_key: str) -> tuple[str, dict]:
    import anthropic
    client = anthropic.Anthropic(api_key=api_key)
    t0 = time.time()
    resp = client.messages.create(
        model      = model,
        max_tokens = 2048,
        system     = system,
        messages   = [{"role": "user", "content": user_msg}],
    )
    elapsed = time.time() - t0
    text = "".join(b.text for b in resp.content if hasattr(b, "text"))
    return text, {
        "elapsed_s":     round(elapsed, 2),
        "input_tokens":  resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
    }


# ─── Result rendering ─────────────────────────────────────────────────────────

def render_result(prompt_idx: int, prompt: str,
                  base_text: str, base_meta: dict,
                  pack_text: str, pack_meta: dict,
                  scan_summary: dict, pack_summary: dict) -> str:
    lines = []
    lines.append(f"\n\n{'='*80}\n")
    lines.append(f"# Prompt {prompt_idx + 1}\n\n> {prompt}\n")

    lines.append(f"\n**Retrieval (shared)**: "
                 f"{scan_summary['direct']} direct, "
                 f"{scan_summary['graph']} graph, "
                 f"{scan_summary['wiki']} wiki files. "
                 f"Graph context: {scan_summary['graph_context_chars']} chars.\n")
    lines.append(f"**Creative pack injected**: "
                 f"{pack_summary['tensions']} tensions, "
                 f"{pack_summary['questions']} open questions, "
                 f"{pack_summary['bridges']} weak-edge bridges.\n")

    lines.append(f"\n## A — Baseline  "
                 f"({base_meta['elapsed_s']}s, "
                 f"in={base_meta['input_tokens']}, out={base_meta['output_tokens']})\n")
    lines.append(base_text)

    lines.append(f"\n## B — Baseline + creative pack  "
                 f"({pack_meta['elapsed_s']}s, "
                 f"in={pack_meta['input_tokens']}, out={pack_meta['output_tokens']})\n")
    lines.append(pack_text)

    return "\n".join(lines)


# ─── Main ─────────────────────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--prompts",   default=None, help="newline-separated prompts file")
    ap.add_argument("--out",       default="creative-prompts-results.md")
    ap.add_argument("--model",     default=None, help="override Sonnet model")
    ap.add_argument("--max-files", type=int, default=12)
    ap.add_argument("--pack-size", type=int, default=6)
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY not set in environment.", file=sys.stderr)
        print("Run from a shell that has it exported (the one you use for the dashboard).", file=sys.stderr)
        return 2

    cfg   = load_config()
    model = args.model or cfg.models.primary
    mem   = cfg.memory_path

    if args.prompts:
        prompts = [p.strip() for p in Path(args.prompts).read_text().splitlines() if p.strip()]
    else:
        prompts = DEFAULT_PROMPTS

    print(f"[test] model={model}  prompts={len(prompts)}  memory={mem}", flush=True)

    out_lines = [
        f"# Creative-prompts A/B test — {time.strftime('%Y-%m-%d %H:%M')}",
        f"\nModel: `{model}`  |  memory: `{mem}`  |  max_files={args.max_files}  "
        f"|  pack_size={args.pack_size}\n",
        "\nFor each prompt, both arms use the **same retrieval** "
        "(`memory_scan` + top-N files + `graph_context`). Arm **B** additionally "
        "appends a 'frictions appendix' built from historical contradictions, "
        "open questions, and weak-edge bridges in the graph.\n",
    ]

    for i, p in enumerate(prompts):
        print(f"\n[test] === prompt {i+1}/{len(prompts)} ===\n  {p[:120]}", flush=True)

        # Baseline (also returns the shared scan; reuse it to keep retrieval fair)
        base_sys, meta = build_baseline_system(p, cfg, args.max_files)
        scan           = meta["scan"]
        scan_summary   = {
            "direct":              len(scan.get("direct", [])),
            "graph":               len(scan.get("graph", [])),
            "wiki":                len(scan.get("wiki", [])),
            "graph_context_chars": len(scan.get("graph_context", "")),
        }

        # Creative pack is the baseline + a frictions appendix
        pack = render_creative_pack(mem, args.pack_size)
        pack_sys = base_sys + pack
        pack_summary = {
            "tensions":  len(_historical_contradictions(mem, args.pack_size)),
            "questions": len(_historical_questions(mem, args.pack_size)),
            "bridges":   len(_weak_edge_bridges(mem, args.pack_size)),
        }

        print(f"[test]   calling Sonnet (baseline)…", flush=True)
        base_text, base_meta = call_sonnet(base_sys, p, model, api_key)
        print(f"[test]   calling Sonnet (creative pack)…", flush=True)
        pack_text, pack_meta = call_sonnet(pack_sys, p, model, api_key)

        out_lines.append(render_result(
            i, p,
            base_text, base_meta,
            pack_text, pack_meta,
            scan_summary, pack_summary,
        ))

    out_path = Path(args.out)
    out_path.write_text("\n".join(out_lines))
    print(f"\n[test] wrote {out_path} ({out_path.stat().st_size} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
