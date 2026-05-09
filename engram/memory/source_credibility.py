"""
Source credibility — classify a memory file path / source string into a
credibility score from SOURCE_CREDIBILITY.

Heuristic-based — no LLM call needed for routing.
"""
import re
from .schemas import SOURCE_CREDIBILITY


def classify_source(source_path: str, content_hint: str = "") -> tuple[str, float]:
    """
    Returns (label, credibility_score). Label maps to SOURCE_CREDIBILITY keys.
    """
    s = (source_path or "").lower()
    c = (content_hint or "")[:500].lower()

    # Direct Leo signals
    if "leo says:" in c or "leo:" in c[:200] or "/sessions/" in s:
        return "leo_statement", SOURCE_CREDIBILITY["leo_statement"]
    if "/leo_" in s or "leo_upload" in s:
        return "leo_upload", SOURCE_CREDIBILITY["leo_upload"]

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

    # Partner doc detection from filename keywords
    if any(k in s for k in ("toyota", "bmw", "honda", "vw", "acmetech", "betamotors", "microsoft")):
        if "partner" in s or "_brief" in s or "_requirements" in s:
            return "partner_doc", SOURCE_CREDIBILITY["partner_doc"]

    # Inferred (e.g. produced by graph synthesis)
    if "synthesised" in s or "inferred" in s:
        return "inferred_from_context", SOURCE_CREDIBILITY["inferred_from_context"]

    return "unknown", SOURCE_CREDIBILITY["unknown"]


def credibility_for_sources(sources: list, content_hint: str = "") -> float:
    """Mean credibility across sources (max if a leo source is present)."""
    if not sources:
        return SOURCE_CREDIBILITY["unknown"]
    scores = []
    has_leo = False
    for s in sources:
        lbl, sc = classify_source(s, content_hint)
        scores.append(sc)
        if lbl in ("leo_statement", "leo_upload"):
            has_leo = True
    if has_leo:
        return max(scores)
    return sum(scores) / len(scores)
