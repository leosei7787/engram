"""
Deep Work mode — multi-agent reasoning team.

A team lead routes the question to 2-3 specialist advisors, runs them in
parallel, and synthesises a unified recommendation. Each advisor has a
distinct lens (financial, commercial, contrarian, HR, marketing, strategy).

Quick chat (single Claude call) is still the default. Deep Work is opt-in
per message via a mode flag.

Cost: ~10× a quick chat (1 routing + 2-3 specialists + 1 synthesis).
Latency: ~30-60s vs ~10-15s for quick chat.
"""
import json
import re
import threading
import time
import queue
from typing import Callable, Optional


# ─── Specialist personas ──────────────────────────────────────────────────
SPECIALISTS = {
    "contrarian": {
        "icon":   "😈",
        "label":  "Contrarian",
        "color":  "#ef4444",
        "short":  "Devil's advocate — challenges assumptions",
        "system": (
            "You are the Contrarian on the advisory team. Your job: "
            "challenge the framing. What's the strongest argument AGAINST the "
            "implicit assumption in the question? What is the user missing? What "
            "second-order effect or downside is the current framing ignoring? "
            "Be brief, sharp, falsifiable. No preamble. 4-8 bullets MAX. "
            "Cite specific facts from the loaded memory context if you can. "
            "If you genuinely think the proposed direction is correct, say so "
            "in one line and then surface the strongest residual risk."
        ),
    },
    "cfo": {
        "icon":   "💰",
        "label":  "CFO",
        "color":  "#10b981",
        "short":  "Financial — P&L, ROI, capex/opex, unit economics",
        "system": (
            "You are the CFO advisor. Read every question through a P&L / "
            "cash / unit-economics lens. Cite actual financials from the "
            "loaded context where relevant. Quantify wherever possible. "
            "When data is missing, say what you'd need to estimate it. "
            "4-8 bullets MAX. Direct, no preamble. Lead with the financial "
            "verdict, then the math."
        ),
    },
    "commercial": {
        "icon":   "🤝",
        "label":  "Commercial",
        "color":  "#3b82f6",
        "short":  "Customer-facing — deal motion, win/loss, account strategy",
        "system": (
            "You are the Commercial advisor. Frame every question through "
            "customer / deal motion: who is the economic buyer vs technical "
            "user? What's the win/loss dynamic vs competitors? Which active "
            "deals does this affect? Cite the loaded account context and "
            "pipeline. 4-8 bullets MAX. Direct, no preamble."
        ),
    },
    "marketing": {
        "icon":   "📣",
        "label":  "Marketing",
        "color":  "#a855f7",
        "short":  "Brand & GTM — positioning, narrative, launch plan",
        "system": (
            "You are the Marketing & Brand advisor. How does this play in "
            "the organization's brand narrative? What's the positioning vs "
            "competitors? What's the GTM sequence — internal first, partners, "
            "public? What's the proof point that earns trust? 4-8 bullets MAX. "
            "Direct, no preamble."
        ),
    },
    "hr": {
        "icon":   "🧑‍🤝‍🧑",
        "label":  "People & Org",
        "color":  "#f59e0b",
        "short":  "Talent & org — capacity, compensation, attrition risk",
        "system": (
            "You are the People & Org advisor. What's the talent / capacity / "
            "motivation angle here? Who actually executes this? Are they "
            "motivated and capable? What's the attrition or hiring risk? "
            "Comp / role-design implications? Cite specific people from the "
            "loaded context where relevant. 4-8 bullets MAX. Direct, no "
            "preamble."
        ),
    },
    "strategy": {
        "icon":   "🎯",
        "label":  "Strategy",
        "color":  "#8b5cf6",
        "short":  "Long-horizon — portfolio, optionality, second-order effects",
        "system": (
            "You are the Strategy advisor. Pull back to the 3-5 year horizon. "
            "How does this compound? What option does it preserve or close? "
            "What's the second-order effect on the portfolio? Apply the "
            "revenue-filter / payback-period lens where relevant. Test against "
            "the core strategic thesis from the loaded context. 4-8 bullets "
            "MAX. Direct, no preamble."
        ),
    },
    "engineering": {
        "icon":   "⚙️",
        "label":  "Engineering",
        "color":  "#0891b2",
        "short":  "Technical — feasibility, build vs buy, debt, scale",
        "system": (
            "You are the Engineering advisor. Tech feasibility, build vs buy, "
            "dependencies, scaling, debt. What has already been built (from "
            "loaded context)? What changes about the platform if we say yes? "
            "What engineering load does this put on which team? 4-8 bullets "
            "MAX. Direct, no preamble."
        ),
    },
}


# ─── Team Lead — routing prompt ───────────────────────────────────────────
_LEAD_ROUTING_PROMPT = """You are the Team Lead on an advisory team.
The user just asked a question. Pick 2-3 specialist advisors from this team to
weigh in. Avoid redundancy (e.g., don't pick both Strategy and Commercial
for a pure deal-motion question). The Contrarian should be picked unless
the question is purely informational.

Available specialists:
{roster}

User's question:
{query}

Output strict JSON, nothing else:
{{
  "specialists": ["<key1>", "<key2>", "<key3>"],
  "rationale":   "one short sentence on why these"
}}

Rules:
- Pick 2 OR 3 (not 1, not 4+)
- Use only keys from the roster above (lowercase, exact)
- Keep rationale to a single line
"""


def _format_roster(allowed: dict) -> str:
    return "\n".join(
        f"- {key}: {info['short']}"
        for key, info in allowed.items()
    )


def pick_specialists(
    query: str,
    *,
    claude_complete: Callable,
    available: Optional[dict] = None,
    force: Optional[list] = None,
) -> dict:
    """
    Returns {specialists: [...], rationale: "..."}.
    Uses Claude Haiku for the routing decision (cheap + fast).
    """
    if force:
        return {"specialists": force[:3], "rationale": "user-forced selection"}

    available = available or SPECIALISTS
    roster = _format_roster(available)
    prompt = _LEAD_ROUTING_PROMPT.format(roster=roster, query=query[:1500])

    raw = ""
    try:
        raw = claude_complete(prompt) or ""
    except Exception as e:
        print(f"[deep] routing error: {e}", flush=True)

    # Best-effort JSON extraction
    m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return _fallback_pick(query, available)
    try:
        parsed = json.loads(m.group())
    except Exception:
        return _fallback_pick(query, available)

    # Validate keys against available roster
    picks = [s for s in (parsed.get("specialists") or []) if s in available]
    if not picks:
        return _fallback_pick(query, available)
    return {
        "specialists": picks[:3],
        "rationale":   (parsed.get("rationale") or "")[:240],
    }


def _fallback_pick(query: str, available: dict) -> dict:
    """Heuristic fallback if routing fails."""
    q = (query or "").lower()
    picks = []
    if any(k in q for k in ("cost", "budget", "roi", "revenue", "margin",
                             "p&l", "capex", "investment")):
        picks.append("cfo")
    if any(k in q for k in ("customer", "deal", "account", "partner", "win",
                             "loss", "sourcing", "contract", "pipeline")):
        picks.append("commercial")
    if any(k in q for k in ("brand", "narrative", "positioning", "launch",
                             "marketing", "gtm", "campaign")):
        picks.append("marketing")
    if any(k in q for k in ("people", "team", "headcount", "hire", "attrition",
                             "comp", "perf")):
        picks.append("hr")
    if any(k in q for k in ("portfolio", "100m", "filter", "long term",
                             "second order", "option", "thesis")):
        picks.append("strategy")
    # Default for anything else
    if not picks:
        picks = ["strategy", "commercial"]
    # Always include Contrarian unless question is pure info
    if "contrarian" not in picks and not any(k in q for k in (
            "what is", "tell me about", "summary", "summarise", "explain")):
        picks.insert(0, "contrarian")
    return {"specialists": picks[:3], "rationale": "heuristic fallback"}


# ─── Specialist runner — parallel ─────────────────────────────────────────
def run_specialist_streamed(
    *,
    role_key: str,
    question: str,
    base_system: str,
    history: list,
    claude_stream: Callable,
    out_queue: "queue.Queue",
    model: str = "claude-haiku-4-5-20251001",
):
    """
    Run one specialist in a thread. Streams tokens onto out_queue as
    {"specialist": role_key, "text": <token>} events.
    On finish, emits {"specialist_done": role_key, "duration_s": float}.
    """
    role = SPECIALISTS.get(role_key)
    if not role:
        out_queue.put({"specialist": role_key, "error": "unknown role"})
        return

    # Compose the role's system prompt: base context + role persona
    role_system = base_system + "\n\n---\n## YOUR ROLE\n" + role["system"]

    t0 = time.time()
    full = ""
    try:
        with claude_stream(
            model=model,
            max_tokens=2048,
            system=role_system,
            messages=history + [{"role": "user", "content": question}],
            _cost_operation=f"deep_{role_key}",
        ) as stream:
            for event in stream:
                etype = getattr(event, "type", "")
                if etype == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if delta and getattr(delta, "type", "") == "text_delta":
                        full += delta.text
                        out_queue.put({"specialist": role_key, "text": delta.text})
        out_queue.put({
            "specialist_done": role_key,
            "duration_s": round(time.time() - t0, 1),
            "char_count":  len(full),
        })
    except Exception as e:
        out_queue.put({"specialist": role_key, "error": str(e)})


def format_perspectives_for_synthesis(perspectives: dict) -> str:
    """Render the team's responses as a single text block for the synthesiser."""
    blocks = []
    for role_key, text in perspectives.items():
        role = SPECIALISTS.get(role_key, {})
        blocks.append(
            f"### {role.get('icon','·')} {role.get('label', role_key)}\n\n{text.strip()}"
        )
    return "\n\n---\n\n".join(blocks)


# ─── Team Lead — synthesis prompt ─────────────────────────────────────────
_LEAD_SYNTHESIS_PROMPT = """You are the Team Lead synthesising your advisors' takes.

User's question:
{query}

Each advisor's take:
{perspectives}

Write a synthesis. Be direct:
- Lead with the decision / trade-off, not the background
- Short in operational contexts, structured in strategic ones
- No filler phrases: "touch base", "circle back", "synergy", "let's align"

Structure:
1. ONE-LINE verdict at the top (the recommendation or the trade-off)
2. **Where the team agrees:** 2-3 bullets
3. **Where they genuinely disagree** (don't paper over): 1-3 bullets
4. **Recommendation / next action(s):** terse, actionable

Keep the whole thing under 250 words. No filler. If the advisors did not
provide enough information to land a recommendation, say so explicitly and
list what you'd need."""


def build_synthesis_prompt(query: str, perspectives: dict) -> str:
    return _LEAD_SYNTHESIS_PROMPT.format(
        query=query[:1500],
        perspectives=format_perspectives_for_synthesis(perspectives),
    )
