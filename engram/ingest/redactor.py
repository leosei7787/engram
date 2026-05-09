"""
engram.ingest.redactor — Pre-compilation redaction layer
=========================================================

Applies configurable regex patterns to raw document text before it enters
the compilation pipeline. Matched spans are replaced with a typed placeholder:
  [REDACTED:email], [REDACTED:phone], [REDACTED:compensation], etc.

Configuration (in engram_config.yaml):

  redaction:
    enabled: true
    rules:
      - name: email
        pattern: '[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+'
        label: email
      - name: compensation
        pattern: '(?:EUR|USD|GBP|CHF)\\s*\\d[\\d,\\.]*[kKmM]?'
        label: compensation

Built-in rule sets (enabled by default, can be overridden):

  builtin_rules:
    email:        true
    phone:        true
    ssn:          true
    credit_card:  true
    compensation: false    # off by default — set true if needed

Redaction is applied AT INGEST — documents stored in the inbox and written
to memory-store/ are already redacted. Retrieval-time redaction is not
implemented (single-pass is simpler and sufficient for V2).

Usage:
    from engram.ingest.redactor import Redactor, RedactorConfig
    r = Redactor(RedactorConfig(enabled=True))
    clean = r.redact(raw_text, source="email_from_hr.md")
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# ─── Built-in rule patterns ───────────────────────────────────────────────────

_BUILTIN_PATTERNS: dict[str, str] = {
    "email": r"[a-zA-Z0-9_.+\-]+@[a-zA-Z0-9\-]+\.[a-zA-Z0-9.\-]+",

    "phone": (
        r"(?:(?:\+|00)\d{1,3}[\s\-.]?)?"
        r"(?:\(?\d{1,4}\)?[\s\-.]?)?"
        r"\d{3,4}[\s\-.]\d{3,4}"
    ),

    "ssn": r"\b\d{3}[-\s]\d{2}[-\s]\d{4}\b",

    "credit_card": r"\b(?:\d[ \-]?){13,16}\b",

    "compensation": (
        r"(?:€|\$|£|USD|EUR|GBP|CHF)[\s]*\d[\d,.]*[kKmM]?"
        r"|"
        r"\b\d[\d,.]*[kKmM]?[\s]*(?:€|\$|£|USD|EUR|GBP|CHF)\b"
    ),
}


# ─── Config ───────────────────────────────────────────────────────────────────

@dataclass
class RedactionRule:
    name:    str
    pattern: str
    label:   str = ""

    def __post_init__(self):
        if not self.label:
            self.label = self.name


@dataclass
class RedactorConfig:
    enabled: bool = False

    # Which built-in rule sets to activate
    builtin_rules: dict = field(default_factory=lambda: {
        "email":        True,
        "phone":        True,
        "ssn":          True,
        "credit_card":  True,
        "compensation": False,
    })

    # Additional custom rules from config file
    custom_rules: list[RedactionRule] = field(default_factory=list)

    # Placeholder template; {label} is substituted
    placeholder: str = "[REDACTED:{label}]"


def redactor_config_from_dict(d: dict) -> RedactorConfig:
    """Parse from raw YAML dict (cfg.redaction section)."""
    if not d:
        return RedactorConfig()
    builtin = d.get("builtin_rules", {})
    custom  = []
    for rule in (d.get("rules") or []):
        custom.append(RedactionRule(
            name    = rule.get("name", "custom"),
            pattern = rule.get("pattern", ""),
            label   = rule.get("label", rule.get("name", "custom")),
        ))
    return RedactorConfig(
        enabled       = bool(d.get("enabled", False)),
        builtin_rules = builtin,
        custom_rules  = custom,
        placeholder   = d.get("placeholder", "[REDACTED:{label}]"),
    )


# ─── Redactor ─────────────────────────────────────────────────────────────────

class Redactor:
    """
    Applies redaction rules to raw document text.

    Thread-safe: compiled regexes are immutable after __init__.
    """

    def __init__(self, config: RedactorConfig):
        self.config  = config
        self._rules: list[tuple[re.Pattern, str]] = []

        if not config.enabled:
            return

        # Add active built-in rules
        for name, pattern in _BUILTIN_PATTERNS.items():
            if config.builtin_rules.get(name, False):
                try:
                    self._rules.append((re.compile(pattern, re.IGNORECASE), name))
                except re.error as e:
                    print(f"[redactor] bad built-in pattern {name!r}: {e}", flush=True)

        # Add custom rules
        for rule in config.custom_rules:
            if rule.pattern:
                try:
                    self._rules.append((re.compile(rule.pattern, re.IGNORECASE), rule.label))
                except re.error as e:
                    print(f"[redactor] bad custom pattern {rule.name!r}: {e}", flush=True)

    def redact(self, text: str, source: str = "") -> tuple[str, int]:
        """
        Apply all active rules to text.

        Returns:
            (redacted_text, n_replacements)
        """
        if not self.config.enabled or not self._rules:
            return text, 0

        total = 0
        for pattern, label in self._rules:
            placeholder = self.config.placeholder.format(label=label)

            def _replace(m: re.Match) -> str:
                nonlocal total
                total += 1
                return placeholder

            text = pattern.sub(_replace, text)

        if total and source:
            print(f"[redactor] {source}: {total} spans redacted", flush=True)

        return text, total

    @property
    def is_active(self) -> bool:
        return self.config.enabled and bool(self._rules)


# ─── Convenience singleton ────────────────────────────────────────────────────

_DEFAULT: Optional[Redactor] = None


def get_redactor(config: Optional[RedactorConfig] = None) -> Redactor:
    """Return (or lazily create) the default Redactor."""
    global _DEFAULT
    if _DEFAULT is None or config is not None:
        _DEFAULT = Redactor(config or RedactorConfig())
    return _DEFAULT
