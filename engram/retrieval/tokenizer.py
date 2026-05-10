"""
engram.retrieval.tokenizer — Query tokenization
================================================

Converts a natural-language query into a set of signal tokens for keyword
matching. Also extracts proper-noun candidates and person-name hints for
meeting/email queries.

New in v0.2: stem_token(), stemmed_tokens(), expand_with_synonyms()
  - stem_token()          lightweight suffix stemmer (no deps)
  - stemmed_tokens()      set of stems for a token set
  - expand_with_synonyms() bidirectional synonym expansion from config
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional


# ─── Stop words ───────────────────────────────────────────────────────────────

STOP_WORDS: frozenset = frozenset({
    # Articles / determiners / pronouns / aux
    "the", "and", "for", "with", "this", "that", "are", "was", "has",
    "have", "what", "give", "about", "from", "its", "our", "can",
    "get", "all", "any", "but", "not", "how", "who", "why", "let",
    "use", "his", "her", "its", "you", "did", "new", "old", "your",
    "their", "they", "them", "she", "him", "out", "off", "than",
    # Common verbs that don't carry topic signal
    "tell", "told", "say", "said", "ask", "asked", "want", "need", "show",
    "help", "make", "made", "see", "seen", "look", "find", "found",
    "going", "goes", "went", "take", "took", "give", "given", "gave",
    "know", "known", "knew", "think", "thought", "feel", "felt",
    "build", "built", "draft", "write", "wrote", "send", "sent",
    # Generic abstract nouns
    "thing", "things", "stuff", "everything", "something", "anything",
    "nothing", "someone", "anyone", "everyone", "nobody", "somebody",
    "everybody", "people", "person", "info", "details", "summary",
    "overview", "background", "context", "issue", "issues", "matter",
    # Time / count generic
    "next", "last", "today", "tomorrow", "yesterday", "now", "then",
    "soon", "later", "before", "after", "during", "since",
    "year", "years", "month", "months", "day", "days", "week", "weeks",
    "one", "two", "three", "four", "five", "six", "many", "few", "lot",
    "much", "most", "more", "less", "least", "best", "worst",
    # Politeness / fluff
    "please", "thanks", "really", "very", "much", "just", "also", "even",
    "still", "yet", "already", "actually", "basically", "simply",
    "merely", "only",
})


# ─── Meeting signal words ─────────────────────────────────────────────────────

MEETING_WORDS: frozenset = frozenset({
    "11", "1on1", "catchup", "catch-up", "meeting", "meet", "agenda",
    "email", "emails", "inbox", "sent", "sender", "discuss", "discussion",
    "prep", "prepare", "preparation",
})


# ─── Core tokenizer ───────────────────────────────────────────────────────────

def query_tokens(query: str) -> set:
    """
    Tokenise query for matching — lowercase words ≥3 chars, minus stop words.

    Returns a set of signal tokens that carry topical meaning.
    """
    return {
        w for w in re.split(r'\W+', query.lower())
        if len(w) >= 3 and w not in STOP_WORDS
    }


def proper_nouns(query: str, tokens: Optional[set] = None) -> set:
    """
    Extract proper-noun tokens from the query (CapitalisedWords ≥3 chars).
    Returns the set of lowercase forms that also appear in tokens.

    These receive a score multiplier during keyword and wiki scoring.
    """
    toks = tokens if tokens is not None else query_tokens(query)
    return {
        w.lower()
        for w in re.findall(r"\b([A-Z][A-Za-z][A-Za-z0-9_-]+)\b", query)
        if w.lower() in toks
    }


def is_meeting_query(tokens: set) -> bool:
    """True if the query looks like a meeting/email prep request."""
    return bool(MEETING_WORDS & tokens)


def query_person_names(query: str, people_file: Optional[Path] = None) -> set:
    """
    Extract first-name-looking tokens from the query (e.g. Alice, Carol).

    Cross-checks against a people reference file if provided — only known
    names count, to avoid matching arbitrary capitalised words.

    Args:
        query:       The raw user query.
        people_file: Optional path to a people.md file with ## Name headings.
    """
    candidates = {w for w in re.findall(r"\b([A-Z][a-z]{2,15})\b", query)}

    if not people_file or not Path(people_file).exists():
        return candidates

    try:
        text = Path(people_file).read_text(errors="ignore")
    except Exception:
        return candidates

    known_first: set = set()
    for m in re.finditer(r"^#{2,4}\s+([A-Z][a-z]+)\b", text, re.MULTILINE):
        known_first.add(m.group(1))
    for m in re.finditer(r"\*\*([A-Z][a-z]+)\b", text):
        known_first.add(m.group(1))

    return candidates & known_first


# ─── Stemmer ─────────────────────────────────────────────────────────────────
#
# A lightweight suffix stemmer — no external deps. Not Porter-complete, but
# handles the most common English morphological variants well enough for BM25-
# style retrieval. Suffixes are tried longest-first so we don't double-strip.
#
# Examples:
#   meetings  → meet    hiring  → hir    decided  → decid
#   decisions → decis   hired   → hir    recruiter → recruit
#   accounts  → account companies → compan  strategic → strateg

_STEM_SUFFIXES: tuple = (
    # 6-char
    "ations",                       # examinations → examin
    "nesses",                       # businesses → busin
    # 5-char
    "ments",                        # agreements → agreem
    "tions",                        # organisations → organis
    "ings",                         # meetings → meet
    # 4-char
    "ions",                         # decisions → decis, opinions → opin
    "ness",                         # business → busin
    "ment",                         # agreement → agreem
    "tion",                         # station → stat
    "ers",                          # recruiters → recruit
    "ies",                          # companies → compan
    "ied",                          # occupied → occupi
    # 3-char
    "ics",                          # logistics → logist, analytics → analyt
    "ing",                          # deciding → decid, hiring → hir
    "ion",                          # decision → decis, option → opt
    # 2-char
    "ic",                           # strategic → strateg, logistic → logist
    "ed",                           # decided → decid, hired → hir
    "er",                           # recruiter → recruit
    "es",                           # decides → decid
    "ly",                           # quickly → quick
    "al",                           # seasonal → season
    # 1-char (last resort — plurals)
    "s",                            # accounts → account
)

_STEM_MIN_ROOT: int = 3  # shortest root we'll keep after stripping


def stem(word: str) -> str:
    """
    Strip the longest matching English suffix, keeping a minimum root length.

    This is the base stem — use stem_token() for retrieval (adds 'e' normalisation).
    """
    for sfx in _STEM_SUFFIXES:
        if word.endswith(sfx) and len(word) - len(sfx) >= _STEM_MIN_ROOT:
            return word[: -len(sfx)]
    return word


def stem_token(word: str) -> str:
    """
    Stem + strip trailing 'e' so hire/hiring/hired all normalise to 'hir'.

    This is the canonical form used for cross-token matching during retrieval.
    Both query tokens and document tokens should be normalised with this function
    before comparing stems.
    """
    s = stem(word)
    # Strip trailing 'e' only when root stays >= _STEM_MIN_ROOT chars.
    # This collapses pairs like decide/deciding → decid, hire/hiring → hir.
    return s.rstrip("e") if len(s) > _STEM_MIN_ROOT else s


def stemmed_tokens(tokens: set) -> set:
    """Return the set of normalised stems for a token set."""
    return {stem_token(t) for t in tokens if len(t) >= _STEM_MIN_ROOT}


# ─── Synonym expansion ────────────────────────────────────────────────────────

def expand_with_synonyms(tokens: set, synonyms: dict) -> set:
    """
    Expand a token set with configured synonyms (bidirectional).

    synonyms format (from config YAML):

        retrieval:
          synonyms:
            automotive: [car, vehicle, vehicles]
            amx: [acmemotors, acmetech]
            hire: [recruit, recruitment, recruiting]

    Logic:
      - If ANY member of a synonym group appears in tokens, ALL members are added.
      - This means "amx" in query → adds "acmemotors"; "acmemotors" in query → adds "amx".

    Args:
        tokens:   Set of query tokens (from query_tokens()).
        synonyms: Dict from config (key → list of equivalents).

    Returns:
        Expanded token set (always a superset of the input).
    """
    if not synonyms:
        return tokens

    expanded = set(tokens)
    for key, vals in synonyms.items():
        key_l   = key.lower()
        vals_l  = {v.lower() for v in vals}
        group   = {key_l} | vals_l
        if tokens & group:        # any member of the group found in query?
            expanded |= group     # pull in all equivalents
    return expanded
