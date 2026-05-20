"""
engram.memory.council — deep-think council mode
=================================================

Multi-persona reasoning orchestrator. Used by the dashboard's /api/deep-think
endpoint when the user invokes /think (or clicks the 💭 button).

Flow
----
  1. **Routing** — orchestrator picks 2–4 personas from team.SPECIALISTS based
     on the question. Personas are chosen to maximise *useful disagreement*.
  2. **Phase 1**  — each persona writes an independent take in parallel.
  3. **Phase 2**  — *(optional, controlled by deep_work.cross_read)* each
     persona reads the others' phase-1 takes and writes a revised stance.
     This is the "interaction" — they react to each other in one round.
  4. **Synthesis** — orchestrator reads all final stances and emits a JSON
     synthesis: a conclusion, *why* this conclusion, and (when the council
     didn't converge) a list of decision options for the user to pick from.

Modes
-----
Default is **CLI subprocess** (mode="cli") — every model call is a
``claude -p <prompt>`` invocation against the user's logged-in account.
No API key needed. ``mode="sdk"`` is the SDK alternative, kept available
for future use.

Concurrency
-----------
Personas run in a ``ThreadPoolExecutor``. Each worker calls ``subprocess.run``
which releases the GIL while waiting on the child process, so N personas
genuinely execute in parallel. The caller supplies an ``on_event`` callback
which is invoked from worker threads — the dashboard endpoint wraps it in
a ``queue.Queue`` for thread-safe SSE forwarding.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

from engram.memory.team import SPECIALISTS  # reuse the persona system-prompts


# ─── Prompts ──────────────────────────────────────────────────────────────────

# Each phase is split into a SYSTEM prompt and a USER prompt. The system
# prompt goes to the CLI via --system-prompt (which OVERRIDES Claude Code's
# default coding-only scope — without this, the CLI refuses non-software
# questions and the council can't operate on strategic/operational topics).

_ROUTING_SYSTEM = """You are the orchestrator of an advisory council that deliberates on the user's strategic, operational, and organisational questions. You are NOT Claude Code — you are not constrained to software engineering tasks. Your job here is to pick which advisors (out of a fixed roster) should weigh in on each question to maximise useful disagreement.

You will receive a roster and a question. You must reply with STRICT JSON ONLY — no prose, no markdown fences, no explanation around the JSON. Just the object."""

_ROUTING_USER = """Convene a council of 2-4 advisors for this question.

Bias toward voices that argue from DIFFERENT STAKES (financial vs commercial vs people vs long-horizon), not different topics. Include a contrarian unless the question is purely informational.

Available roster:
{roster}

User's question:
<<<{query}>>>

Reply with STRICT JSON ONLY:
{{
  "personas":  ["<key1>", "<key2>", "<key3>"],
  "rationale": "one short sentence on why this panel for this question"
}}

Rules:
- Pick 2, 3, or 4 keys (lowercase, exact)
- Use only keys from the roster above"""


_PHASE1_SYSTEM_PREAMBLE = """You are an advisor on a deliberation council convened to help the user reason through a strategic, operational, or organisational question. You are NOT Claude Code and you are not constrained to software engineering tasks — your job here is to give a sharp, opinionated, fact-grounded perspective from your specific lens.

YOUR ROLE ON THIS COUNCIL:
{persona_system}"""

_PHASE1_USER = """## CONTEXT (loaded memory + uploaded files)
{context}

---
## QUESTION
{query}

Give your independent take in EXACTLY this format (markdown bold labels, no preamble):

**Position** (2-3 sentences): ...
**Top concern** (one sentence — the single biggest risk or trade-off from your lens): ...
**Recommendation** (one sentence — what you would do): ..."""


# Cross-read uses the same persona system; only the user message differs.
_CROSSREAD_USER = """You earlier wrote this take on the question:

YOUR EARLIER TAKE:
<<<
{your_take}
>>>

Your fellow advisors wrote these takes:

{others_block}

Now revise. Be honest about what changed your mind, what didn't, and what you'd push back on. Use EXACTLY this format:

**Where I agree with others** (1-2 bullets, name the advisor): ...
**Where I push back** (1-2 bullets, name the advisor and the disagreement): ...
**My revised position** (2-4 sentences — your final stance for the synthesiser): ..."""


_SYNTHESIS_SYSTEM = """You are the chair of an advisory council, synthesising your advisors' deliberation into a single recommendation for the user. You are NOT Claude Code — you are not constrained to software engineering. Your job is to produce a terse, decisive synthesis or, when the council genuinely split, a clear set of decision options.

You must reply with STRICT JSON ONLY — no prose, no markdown fences, no explanation. Just the JSON object."""

_SYNTHESIS_USER = """QUESTION:
<<<{query}>>>

Each advisor's REVISED stance (after they saw each other's takes):

{stances_block}

Write the synthesis. Reply with STRICT JSON ONLY:
{{
  "conclusion":          "<the recommendation or finding, 1-3 short paragraphs, plain markdown allowed inside the string>",
  "why_this_conclusion": "<1 paragraph naming which advisors carried weight and why their argument won>",
  "decision_required":   true,
  "options": [
    {{ "label": "<short verb-led option>", "summary": "<1-sentence description of what choosing this means>" }}
  ],
  "disagreement_notes":  "<1 short paragraph on residual disagreement, or empty string>"
}}

Use ``decision_required=true`` ONLY when the council genuinely split on the substantive recommendation (not just framing nits). In that case produce 2-3 *mutually exclusive* options the user could pick between, and the conclusion should explain the trade-off rather than make the call. Otherwise set ``decision_required=false`` and ``options=[]`` and make the call yourself.

Conclusion must be terse — no preamble, no "I think", no filler. Lead with the call."""


# ─── CLI / SDK shim ──────────────────────────────────────────────────────────

def _resolve_cli_bin(cfg) -> Optional[str]:
    bin_ = (getattr(getattr(cfg, "chat",  None), "cli_bin",    None)
            or getattr(getattr(cfg, "paths", None), "claude_bin", None)
            or shutil.which("claude"))
    return bin_ or None


def _cli_call(user: str, *, system: str, model: str, cli_bin: str, timeout: int) -> str:
    # --system-prompt OVERRIDES Claude Code's default coding-only system prompt.
    # Without this, the CLI refuses anything not framed as software engineering
    # ("This request about business strategy is outside my scope.") which makes
    # the council unusable for the very questions it was built to handle.
    cmd = [cli_bin]
    if system:
        cmd += ["--system-prompt", system]
    cmd += ["-p", user, "--output-format", "text", "--model", model]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return (proc.stdout or "").strip()


def _sdk_call(user: str, *, system: str, model: str, max_tokens: int = 2000) -> str:
    import anthropic
    client = anthropic.Anthropic()
    kwargs = {
        "model":      model,
        "max_tokens": max_tokens,
        "messages":   [{"role": "user", "content": user}],
    }
    if system:
        kwargs["system"] = system
    resp = client.messages.create(**kwargs)
    out = ""
    for block in (resp.content or []):
        if getattr(block, "type", "") == "text":
            out += getattr(block, "text", "")
    return out


def _call(user: str, *, system: str = "", mode: str, model: str,
          cli_bin: Optional[str], timeout: int) -> str:
    if mode == "sdk":
        return _sdk_call(user, system=system, model=model)
    if not cli_bin:
        raise RuntimeError("mode=cli but claude binary not found on PATH")
    return _cli_call(user, system=system, model=model, cli_bin=cli_bin, timeout=timeout)


def _extract_json(raw: str) -> Optional[dict]:
    if not raw:
        return None
    cleaned = re.sub(r"```(?:json)?", "", raw).strip().strip("`").strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def _format_roster(allowed: dict) -> str:
    return "\n".join(
        f"- {key}: {info.get('short', info.get('label', key))}"
        for key, info in allowed.items()
    )


# ─── Persona selection ────────────────────────────────────────────────────────

def select_personas(*, query: str, cfg, cli_bin: Optional[str], allowed: dict) -> dict:
    """Returns {keys: [persona_key, ...], rationale: str, raw: <preview>}.

    Falls back to a default panel if the orchestrator's JSON is unparseable
    or names personas not in the roster. Logs the raw output on fallback so
    we can tell *why* (timeout vs auth vs model-not-available vs JSON-with-preamble).
    """
    mode   = cfg.deep_work.mode
    model  = cfg.deep_work.routing_model
    user_prompt = _ROUTING_USER.format(roster=_format_roster(allowed), query=query[:1500])
    raw = ""
    err = ""
    t0  = time.time()
    try:
        raw = _call(user_prompt, system=_ROUTING_SYSTEM, mode=mode, model=model,
                    cli_bin=cli_bin, timeout=60)
    except subprocess.TimeoutExpired:
        err = f"CLI timed out after 60s on model={model}"
        print(f"[council] routing TIMEOUT (60s) model={model}", flush=True)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"[:200]
        print("[council] routing failed:\n" + traceback.format_exc(), flush=True)

    elapsed = round(time.time() - t0, 1)
    parsed = _extract_json(raw) or {}
    keys = [k for k in (parsed.get("personas") or []) if k in allowed]
    if keys:
        return {
            "keys":      keys[:4],
            "rationale": (parsed.get("rationale") or "")[:240],
        }

    # ── Fallback path — log what the orchestrator actually returned so we
    # can diagnose without re-running. Includes the elapsed time so timeouts
    # vs slow-but-bad responses are distinguishable.
    preview = (raw or "(empty)")[:500].replace("\n", "\\n")
    if err:
        rationale = f"fallback — orchestrator error: {err}"
    elif not raw.strip():
        rationale = f"fallback — orchestrator returned empty output ({elapsed}s)"
    else:
        rationale = f"fallback — orchestrator output not valid JSON ({elapsed}s)"
    print(f"[council] routing fallback ({elapsed}s, model={model}): {rationale}", flush=True)
    print(f"[council]   raw[:500]: {preview}", flush=True)
    keys = [k for k in ("contrarian", "strategy", "commercial") if k in allowed][:3]
    return {"keys": keys, "rationale": rationale}


# ─── Main entry point ─────────────────────────────────────────────────────────

def run_council(
    *,
    query:        str,
    context_text: str,
    cfg,
    on_event:     Callable[[dict], None],
) -> dict:
    """Run the full council pipeline and return the transcript dict.

    ``on_event`` is called from worker threads — the caller must make it
    thread-safe (the dashboard endpoint uses a ``queue.Queue``).

    Events emitted:
      {"phase": "selecting"}
      {"phase": "selected",  "personas": [...], "rationale": "..."}
      {"phase": "phase1"}
      {"persona_started": key, "phase": "phase1"}
      {"persona_done":    key, "phase": "phase1"}
      {"phase": "crossread"}                    # only if cross_read=true
      {"persona_started"/persona_done as above, phase: "crossread"}
      {"phase": "synthesizing"}
      {"council_done": <full transcript>}        # terminal
      {"council_error": "<reason>"}              # on fatal error
    """
    started = time.time()
    mode    = cfg.deep_work.mode
    cli_bin = _resolve_cli_bin(cfg)
    if mode == "cli" and not cli_bin:
        on_event({"council_error": "Claude CLI not found on PATH"})
        return {"error": "no_cli_bin"}

    # Build the effective roster — config can disable specialists.
    allowed = {
        k: SPECIALISTS[k]
        for k, info in cfg.deep_work.specialists.items()
        if k in SPECIALISTS and info.get("enabled", True)
    }
    if not allowed:
        on_event({"council_error": "No personas enabled in deep_work.specialists"})
        return {"error": "no_personas"}

    # ── Phase A: routing ─────────────────────────────────────────────────
    on_event({"phase": "selecting"})
    pick = select_personas(query=query, cfg=cfg, cli_bin=cli_bin, allowed=allowed)
    personas = [{
        "key":   k,
        "label": allowed[k].get("label", k),
        "icon":  allowed[k].get("icon",  "·"),
        "color": allowed[k].get("color", "#888"),
        "lens":  allowed[k].get("short", ""),
    } for k in pick["keys"]]
    on_event({
        "phase":     "selected",
        "personas":  personas,
        "rationale": pick["rationale"],
    })

    # ── Phase B: independent takes in parallel ───────────────────────────
    on_event({"phase": "phase1"})

    def _phase1_one(key: str) -> tuple[str, str]:
        on_event({"persona_started": key, "phase": "phase1"})
        sys_prompt = _PHASE1_SYSTEM_PREAMBLE.format(persona_system=SPECIALISTS[key]["system"])
        user_msg = _PHASE1_USER.format(
            context=context_text[:8000] if context_text else "(no preloaded context)",
            query=query[:2000],
        )
        try:
            text = _call(
                user_msg, system=sys_prompt, mode=mode,
                model=cfg.deep_work.specialist_model,
                cli_bin=cli_bin, timeout=120,
            )
        except Exception:
            print(f"[council] phase1 {key} error:\n" + traceback.format_exc(), flush=True)
            text = ""
        on_event({"persona_done": key, "phase": "phase1"})
        return (key, text)

    phase1: dict = {}
    keys_ordered = [p["key"] for p in personas]
    with ThreadPoolExecutor(max_workers=max(1, len(personas))) as ex:
        for key, text in ex.map(_phase1_one, keys_ordered):
            phase1[key] = text

    # ── Phase C: cross-read (optional second pass) ───────────────────────
    revised = dict(phase1)
    if cfg.deep_work.cross_read and len(personas) >= 2:
        on_event({"phase": "crossread"})

        def _phase2_one(key: str) -> tuple[str, str]:
            on_event({"persona_started": key, "phase": "crossread"})
            others = []
            for p in personas:
                if p["key"] == key:
                    continue
                t = (phase1.get(p["key"]) or "").strip()
                if t:
                    others.append(f"### {p['icon']} {p['label']}\n\n{t}")
            others_block = "\n\n---\n\n".join(others) or "(no other takes available)"
            sys_prompt = _PHASE1_SYSTEM_PREAMBLE.format(persona_system=SPECIALISTS[key]["system"])
            user_msg = _CROSSREAD_USER.format(
                your_take    = (phase1.get(key) or "").strip() or "(no prior take)",
                others_block = others_block,
            )
            try:
                text = _call(
                    user_msg, system=sys_prompt, mode=mode,
                    model=cfg.deep_work.specialist_model,
                    cli_bin=cli_bin, timeout=120,
                )
            except Exception:
                print(f"[council] crossread {key} error:\n" + traceback.format_exc(), flush=True)
                text = phase1.get(key, "")
            on_event({"persona_done": key, "phase": "crossread"})
            return (key, text)

        with ThreadPoolExecutor(max_workers=max(1, len(personas))) as ex:
            for key, text in ex.map(_phase2_one, keys_ordered):
                revised[key] = text

    # ── Phase D: synthesis ───────────────────────────────────────────────
    on_event({"phase": "synthesizing"})
    stances = []
    for p in personas:
        t = (revised.get(p["key"]) or "").strip()
        stances.append(f"### {p['icon']} {p['label']}\n\n{t}")
    synth_user = _SYNTHESIS_USER.format(
        query         = query[:2000],
        stances_block = ("\n\n---\n\n".join(stances))[:18000],
    )
    try:
        synth_raw = _call(
            synth_user, system=_SYNTHESIS_SYSTEM, mode=mode,
            model=cfg.deep_work.synthesis_model,
            cli_bin=cli_bin, timeout=180,
        )
    except Exception:
        print("[council] synthesis error:\n" + traceback.format_exc(), flush=True)
        synth_raw = ""

    synth = _extract_json(synth_raw) or {}
    conclusion = (synth.get("conclusion") or "").strip()
    if not conclusion:
        # Last-ditch fallback: surface whatever the synthesiser produced so
        # the user gets *something* rather than an empty bubble.
        conclusion = synth_raw.strip() or "(council produced no synthesizable output)"

    transcript = {
        "query":              query,
        "personas":           personas,
        "routing_rationale":  pick["rationale"],
        "phase1":             phase1,
        "revised":            revised,
        "conclusion":         conclusion,
        "why_this_conclusion":(synth.get("why_this_conclusion") or "").strip(),
        "decision_required":  bool(synth.get("decision_required")),
        "options":            list(synth.get("options") or [])[:3],
        "disagreement_notes": (synth.get("disagreement_notes") or "").strip(),
        "elapsed_seconds":    round(time.time() - started, 1),
        "mode":               mode,
    }
    on_event({"council_done": transcript})
    return transcript


# ─── Daily-cap helper ─────────────────────────────────────────────────────────

def count_today(memory_path) -> int:
    """How many council runs have completed today. Used to enforce
    deep_work.max_per_day. Reads the council log written by the dashboard
    endpoint at MEMORY/sessions/council_log.jsonl."""
    from datetime import date
    from pathlib import Path
    log = Path(memory_path) / "sessions" / "council_log.jsonl"
    if not log.exists():
        return 0
    today = date.today().isoformat()
    n = 0
    try:
        for line in log.read_text(errors="ignore").splitlines():
            if not line.strip():
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            if (rec.get("date") or "")[:10] == today:
                n += 1
    except Exception:
        return 0
    return n


def append_log(memory_path, transcript: dict) -> None:
    """Append one row per completed council run for the daily-cap counter."""
    from datetime import datetime
    from pathlib import Path
    log = Path(memory_path) / "sessions" / "council_log.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "date":            datetime.now().isoformat(timespec="seconds"),
        "personas":        [p.get("key") for p in transcript.get("personas", [])],
        "elapsed_seconds": transcript.get("elapsed_seconds"),
        "decision":        bool(transcript.get("decision_required")),
        "mode":            transcript.get("mode"),
    }
    with log.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row) + "\n")
