"""
engram.memory.calendar_extractor — AI-driven extraction of meeting signal
==========================================================================

Replaces the keyword-scored ``_classify_event`` panel on the Top of Mind
tab. Walks events from the most-recent ICS file, calls Haiku with the
event details + the user's name, and emits structured records like:

  {
    "uid":              "<ics-uid-or-fallback>",
    "iso":              "2026-05-15",
    "time":             "11:00–12:00",
    "summary":          "Maps Product Leadership - Weekly call",
    "location":         "MR-NL-AMS-3.10 Nile 10p",
    "organizer":        "Alice Chen",
    "attendees":        ["Alice Chen", "Bob Smith", "Carol Davis"],
    "is_high_stakes":   true,
    "kind":             "decision_review" | "exec_forum" | "1on1" | "deep_work" | "social" | "travel" | "personal",
    "recurrence":       "weekly" | "monthly" | "ad_hoc" | "one_off",
    "is_cancelled":     false,
    "why":              ["leadership", "weekly", "decision-grade"],
    "action_required":  "review the maps roadmap deck before Mon",
    "account_links":    ["acmemotors", "acmecorp"],
    "people_links":     ["alice_chen", "bob_smith", "carol_davis"],
    "urgency":          "high" | "medium" | "low",
    "confidence":       0.0-1.0,
  }

Cancelled events are dropped before persisting. Personal items (drop-offs,
lunches, OOO) are not surfaced. Output: MEMORY/signals/calendar.json.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from engram.ingest.ics import parse_ics, upcoming as ics_upcoming, Event


# ─── Output file ──────────────────────────────────────────────────────────────

SIGNALS_FILE = "signals/calendar.json"


def signals_path(memory_path: Path) -> Path:
    return memory_path / SIGNALS_FILE


def load_signals(memory_path: Path) -> Optional[dict]:
    p = signals_path(memory_path)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def save_signals(signals: dict, memory_path: Path) -> Path:
    out = signals_path(memory_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(signals, indent=2))
    return out


def signals_age_seconds(memory_path: Path) -> Optional[float]:
    p = signals_path(memory_path)
    if not p.exists():
        return None
    try:
        return time.time() - p.stat().st_mtime
    except Exception:
        return None


# ─── Recurrence detection ─────────────────────────────────────────────────────

def _recurrence_kind(rrule: str) -> str:
    """Boil the RRULE down to a coarse bucket the rest of the system uses."""
    if not rrule:
        return "one_off"
    r = rrule.upper()
    if "FREQ=DAILY" in r:
        return "daily"
    if "FREQ=WEEKLY" in r:
        return "weekly"
    if "FREQ=MONTHLY" in r:
        return "monthly"
    if "FREQ=YEARLY" in r:
        return "yearly"
    return "ad_hoc"


# ─── Slugify (matches session_harvester's helper) ─────────────────────────────

def _slug(text: str) -> str:
    return re.sub(r"[^\w\-]", "_", (text or "").lower()).strip("_")[:60]


# ─── Per-event prompt ─────────────────────────────────────────────────────────

_EXTRACT_PROMPT = """You are extracting durable meeting signal for {user_name}'s calendar dashboard.

You will be shown ONE upcoming calendar event. Decide whether it matters, why, and what context the AI should pre-load when {user_name} preps for it.

Output STRICT JSON ONLY (no prose, no fences):
{{
  "is_high_stakes":   true|false,
  "kind":             "decision_review"|"exec_forum"|"1on1"|"deep_work"|"social"|"travel"|"personal",
  "why":              ["short tag 1", "short tag 2", ...],   // 1-4 short tags explaining the surfacing
  "action_required":  "<one short sentence; '' if none>",
  "account_links":    ["<account name>", ...],               // any companies / customers referenced
  "urgency":          "high"|"medium"|"low",
  "confidence":       0.0-1.0
}}

Rules:
- "high_stakes" = decision-grade, exec forum, board / steering, customer ask, strategic review, high-stakes interview, kickoff. NOT every meeting on the calendar.
- "personal" = drop-offs, doctor, school, gym, OOO, family. Always low urgency, never high stakes.
- "1on1" = bilateral (could be high-stakes if with skip-level / report).
- "deep_work" = blocked focus time, prep block.
- Mention specific account names ONLY if the event clearly involves an external customer/partner.
- Keep tags short (one or two words each).

Event details:
  When:        {when}
  Duration:    {duration_min} min{recurrence_hint}
  Summary:     {summary}
  Location:    {location}
  Organizer:   {organizer}
  Attendees:   {attendees}
  Description: {description}

JSON:"""


def _format_when(e: Event) -> str:
    local      = e.start.astimezone()
    local_end  = e.end.astimezone()
    if e.all_day:
        return local.strftime("%a %d %b %Y") + " (all-day)"
    return local.strftime("%a %d %b %Y %H:%M") + "–" + local_end.strftime("%H:%M")


def _haiku_call(prompt: str, cfg, *, max_tokens: int = 350, timeout: int = 18) -> str:
    """Invoke Haiku via SDK if API key is set, else fall back to claude CLI."""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if api_key:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model      = cfg.models.haiku,
            max_tokens = max_tokens,
            messages   = [{"role": "user", "content": prompt}],
        )
        out = ""
        for block in (resp.content or []):
            if getattr(block, "type", "") == "text":
                out += getattr(block, "text", "")
        return out
    import shutil as _shutil
    cli_bin = (getattr(getattr(cfg, "chat", None), "cli_bin", None) or None) \
              or _shutil.which("claude")
    if not cli_bin:
        return ""
    proc = subprocess.run(
        [cli_bin, "-p", prompt, "--output-format", "text", "--model", cfg.models.haiku],
        capture_output=True, text=True, timeout=timeout,
    )
    return (proc.stdout or "").strip()


# ─── Per-event extraction ─────────────────────────────────────────────────────

def extract_event_signal(event: Event, *, user_name: str, cfg) -> Optional[dict]:
    """Run Haiku over one event. Returns the raw classification dict or None
    on parse failure / no signal."""
    if event.is_cancelled:
        return None

    duration_min = max(1, int((event.end - event.start).total_seconds() / 60))
    rec_hint = ""
    if event.is_recurring:
        rec_hint = f"  (recurring {_recurrence_kind(event.rrule)})"

    prompt = _EXTRACT_PROMPT.format(
        user_name    = user_name or "the user",
        when         = _format_when(event),
        duration_min = duration_min,
        recurrence_hint = rec_hint,
        summary      = (event.summary or "")[:200],
        location     = (event.location or "")[:140] or "—",
        organizer    = (event.organizer or "")[:140] or "—",
        attendees    = ", ".join((event.attendees or [])[:20]) or "—",
        description  = (event.description or "")[:600] or "—",
    )

    try:
        text = _haiku_call(prompt, cfg)
    except Exception:
        print("[calendar] LLM error:\n" + traceback.format_exc(), flush=True)
        return None

    cleaned = re.sub(r"```(?:json)?", "", text).strip().strip("`").strip()
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ─── Build display record from raw signal ─────────────────────────────────────

def _build_record(event: Event, classification: dict) -> dict:
    local      = event.start.astimezone()
    local_end  = event.end.astimezone()
    if event.all_day:
        time_str = "all-day"
    else:
        time_str = local.strftime("%H:%M") + "–" + local_end.strftime("%H:%M")

    # Resolve attendee + organizer name → slug for graph link follow-ups
    people = set()
    for name in (event.attendees or []):
        s = _slug(name)
        if s:
            people.add(s)
    if event.organizer:
        s = _slug(event.organizer.split("@", 1)[0])
        if s:
            people.add(s)

    # Classifier may emit account names; slug them for downstream lookup
    account_links = []
    for a in (classification.get("account_links") or []):
        if isinstance(a, str) and a.strip():
            account_links.append(_slug(a))

    why = [str(t).strip() for t in (classification.get("why") or []) if str(t).strip()]

    return {
        "uid":             event.uid or _slug(f"{event.summary}-{event.start.isoformat()}"),
        "date":            local.strftime("%a %d %b"),
        "iso":             local.strftime("%Y-%m-%d"),
        "time":            time_str,
        "summary":         (event.summary or "")[:140],
        "location":        ("" if event.location and ("Microsoft Teams" in event.location or "Skype" in event.location)
                            else (event.location or "")[:60]),
        "organizer":       (event.organizer.split("@")[0] if event.organizer and "@" in event.organizer else (event.organizer or "")),
        "attendees":       (event.attendees or [])[:20],
        "is_high_stakes":  bool(classification.get("is_high_stakes")),
        "kind":            classification.get("kind", "ad_hoc"),
        "recurrence":      _recurrence_kind(event.rrule),
        "is_recurring":    event.is_recurring,
        "is_cancelled":    event.is_cancelled,
        "why":             why[:5],
        "action_required": (classification.get("action_required") or "")[:200],
        "account_links":   sorted(set(account_links))[:10],
        "people_links":    sorted(people)[:20],
        "urgency":         classification.get("urgency", "medium"),
        "confidence":      float(classification.get("confidence") or 0.5),
    }


# ─── Top-level batch entrypoint ───────────────────────────────────────────────

def extract_calendar_signals(
    *,
    ics_path:    Path,
    user_name:   str,
    cfg,
    days_ahead:  int = 14,
    max_events:  int = 60,
) -> dict:
    """Parse the ICS, run per-event Haiku extraction, return the full signal
    dict ready to ``save_signals``. Cancelled and personal events are dropped
    from the surfaced list."""
    started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if not ics_path or not Path(ics_path).exists():
        return {
            "extracted_at": started_at,
            "events":       [],
            "scanned":      0,
            "high_stakes":  0,
            "skipped":      {"cancelled": 0, "personal": 0, "errors": 0},
            "ics_path":     str(ics_path) if ics_path else "",
        }

    events_all = parse_ics(ics_path)
    upc        = ics_upcoming(events_all, days_ahead=days_ahead)[:max_events]

    out: list = []
    counts = {"cancelled": 0, "personal": 0, "errors": 0}
    high_stakes = 0

    for e in upc:
        if e.is_cancelled:
            counts["cancelled"] += 1
            continue
        cls = extract_event_signal(e, user_name=user_name, cfg=cfg)
        if cls is None:
            counts["errors"] += 1
            continue
        if (cls.get("kind") or "") == "personal":
            counts["personal"] += 1
            continue
        rec = _build_record(e, cls)
        if rec.get("is_high_stakes"):
            high_stakes += 1
        out.append(rec)

    # Sort: high-stakes first within urgency tier, then chronologically
    urg_rank = {"high": 0, "medium": 1, "low": 2}
    out.sort(key=lambda r: r["iso"])
    out.sort(key=lambda r: (
        0 if r["is_high_stakes"] else 1,
        urg_rank.get(r["urgency"], 1),
    ))

    return {
        "extracted_at": started_at,
        "ics_path":     str(ics_path),
        "scanned":      len(upc),
        "high_stakes":  high_stakes,
        "skipped":      counts,
        "events":       out,
    }


# ─── Past-occurrence finder (Stage 3) ─────────────────────────────────────────
# When a recurring meeting series surfaces, the curator should pre-load notes
# from prior occurrences. We do that by populating the meeting entity's
# `sources` list with paths to past transcripts / session logs / notes that
# reference the meeting series. The curator's spread-activation already
# includes entity sources as candidates, so no curator changes needed.

_PAST_OCCURRENCE_DIRS = (
    "daily/transcripts",
    "sessions",
    "daily/notes",
    "daily/documents",
)


def _stem_tokens(s: str) -> set[str]:
    """Coarse tokenisation used for matching meeting summaries to past
    occurrence files. Drops obvious filler words."""
    if not s:
        return set()
    skip = {
        "the", "and", "for", "with", "from", "into", "call", "meeting",
        "weekly", "monthly", "biweekly", "review", "sync",
    }
    out: set = set()
    for tok in re.findall(r"[a-z][a-z0-9]+", s.lower()):
        if len(tok) >= 3 and tok not in skip:
            out.add(tok)
    return out


def _find_past_occurrences(memory_path: Path, summary: str, *, limit: int = 4,
                           days_back: int = 90) -> list[str]:
    """Find recent files whose name or first ~500 chars reference the
    meeting summary. Returns relative paths (relative to memory_path's
    parent so the curator's path joins resolve correctly). At most
    ``limit`` files, sorted newest first.
    """
    needle = _stem_tokens(summary)
    if not needle:
        return []
    cutoff = time.time() - days_back * 86400
    base   = memory_path.parent           # base_path; sources are stored relative to it
    hits: list[tuple[float, str, int]] = []   # (mtime, rel_path, score)

    for sub in _PAST_OCCURRENCE_DIRS:
        d = memory_path / sub
        if not d.exists():
            continue
        try:
            for f in d.rglob("*.md"):
                try:
                    mt = f.stat().st_mtime
                    if mt < cutoff:
                        continue
                    if "_processed" in f.parts or any(p.startswith("_") for p in f.parts):
                        # Skip backups / archives
                        pass
                except Exception:
                    continue
                # Score: filename-stem token overlap is cheap and effective for
                # summary-named files (e.g. "Maps Product Leadership Weekly call.md").
                stem_tokens = _stem_tokens(f.stem.replace("_", " "))
                score = len(needle & stem_tokens)
                if score == 0:
                    # Fall back to first ~600 chars of content (cheap-ish)
                    try:
                        head = f.read_text(errors="ignore")[:600].lower()
                    except Exception:
                        continue
                    body_score = sum(1 for tok in needle if tok in head)
                    if body_score < max(2, len(needle) // 2):
                        continue
                    score = body_score
                try:
                    rel = str(f.relative_to(base))
                except ValueError:
                    rel = str(f)
                hits.append((mt, rel, score))
        except Exception:
            continue

    # Best score first, then newest
    hits.sort(key=lambda x: (-x[2], -x[0]))
    return [rel for (_, rel, _) in hits[:limit]]


# ─── Graph integration (Stage 2) ──────────────────────────────────────────────
# Writes meeting entities + attended_by / about_account edges to MEMORY/
# graph.json so the curator's spread-activation surfaces meeting context
# automatically when the user asks about a person or account.

_graph_lock = threading.Lock()


def _load_graph(memory_path: Path) -> dict:
    p = memory_path / "graph.json"
    if not p.exists():
        return {"entities": {}, "edges": []}
    try:
        g = json.loads(p.read_text())
        g.setdefault("entities", {})
        g.setdefault("edges", [])
        return g
    except Exception:
        return {"entities": {}, "edges": []}


def _save_graph(memory_path: Path, g: dict) -> None:
    p = memory_path / "graph.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    g["updated"] = datetime.now().isoformat(timespec="seconds")
    p.write_text(json.dumps(g, indent=2))


def _build_lookups(graph: dict) -> tuple[dict, dict]:
    """Return ({lowercase_token → person_id}, {lowercase_token → account_id})
    for fuzzy resolution of attendees and account_links."""
    people:   dict = {}
    accounts: dict = {}
    for eid, ent in (graph.get("entities") or {}).items():
        name = (ent.get("name") or "").strip()
        if not name:
            continue
        t = ent.get("type", "")
        target = people if t == "person" else accounts if t in ("account", "company") else None
        if target is None:
            continue
        target.setdefault(name.lower(), eid)
        for tok in name.lower().split():
            target.setdefault(tok, eid)
    return people, accounts


def _meeting_entity_id(rec: dict) -> str:
    """One ID per meeting series. Recurring meetings collapse to a single
    entity (we don't want a node per weekly occurrence — too much graph
    bloat). One-offs get the date appended for distinctness."""
    summary = rec.get("summary") or "meeting"
    base = _slug(summary)
    if rec.get("is_recurring"):
        return f"meeting:{base}"
    iso = rec.get("iso") or "unknown"
    return f"meeting:{base}:{iso}"


def write_meetings_to_graph(*, memory_path: Path, signals: dict) -> dict:
    """Add / refresh meeting entities + attended_by and about_account edges
    in graph.json. Idempotent per meeting ID. Returns counts for telemetry.
    """
    events = [e for e in (signals.get("events") or []) if e.get("is_high_stakes") and not e.get("is_cancelled")]
    if not events:
        return {"meetings_added": 0, "edges_added": 0, "skipped_unresolved": 0}

    with _graph_lock:
        g = _load_graph(memory_path)
        people_idx, accounts_idx = _build_lookups(g)

        # Drop ALL existing meeting entities + their edges first — the
        # extraction is the source of truth so a stale recurring meeting
        # whose summary changed shouldn't linger.
        ents = g.get("entities") or {}
        old_meeting_ids = {eid for eid, e in ents.items() if e.get("type") == "meeting"}
        for eid in old_meeting_ids:
            ents.pop(eid, None)
        g["edges"] = [
            e for e in (g.get("edges") or [])
            if e.get("from") not in old_meeting_ids and e.get("to") not in old_meeting_ids
        ]

        meetings_added = 0
        edges_added    = 0
        unresolved     = 0

        now_iso = datetime.now().isoformat(timespec="seconds")

        # Stage 3: only bother finding past occurrences for recurring series
        # (one-off events can't have prior occurrences by definition).
        # Per-write cost: ~200 file stat()s on a typical inbox; well under
        # the LLM extraction time so it's not the bottleneck.
        past_occurrence_count = 0

        for rec in events:
            mid = _meeting_entity_id(rec)
            past_notes: list = []
            if rec.get("is_recurring"):
                try:
                    past_notes = _find_past_occurrences(memory_path, rec.get("summary", ""))
                except Exception:
                    past_notes = []
                if past_notes:
                    past_occurrence_count += len(past_notes)

            ents[mid] = {
                "id":   mid,
                "name": rec.get("summary", ""),
                "type": "meeting",
                "props": {
                    "kind":         rec.get("kind", ""),
                    "recurrence":   rec.get("recurrence", "one_off"),
                    "is_recurring": bool(rec.get("is_recurring")),
                    "next_iso":     rec.get("iso", ""),
                    "next_time":    rec.get("time", ""),
                    "location":     rec.get("location", ""),
                    "organizer":    rec.get("organizer", ""),
                    "urgency":      rec.get("urgency", "medium"),
                    "kind_why":     rec.get("why", []),
                    "action":       rec.get("action_required", ""),
                },
                # Past-occurrence files end up here; the curator's spread
                # activation pulls entity sources into the candidate set,
                # so when this meeting surfaces, last week's transcript
                # comes along automatically.
                "sources":     past_notes,
                "salience":    0.7 if rec.get("urgency") == "high" else 0.55,
                "first_seen":  now_iso,
            }
            meetings_added += 1

            # Resolve people_links / attendees → person entity IDs.
            for slug in (rec.get("people_links") or []):
                pid = people_idx.get(slug.lower()) or people_idx.get(slug.replace("_", " ").lower())
                if not pid:
                    unresolved += 1
                    continue
                g["edges"].append({
                    "from": mid, "to": pid, "type": "attended_by",
                    "weight": 0.6, "first_seen": now_iso,
                })
                g["edges"].append({
                    "from": pid, "to": mid, "type": "attends",
                    "weight": 0.6, "first_seen": now_iso,
                })
                edges_added += 2

            # Resolve account_links → account entity IDs.
            for slug in (rec.get("account_links") or []):
                aid = accounts_idx.get(slug.lower()) or accounts_idx.get(slug.replace("_", " ").lower())
                if not aid:
                    unresolved += 1
                    continue
                g["edges"].append({
                    "from": mid, "to": aid, "type": "about_account",
                    "weight": 0.7, "first_seen": now_iso,
                })
                g["edges"].append({
                    "from": aid, "to": mid, "type": "discussed_in",
                    "weight": 0.7, "first_seen": now_iso,
                })
                edges_added += 2

        g["entities"] = ents
        _save_graph(memory_path, g)

        return {
            "meetings_added":       meetings_added,
            "edges_added":          edges_added,
            "skipped_unresolved":   unresolved,
            "old_meetings_purged":  len(old_meeting_ids),
            "past_occurrences_attached": past_occurrence_count,
        }


# ─── Background-friendly wrapper ──────────────────────────────────────────────

_refresh_lock = threading.Lock()
_refresh_in_flight = False


def refresh_in_background(
    *,
    memory_path: Path,
    ics_path:    Path,
    user_name:   str,
    cfg,
    days_ahead:  int = 14,
    max_events:  int = 60,
) -> None:
    """Fire-and-forget refresh. Single-flight: bursts collapse to one run."""
    global _refresh_in_flight
    with _refresh_lock:
        if _refresh_in_flight:
            return
        _refresh_in_flight = True

    def _run():
        global _refresh_in_flight
        try:
            sigs = extract_calendar_signals(
                ics_path    = ics_path,
                user_name   = user_name,
                cfg         = cfg,
                days_ahead  = days_ahead,
                max_events  = max_events,
            )
            save_signals(sigs, memory_path)
            print(f"[calendar] refresh done: {sigs['scanned']} scanned · "
                  f"{sigs['high_stakes']} high-stakes · "
                  f"{sigs['skipped']['cancelled']} cancelled + "
                  f"{sigs['skipped']['personal']} personal + "
                  f"{sigs['skipped']['errors']} errors filtered out", flush=True)

            # Stage 2: also write meeting nodes + attended_by / about_account
            # edges into graph.json so the curator's spread-activation
            # surfaces meeting context when the user asks about a person /
            # account.
            try:
                gr = write_meetings_to_graph(memory_path=memory_path, signals=sigs)
                print(f"[calendar] graph: +{gr['meetings_added']} meetings · "
                      f"+{gr['edges_added']} edges · "
                      f"{gr['skipped_unresolved']} unresolved attendees/accounts · "
                      f"{gr['old_meetings_purged']} stale meetings purged · "
                      f"{gr.get('past_occurrences_attached', 0)} past notes linked to recurring series",
                      flush=True)
            except Exception:
                print("[calendar] graph write error:\n" + traceback.format_exc(), flush=True)
        except Exception:
            print("[calendar] background error:\n" + traceback.format_exc(), flush=True)
        finally:
            with _refresh_lock:
                _refresh_in_flight = False

    threading.Thread(target=_run, daemon=True, name="engram-calendar-refresh").start()


# ─── Helper: find latest ICS the same way the dashboard does ──────────────────

def find_latest_ics(*, inbox_src: Optional[Path], memory_path: Path) -> Optional[Path]:
    """Match the dashboard's ICS-pick logic so background extraction always
    operates on the same file the chat agenda would see."""
    candidates: list = []
    for d in (inbox_src, memory_path):
        if d and Path(d).exists():
            try:
                candidates.extend(Path(d).rglob("*.ics"))
            except Exception:
                continue
    # Skip _processed/ archives — same rule as the chat agenda search
    candidates = [p for p in candidates if "_processed" not in p.parts]
    if not candidates:
        return None
    return max(candidates, key=lambda f: f.stat().st_mtime)
