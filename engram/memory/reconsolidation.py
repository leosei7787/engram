"""
Reconsolidation — when retrieved memory is contradicted (or enriched) by a
chat response, generate a memory-update proposal automatically.

Mirrors the brain's reconsolidation step: every time you recall a memory,
it becomes plastic and can be updated. We mirror this for high-stakes facts.

Flow (runs as a background task after each chat response):
  1. Take the activated entities from the response's context
  2. Take the assistant's reply text
  3. Run a fast Claude haiku check: "Does the reply state any new fact that
     conflicts with or enriches what's in memory about these entities?"
  4. If yes, produce a proposal in the harvest queue + proposal index
"""
import json
import time
from pathlib import Path
from typing import Callable, Optional


RECONSOLIDATION_PROMPT = """You are reviewing whether a user's message contains
factual updates that should be saved to memory.

CRITICAL RULE: Only extract facts that the USER stated in their message.
The assistant's reply is inference — do NOT treat it as a source of truth.

Activated entities (currently in memory):
{entities}

User's message:
{user_query}

Task: Identify ANY clear, factual update where THE USER'S MESSAGE either:
  (a) Contradicts a known fact about an entity (e.g., a role change, status
      change, deadline change, ownership change)
  (b) States a substantial new fact not currently in memory

For each finding, return:
  - entity:    the entity name being updated
  - kind:      "contradiction" | "enrichment"
  - summary:   one-line description of the update
  - confidence: 0.0–1.0

Output strict JSON:
{{"findings": [
  {{"entity": "...", "kind": "...", "summary": "...", "confidence": 0.85}}
]}}

If the user's message contains nothing factually new, return: {{"findings": []}}.

Output ONLY the JSON object."""


def detect_reconsolidation(
    user_query: str,
    assistant_reply: str,
    activated_nodes: dict,
    entities: dict,
    *,
    claude_complete: Callable,
    max_entities_in_prompt: int = 10,
) -> list:
    """
    Run a haiku-class reconsolidation check.
    Returns list of findings dicts.
    """
    if not activated_nodes or not assistant_reply or len(assistant_reply) < 100:
        return []

    # Pick top activated entities by strength
    top = sorted(activated_nodes.items(), key=lambda x: -x[1])[:max_entities_in_prompt]
    ent_lines = []
    for eid, _strength in top:
        ent = entities.get(eid)
        if not ent: continue
        line = f"- {ent.get('name','?')} ({ent.get('type','?')})"
        if ent.get("description"):
            line += f": {ent['description'][:200]}"
        ent_lines.append(line)
    if not ent_lines:
        return []

    prompt = RECONSOLIDATION_PROMPT.format(
        entities="\n".join(ent_lines),
        user_query=user_query[:1000],
    )

    try:
        raw = claude_complete(prompt)
    except Exception as e:
        print(f"[reconsolidation] llm error: {e}", flush=True)
        return []

    # Parse JSON
    import re
    m = re.search(r'\{[^{}]*"findings"[^{}]*\[.*?\]\s*\}', raw, re.DOTALL)
    if not m:
        m = re.search(r'\{.*\}', raw, re.DOTALL)
    if not m:
        return []
    try:
        parsed = json.loads(m.group())
        return parsed.get("findings", []) or []
    except Exception:
        return []


def findings_to_proposal(
    findings: list,
    *,
    user_query: str,
    session_id: str,
    activated_nodes: dict,
    entities: dict,
) -> Optional[dict]:
    """
    Build a single harvest proposal from a list of reconsolidation findings.

    The proposal targets MEMORY/episodic/reconsolidation_log.md so the user
    can review and decide whether to update canonical files.
    """
    if not findings:
        return None

    today = time.strftime("%Y-%m-%d")
    lines = [f"# Reconsolidation log\n", f"_Updated {today}_", ""]
    lines.append(f"## Session {session_id} — \"{user_query[:80]}\"\n")

    high_priority = []
    for f in findings:
        kind_emoji = "⚡" if f.get("kind") == "contradiction" else "➕"
        ent = f.get("entity", "?")
        summary = f.get("summary", "")
        conf = float(f.get("confidence", 0.5))
        lines.append(f"- {kind_emoji} **{ent}** — {summary} _(conf {conf:.2f})_")
        if conf >= 0.7 and f.get("kind") == "contradiction":
            high_priority.append(f)

    content = "\n".join(lines)
    reason_kind = "contradicts" if high_priority else "enriches"
    return {
        "path":      "MEMORY/episodic/reconsolidation_log.md",
        "operation": "append",
        "reason":    f"Reconsolidation: chat response {reason_kind} memory ({len(findings)} update[s])",
        "content":   content,
        "source":    "reconsolidation",
        "_findings": findings,
    }


def run_reconsolidation_pipeline(
    *,
    user_query: str,
    assistant_reply: str,
    activated_nodes: dict,
    entities: dict,
    session_id: str,
    claude_complete: Callable,
    sessions_dir: Path,
    proposal_index_path: Path,
    proposal_module,            # passes v3.proposals to avoid circular import
) -> dict:
    """
    Full pipeline. Returns stats dict {findings, proposal_uid, harvest_filename}.
    """
    findings = detect_reconsolidation(
        user_query, assistant_reply, activated_nodes, entities,
        claude_complete=claude_complete,
    )
    if not findings:
        return {"findings": 0}

    proposal = findings_to_proposal(
        findings,
        user_query=user_query,
        session_id=session_id,
        activated_nodes=activated_nodes,
        entities=entities,
    )
    if not proposal:
        return {"findings": len(findings), "proposal_built": False}

    # Save into a harvest file (so it surfaces in the existing harvest banner)
    import uuid as _uuid
    proposal["uid"] = f"prop_{_uuid.uuid4().hex[:10]}"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    harvest_path = sessions_dir / f"harvest_reconsolidation_{time.strftime('%Y-%m-%d_%H%M%S')}.json"
    harvest_path.write_text(json.dumps({
        "ts":     time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "reconsolidation",
        "items":  [proposal],
    }, indent=2))

    # Index the proposal
    proposal_module.add_proposals(
        proposal_index_path, [proposal],
        source="reconsolidation",
        harvest_filename=harvest_path.name,
    )

    return {
        "findings":         len(findings),
        "proposal_uid":     proposal["uid"],
        "harvest_filename": harvest_path.name,
    }
