"""
Source credibility — classify a memory file path / source string into a
credibility score from SOURCE_CREDIBILITY.

Heuristic-based — no LLM call needed for routing.

Marker strings that flag "this content is the user's own voice" (e.g.
"user says:", "/user_") are user-agnostic by design. The list of partner /
account name fragments used for the partner-doc boost is configurable —
the caller passes a list (typically from cfg.retrieval.priority_accounts)
or accepts the empty default and gets no boost.
"""
import os
from .schemas import SOURCE_CREDIBILITY


def _partner_keys_override() -> list:
    """Hook for callers / tests to inject a list of partner/account name
    fragments. Default: empty (no partner-doc boost). Set the
    ENGRAM_PARTNER_KEYS env var (comma-separated) for a quick override
    without code changes; the dashboard / pipeline pass the same list
    via classify_source's `partner_keys` arg if present.
    """
    raw = os.environ.get("ENGRAM_PARTNER_KEYS", "")
    if not raw:
        return []
    return [k.strip().lower() for k in raw.split(",") if k.strip()]


def classify_source(
    source_path: str,
    content_hint: str = "",
    *,
    partner_keys: list | None = None,
) -> tuple[str, float]:
    """Returns (label, credibility_score). Label maps to SOURCE_CREDIBILITY keys."""
    s = (source_path or "").lower()
    c = (content_hint or "")[:500].lower()

    # Tier 1 — direct user-voice signals. Marker strings are generic so
    # any deployment can use them.
    if "user says:" in c or "user:" in c[:200] or "/sessions/" in s:
        return "user_statement", SOURCE_CREDIBILITY["user_statement"]
    if "/user_" in s or "user_upload" in s:
        return "user_upload", SOURCE_CREDIBILITY["user_upload"]

    # Path-based heuristics
    if "/decisions/" in s:
        return "internal_doc", SOURCE_CREDIBILITY["internal_doc"]
    if "/accounts/" in s:
        return "internal_doc", SOURCE_CREDIBILITY["internal_doc"]
    if "/research/" in s:
        if any(k in s for k in ("gartner", "idc", "forrester", "abi")):
            return "analyst_report", SOURCE_CREDIBILITY["analyst_report"]
        if any(k in s for k in ("ft_", "press", "news")):
            return "news_article", SOURCE_CREDIBILITY["news_article"]
        return "internal_deck", SOURCE_CREDIBILITY["internal_deck"]

    # Daily file types
    if "/daily/emails/" in s:
        return "internal_email", SOURCE_CREDIBILITY["internal_email"]
    if "/daily/transcripts/" in s:
        return "internal_meeting", SOURCE_CREDIBILITY["internal_meeting"]
    if "/daily/documents/" in s:
        return "internal_doc", SOURCE_CREDIBILITY["internal_doc"]

    # File extension fallbacks
    if s.endswith(".pptx") or s.endswith(".key"):
        return "internal_deck", SOURCE_CREDIBILITY["internal_deck"]
    if s.endswith(".docx") or s.endswith(".pdf"):
        return "internal_doc", SOURCE_CREDIBILITY["internal_doc"]

    # Partner doc detection — keys come from config / env, never hardcoded
    keys = partner_keys if partner_keys is not None else _partner_keys_override()
    if keys and any(k in s for k in keys):
        if "partner" in s or "_brief" in s or "_requirements" in s:
            return "partner_doc", SOURCE_CREDIBILITY["partner_doc"]

    # Inferred (e.g. produced by graph synthesis)
    if "synthesised" in s or "inferred" in s:
        return "inferred_from_context", SOURCE_CREDIBILITY["inferred_from_context"]

    return "unknown", SOURCE_CREDIBILITY["unknown"]


def credibility_for_sources(sources: list, content_hint: str = "") -> float:
    """Mean credibility across sources; max if a direct user-voice source is present."""
    if not sources:
        return SOURCE_CREDIBILITY["unknown"]
    scores = []
    has_user_voice = False
    for s in sources:
        lbl, sc = classify_source(s, content_hint)
        scores.append(sc)
        if lbl in ("user_statement", "user_upload"):
            has_user_voice = True
    if has_user_voice:
        return max(scores)
    return sum(scores) / len(scores)
