"""
Open Questions — first-class graph nodes representing unresolved threads.

Extracted from conversation harvesting and document ingestion. Surfaced
proactively when new content connects to them.
"""
import json
import re
import uuid
from pathlib import Path
from .schemas import OpenQuestion, _now


# Patterns that suggest an open question
OPEN_Q_PATTERNS = [
    r"\bwe don'?t know\b",
    r"\bopen question\b",
    r"\bunclear\b",
    r"\btbd\b",
    r"\bto be (?:determined|defined|decided)\b",
    r"\bneed to (?:check|figure out|decide|confirm)\b",
    r"\bunresolved\b",
    r"\bnot yet (?:decided|known|confirmed)\b",
    r"\bpending\b.*\?",
    r"\?\s*$",
]

# Question keywords used for entity linking
QUESTION_KEYWORDS = [
    "when", "where", "who", "what", "how", "why", "which", "should", "will",
]


def extract_open_questions(text: str, source: str = "") -> list:
    """
    Heuristic extraction. Splits on sentences, returns those matching patterns.
    Returns list of OpenQuestion dicts (not yet linked to entities).
    """
    out = []
    if not text:
        return out

    # Split into sentences
    sentences = re.split(r'(?<=[.!?])\s+', text)
    for s in sentences:
        s = s.strip()
        if len(s) < 15 or len(s) > 400:
            continue
        s_lower = s.lower()
        matches = any(re.search(p, s_lower) for p in OPEN_Q_PATTERNS)
        if not matches:
            continue
        # Avoid past-tense / closed statements
        if re.search(r"\b(decided|resolved|answered|done|closed)\b", s_lower):
            continue
        oq = OpenQuestion(
            id=f"oq_{uuid.uuid4().hex[:10]}",
            text=s,
            created_from=source,
        )
        out.append(oq.to_dict())
    return out


def link_to_entities(open_q: dict, entities: dict) -> dict:
    """Match question text against entity names; link top matches."""
    text_lower = open_q["text"].lower()
    linked = []
    for eid, ent in entities.items():
        name = ent.get("name", "")
        if not name or len(name) < 3:
            continue
        if name.lower() in text_lower:
            linked.append(eid)
    open_q["linked_entities"] = linked[:8]
    return open_q


def infer_priority(open_q: dict, entities: dict) -> str:
    """Priority based on linked entity tiers + question content."""
    text = open_q["text"].lower()
    if any(k in text for k in ("decision", "deadline", "blocker", "risk", "board", "ceo")):
        return "high"

    # Touches crystallised/active entities
    for eid in open_q.get("linked_entities", []):
        ent = entities.get(eid, {})
        if ent.get("tier") == "crystallised":
            return "high"
    if any(entities.get(eid, {}).get("type", "").lower() in ("deal", "decision")
           for eid in open_q.get("linked_entities", [])):
        return "high"
    return "medium"


# ─── Proactive surfacing ──────────────────────────────────────────────────
def proactive_surface(
    new_entity_ids: set,
    open_questions: list,
    *,
    relevance_threshold: float = 0.4,
) -> list:
    """
    Given the entities extracted from a newly-ingested doc, return open
    questions whose linked_entities overlap with them.
    """
    surfaces = []
    for q in open_questions:
        if q.get("status") != "open":
            continue
        linked = set(q.get("linked_entities", []) or [])
        if not linked:
            continue
        overlap = linked & new_entity_ids
        if not overlap:
            continue
        relevance = len(overlap) / max(len(linked), 1)
        if relevance >= relevance_threshold:
            surfaces.append({
                "question_id": q["id"],
                "text": q["text"],
                "priority": q.get("priority", "medium"),
                "overlap_entities": sorted(overlap),
                "relevance": round(relevance, 2),
                "may_answer": relevance > 0.7,
            })
    return sorted(surfaces, key=lambda x: -x["relevance"])


# ─── Persistence ──────────────────────────────────────────────────────────
def load_open_questions(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def save_open_questions(path: Path, items: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2))


def add_open_questions(path: Path, new_items: list, entities: dict) -> int:
    """Add new questions, link to entities, dedup by text. Returns count added."""
    existing = load_open_questions(path)
    seen = {q.get("text", "").lower() for q in existing}
    added = 0
    for q in new_items:
        text_norm = q.get("text", "").lower()
        if text_norm in seen:
            continue
        link_to_entities(q, entities)
        q["priority"] = infer_priority(q, entities)
        existing.append(q)
        seen.add(text_norm)
        added += 1
    save_open_questions(path, existing)
    return added


def mark_stale(path: Path, max_age_days: int = 30) -> int:
    """Mark questions as 'stale' if open and older than max_age_days."""
    import time
    items = load_open_questions(path)
    now_t = time.time()
    n = 0
    for q in items:
        if q.get("status") != "open":
            continue
        try:
            t = time.mktime(time.strptime(q.get("created_at", "")[:10], "%Y-%m-%d"))
        except Exception:
            continue
        if now_t - t > max_age_days * 86400:
            q["status"] = "stale"
            n += 1
    save_open_questions(path, items)
    return n


def mark_answered(path: Path, question_id: str, answered_by: str) -> bool:
    items = load_open_questions(path)
    for q in items:
        if q.get("id") == question_id:
            q["status"] = "answered"
            q["answered_by"] = answered_by
            q["answered_at"] = _now()
            save_open_questions(path, items)
            return True
    return False


def list_by_status(path: Path, status: str = "open") -> list:
    return [q for q in load_open_questions(path) if q.get("status") == status]
