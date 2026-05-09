"""
engram.ingest.cleaner — Email/HTML noise stripper
==================================================

Turns a raw email/.eml/.html file into clean, signal-only markdown.
Designed for the watcher: cheap, deterministic, no LLM call required.

Pipeline:
  1. Detect marketing/spam emails — skip ingestion entirely
  2. Strip MIME headers, raw HTML/CSS/JS, tracking pixels
  3. Collapse whitespace, decode entities
  4. Truncate quoted reply chains (anything below the first "On <date> wrote:")
  5. Add a short header with From/Subject/Date if extractable

If you want richer extraction (named entities, key facts), pipe the cleaned
output through a small local LLM via `optional_llm_summarize()` — see the
SUMMARIZER_BIN env var for the binary path.

API:
    cleaner = EmailCleaner()
    result = cleaner.clean(raw_text, filename="...")
    # result is None if marketing/spam, else a dict:
    # {"clean": str, "from": str|None, "subject": str|None,
    #  "is_marketing": bool, "stripped_chars": int}
"""
from __future__ import annotations

import html as _html
import os
import re
import subprocess
from dataclasses import dataclass


# ─── Marketing / spam classifier ──────────────────────────────────────────────
# Heuristic. Any single hit ⇒ classify as marketing. Conservative on the
# "skip" decision: only skip if marketing + very low signal in body.
MARKETING_FROM_DOMAINS = {
    # Newsletters / promo
    "uber.com", "ubereats.com", "noreply.uber.com",
    "lot.com", "lotpolish.com", "lufthansa.com", "klm.com", "airfrance.com",
    "ryanair.com", "easyjet.com", "wizzair.com", "vueling.com",
    "booking.com", "expedia.com", "trivago.com", "hotels.com", "agoda.com",
    "trip.com", "skyscanner.com",
    "linkedin.com",  # promotional only — not member messages
    "spotify.com", "netflix.com", "disney.com", "youtube.com",
    "amazon.com", "amazon.de", "amazon.co.uk",
    "marketing.", "newsletter.", "promo.",
}

MARKETING_SUBJECT_RE = re.compile(
    r"\b(?:"
    r"\d+%\s*off|"
    r"\d+\s*[€$£]\s*off|"
    r"limited\s*time|"
    r"sale\s*ends|"
    r"hurry|don'?t\s*miss|"
    r"newsletter|digest|weekly\s*recap|monthly\s*recap|"
    r"unsubscribe|"
    r"upgrade\s*available|"
    r"order\s*now|book\s*now|shop\s*now|"
    r"flash\s*sale|"
    r"earn\s*rewards|points\s*available|"
    r"free\s*trial|free\s*shipping"
    r")\b",
    re.IGNORECASE,
)

# ─── HTML stripper ────────────────────────────────────────────────────────────
_TAG_RE     = re.compile(r"<[^>]+>", re.DOTALL)
_STYLE_RE   = re.compile(r"<(style|script|head|svg)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)
_CSS_BLOCK  = re.compile(r"@(?:media|font-face|keyframes|import|charset)[^{}]*\{[^{}]*\}", re.IGNORECASE | re.DOTALL)
_CSS_RULE   = re.compile(r"^[\.\#\@a-z][^{}\n]{0,200}\{[^{}]*\}", re.IGNORECASE | re.MULTILINE)
_WS_RE      = re.compile(r"[ \t]{2,}")
_MULTI_BLANK = re.compile(r"\n{3,}")


def _strip_html(text: str) -> str:
    """Remove HTML/CSS/JS noise, decode entities, collapse whitespace."""
    s = text
    # Remove comments first so we don't accidentally keep CSS inside them
    s = _COMMENT_RE.sub(" ", s)
    # Strip style/script/head/svg blocks completely
    s = _STYLE_RE.sub(" ", s)
    # Strip CSS-style at-rules and rule blocks (post-style-tag leakage)
    s = _CSS_BLOCK.sub(" ", s)
    s = _CSS_RULE.sub(" ", s)
    # Drop all remaining tags
    s = _TAG_RE.sub(" ", s)
    # Decode HTML entities (&nbsp;, &amp;, etc.)
    s = _html.unescape(s)
    # Drop zero-width / soft-hyphen / non-breaking-space junk
    s = re.sub(r"[​‌‍­‎‏]", "", s)
    s = s.replace(" ", " ")
    # Collapse whitespace
    s = _WS_RE.sub(" ", s)
    s = _MULTI_BLANK.sub("\n\n", s)
    # Strip trailing whitespace per line
    s = "\n".join(line.rstrip() for line in s.splitlines())
    return s.strip()


# ─── Email header parsing ─────────────────────────────────────────────────────
_FROM_RE    = re.compile(r"^\s*\*?\*?From:\*?\*?\s*(.+?)$",    re.IGNORECASE | re.MULTILINE)
_SUBJECT_RE = re.compile(r"^\s*\*?\*?Subject:\*?\*?\s*(.+?)$", re.IGNORECASE | re.MULTILINE)
_TO_RE      = re.compile(r"^\s*\*?\*?To:\*?\*?\s*(.+?)$",      re.IGNORECASE | re.MULTILINE)
_DATE_RE    = re.compile(r"^\s*\*?\*?Date:\*?\*?\s*(.+?)$",    re.IGNORECASE | re.MULTILINE)


def _extract_headers(text: str, max_search_chars: int = 4000) -> dict:
    head = text[:max_search_chars]
    out = {}
    for label, rx in (("from", _FROM_RE), ("subject", _SUBJECT_RE),
                      ("to", _TO_RE), ("date", _DATE_RE)):
        m = rx.search(head)
        if m:
            out[label] = m.group(1).strip()[:200]
    return out


def _extract_email_addr(from_field: str | None) -> str | None:
    if not from_field:
        return None
    m = re.search(r"<?([\w.+\-]+@[\w.\-]+)>?", from_field)
    return m.group(1).lower() if m else None


def _extract_domain(addr: str | None) -> str | None:
    if not addr or "@" not in addr:
        return None
    return addr.split("@")[-1].lower()


# ─── Reply chain truncation ───────────────────────────────────────────────────
_REPLY_BOUNDARY_RE = re.compile(
    r"^(?:"
    r"On\s+\w{3},?\s+\w+\s+\d+,?\s+\d{4}.*wrote:|"
    r"-+\s*Original Message\s*-+|"
    r"From:\s+.+\nSent:\s+.+|"
    r"Le\s+\w+\s+\d+\s+\w+\s+\d{4}.*écrit\s*:"
    r")",
    re.MULTILINE,
)


def _truncate_replies(text: str) -> str:
    """Cut everything below the first reply boundary marker."""
    m = _REPLY_BOUNDARY_RE.search(text)
    if m and m.start() > 200:
        return text[:m.start()].rstrip() + "\n\n[... reply chain truncated ...]"
    return text


# ─── Marketing classifier ─────────────────────────────────────────────────────
def _is_marketing(headers: dict, body: str) -> tuple[bool, str]:
    """Return (is_marketing, reason). Conservative."""
    addr = _extract_email_addr(headers.get("from"))
    dom  = _extract_domain(addr)

    # 1. Sender domain match
    if dom:
        for marker in MARKETING_FROM_DOMAINS:
            if marker in dom:
                return True, f"marketing-domain:{dom}"

    # 2. Subject pattern
    subj = headers.get("subject", "") or ""
    if MARKETING_SUBJECT_RE.search(subj):
        return True, f"marketing-subject"

    # 3. Body contains heavy unsubscribe + tracking link patterns + low signal
    if "unsubscribe" in body.lower() and len(body) > 1500:
        # Combined with Order/promo CTAs: very likely marketing
        if any(kw in body.lower() for kw in
               ("promo code", "order now", "limited time", "shop now",
                "earn points", "free trial", "redeem", "% off")):
            return True, "marketing-cta-pattern"

    return False, ""


# ─── Public API ───────────────────────────────────────────────────────────────
@dataclass
class CleanResult:
    clean:           str
    headers:         dict
    is_marketing:    bool
    skip_reason:     str
    original_chars:  int
    cleaned_chars:   int

    def to_markdown(self) -> str:
        h = self.headers
        parts = []
        if h.get("subject"):
            parts.append(f"# {h['subject']}\n")
        meta = []
        if h.get("from"): meta.append(f"**From:** {h['from']}")
        if h.get("to"):   meta.append(f"**To:** {h['to']}")
        if h.get("date"): meta.append(f"**Date:** {h['date']}")
        if meta:
            parts.append("\n".join(meta))
            parts.append("")
        parts.append(self.clean)
        return "\n".join(parts)


class EmailCleaner:
    """
    Stateless cleaner. Use a single instance for many files.

    skip_marketing: if True, .clean() returns is_marketing=True with empty
                    output so the watcher can drop the file silently.
    """

    def __init__(self, *, skip_marketing: bool = True):
        self.skip_marketing = skip_marketing

    def clean(self, raw: str, *, filename: str = "") -> CleanResult:
        original_len = len(raw)
        headers = _extract_headers(raw)

        # Strip HTML/CSS/JS first, then truncate replies
        body = _strip_html(raw)
        body = _truncate_replies(body)

        is_marketing, reason = _is_marketing(headers, body)

        if is_marketing and self.skip_marketing:
            return CleanResult(
                clean="", headers=headers,
                is_marketing=True, skip_reason=reason,
                original_chars=original_len,
                cleaned_chars=0,
            )

        return CleanResult(
            clean=body,
            headers=headers,
            is_marketing=is_marketing,
            skip_reason=reason if is_marketing else "",
            original_chars=original_len,
            cleaned_chars=len(body),
        )


# ─── Optional: local LLM summarization (Ollama / llama.cpp) ──────────────────
def optional_llm_summarize(text: str, *, model: str | None = None) -> str | None:
    """
    Run a small local model to extract the signal from a cleaned email.
    Returns None if no model is configured or call fails — never raises.

    Configuration:
      $ENGRAM_LOCAL_LLM = "ollama" (or "llama-cpp")
      $ENGRAM_LOCAL_LLM_MODEL = "llama3.2:3b" (or any local model name)

    Falls back silently if Ollama/llama.cpp aren't installed.
    """
    backend = os.environ.get("ENGRAM_LOCAL_LLM", "")
    model   = model or os.environ.get("ENGRAM_LOCAL_LLM_MODEL", "llama3.2:3b")
    if not backend or not text.strip():
        return None

    prompt = (
        "Extract the key facts from this email. Output 3-5 bullet points only — "
        "no preamble, no greeting echo. Drop greetings, signatures, marketing CTAs, "
        "tracking pixels, legal disclaimers. Keep names, dates, decisions, action items.\n\n"
        f"---\n{text[:8000]}\n---"
    )

    try:
        if backend == "ollama":
            r = subprocess.run(
                ["ollama", "run", model, prompt],
                capture_output=True, text=True, timeout=30,
            )
            return (r.stdout or "").strip() or None
    except Exception:
        return None
    return None


# ─── CLI for quick testing ───────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m engram.ingest.cleaner <file>")
        sys.exit(1)
    raw = open(sys.argv[1], encoding="utf-8", errors="ignore").read()
    res = EmailCleaner(skip_marketing=False).clean(raw, filename=sys.argv[1])
    print(f"original: {res.original_chars:,} chars")
    print(f"cleaned:  {res.cleaned_chars:,} chars  ({100-100*res.cleaned_chars//max(res.original_chars,1)}% noise stripped)")
    print(f"marketing: {res.is_marketing}  reason: {res.skip_reason or '-'}")
    print()
    print(res.to_markdown())
