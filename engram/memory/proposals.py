"""
Proposal index — durable record of every memory proposal produced by:
  - memory consolidation (nightly)
  - chat session harvesting
  - reconsolidation (retrieved-memory contradicted in a response)
  - manual writes via the chat write_memory tool

Each proposal has a stable UID and one of these statuses:
  pending   — surfaced to the user, awaiting decision
  saved     — applied to the canonical file
  skipped   — explicitly rejected by the user
  superseded — replaced by a newer proposal touching the same path

Lives at MEMORY/proposals/index.json. Append-friendly JSON list (small enough
to load whole; thousands of proposals = ~1 MB).
"""
import json
import os
import time
import uuid
from pathlib import Path
from typing import Optional


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def load_index(path: Path) -> list:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text())
    except Exception:
        return []


def save_index(path: Path, items: list):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(items, indent=2))


def add_proposals(
    path: Path,
    items: list,
    *,
    source: str,
    harvest_filename: Optional[str] = None,
) -> int:
    """
    Add new proposals to the index. Marks any prior pending proposals
    touching the same canonical path as 'superseded'.
    """
    idx = load_index(path)

    # Mark older pending proposals for the same path as superseded
    incoming_paths = {it.get("path") for it in items if it.get("path")}
    for prev in idx:
        if (prev.get("status") == "pending"
            and prev.get("path") in incoming_paths
            and prev.get("source") != source):
            prev["status"] = "superseded"
            prev["superseded_at"] = _now()

    added = 0
    for it in items:
        uid = it.get("uid") or f"prop_{uuid.uuid4().hex[:10]}"
        it["uid"] = uid
        # Don't add if already present (consolidation re-runs)
        if any(p.get("uid") == uid for p in idx):
            continue
        # Compute a quick salience score for ranking
        sal = compute_proposal_salience(it)
        idx.append({
            "uid":       uid,
            "ts":        _now(),
            "path":      it.get("path"),
            "operation": it.get("operation", "update"),
            "reason":    it.get("reason", "")[:300],
            "source":    source,
            "harvest_filename": harvest_filename,
            "salience":  sal,
            "status":    "pending",
        })
        added += 1
    save_index(path, idx)
    return added


def update_status(path: Path, uid: str, new_status: str,
                  *, applied_path: Optional[str] = None) -> bool:
    """Set status for a proposal. Returns True if found."""
    idx = load_index(path)
    for p in idx:
        if p.get("uid") == uid:
            p["status"] = new_status
            p[f"{new_status}_at"] = _now()
            if applied_path:
                p["applied_path"] = applied_path
            save_index(path, idx)
            return True
    return False


def list_pending(path: Path) -> list:
    return [p for p in load_index(path) if p.get("status") == "pending"]


def list_by_status(path: Path, status: str) -> list:
    if status == "all":
        return load_index(path)
    return [p for p in load_index(path) if p.get("status") == status]


def stats(path: Path) -> dict:
    idx = load_index(path)
    out = {"total": len(idx), "pending": 0, "saved": 0, "skipped": 0, "superseded": 0}
    for p in idx:
        s = p.get("status", "pending")
        out[s] = out.get(s, 0) + 1
    return out


# ─── Salience scoring for proposals ───────────────────────────────────────
def compute_proposal_salience(item: dict) -> float:
    """
    Heuristic salience: decisions/* > active deals > general updates > people.
    Range 0.0 - 1.0.
    """
    p = (item.get("path") or "").lower()
    reason = (item.get("reason") or "").lower()

    base = 0.5
    # Wiki = canonical entity records (consolidation target).
    # Match wiki/<topic>/ first so they take precedence over the legacy
    # MEMORY/<topic>/ checks below.
    if "wiki/decisions/" in p:
        base = 0.95
    elif "wiki/projects/" in p:
        base = 0.85
    elif "wiki/people/" in p:
        base = 0.60
    elif "wiki/concepts/" in p or "wiki/systems/" in p:
        base = 0.55
    elif "/decisions/" in p:
        base = 0.95
    elif "/accounts/" in p:
        # Legacy MEMORY/accounts/ path. New writers target wiki/projects or
        # wiki/people; this branch survives for back-compat with existing
        # pending proposals. A per-deployment list of high-priority account
        # name fragments can boost specific deals (ENGRAM_PARTNER_KEYS env
        # var, comma-separated). Empty default = no boost.
        priority = [k.strip().lower() for k in
                    (os.environ.get("ENGRAM_PARTNER_KEYS", "") or "").split(",")
                    if k.strip()]
        if priority and any(k in p for k in priority):
            base = 0.85
        else:
            base = 0.70
    elif "/crystallised/" in p:
        base = 0.95
    elif "/context/people" in p:
        base = 0.55
    elif "/context/" in p:
        base = 0.60
    elif "/weekly/" in p:
        base = 0.55

    # Boost if reason mentions decisions, deadline, blocker, risk
    if any(k in reason for k in ("decision", "deadline", "blocker", "risk", "approved")):
        base = min(1.0, base + 0.10)

    return round(base, 3)


def sort_by_salience(items: list) -> list:
    """Return items sorted by salience desc, with stable secondary sort on path."""
    def key(item):
        sal = compute_proposal_salience(item)
        return (-sal, item.get("path", ""))
    return sorted(items, key=key)
