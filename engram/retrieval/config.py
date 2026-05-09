"""
engram.retrieval.config — Configuration loader
===============================================

Loads engram configuration from (in priority order):
  1. ENGRAM_* environment variables
  2. YAML config file (ENGRAM_CONFIG_FILE env var, default ~/.engram/config.yaml)
  3. Hardcoded safe defaults

Usage:
    from engram.retrieval.config import load_config
    cfg = load_config()

    memory_path = cfg.memory_path
    wiki_path   = cfg.wiki_path
    max_files   = cfg.retrieval.keyword.max_files
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


# ─── Default config file location ────────────────────────────────────────────

DEFAULT_CONFIG_FILE = Path.home() / ".engram" / "config.yaml"


# ─── Dataclasses (typed view of the YAML) ────────────────────────────────────

@dataclass
class IdentityConfig:
    org_name:     str = "My Organisation"
    user_name:    str = "User"
    user_role:    str = ""
    user_email:   str = ""
    system_name:  str = "Knowledge System"
    accent_color: str = "#6366f1"


@dataclass
class PathsConfig:
    memory_path:  str = ""
    wiki_path:    str = ""
    inbox_src:    str = ""
    outputs_path: str = ""
    sessions_dir: Optional[str] = None
    claude_bin:   Optional[str] = None
    base_path:    Optional[str] = None


@dataclass
class ModelsConfig:
    primary:   str = "claude-sonnet-4-5"
    haiku:     str = "claude-haiku-4-5"
    deep_work: str = "claude-haiku-4-5"
    local:     Optional[str] = None


@dataclass
class TierDecayConfig:
    working:      float = 0.40
    episodic:     float = 0.035
    semantic:     float = 0.006
    crystallised: float = 0.001


@dataclass
class SalienceModifiersConfig:
    is_decision:          float = 0.30
    is_risk:              float = 0.25
    source_is_user:       float = 0.20
    active_deal:          float = 0.15
    recent_upload:        float = 0.10
    contradicts_existing: float = 0.20


@dataclass
class RisConfig:
    retrieval_boost:         float = 0.05
    max_boost:               float = 0.20
    decay_inhibition_factor: float = 0.70


@dataclass
class CompressionConfig:
    min_confidence_to_keep: float = 0.25
    min_salience_to_keep:   float = 0.15
    max_working_age_hours:  int   = 48


@dataclass
class SleepCycleConfig:
    enabled:  bool = True
    schedule: str  = "02:00"
    phases: list = field(default_factory=lambda: [
        "deduplication",
        "contradiction_resolution",
        "insight_surfacing",
        "tier_promotion",
        "compression",
    ])


@dataclass
class MemoryConfig:
    tier_decay:          TierDecayConfig          = field(default_factory=TierDecayConfig)
    salience_modifiers:  SalienceModifiersConfig   = field(default_factory=SalienceModifiersConfig)
    ris:                 RisConfig                 = field(default_factory=RisConfig)
    compression:         CompressionConfig         = field(default_factory=CompressionConfig)
    sleep_cycle:         SleepCycleConfig          = field(default_factory=SleepCycleConfig)


@dataclass
class KeywordConfig:
    max_files:        int   = 8
    body_read_chars:  int   = 8000
    path_boosts:      dict  = field(default_factory=lambda: {
        "/accounts/":      2.5,
        "/decisions/":     1.8,
        "/weekly/":        1.6,
        "/context/":       1.4,
        "/saved/":         1.4,
        "/research/":      1.3,
        "/daily/emails/":  1.0,
        "/daily/":         0.9,
        "/episodic/":      1.1,
        "/archive/":       0.6,
    })
    filename_match:   dict  = field(default_factory=lambda: {
        "stem_hit_multiplier": 3.0,
        "path_hit_multiplier": 1.5,
    })
    score_components: dict  = field(default_factory=lambda: {
        "overlap_log_factor":  1.0,
        "proper_noun_factor":  3.0,
        "tf_log_factor":       0.4,
    })
    dynamic_caps:     dict  = field(default_factory=lambda: {
        "proper_noun_query_cap": 12,
        "meeting_query_cap":     15,
    })
    meeting_from_person: dict = field(default_factory=lambda: {
        "base_multiplier":             2.0,
        "short_email_threshold_bytes": 1500,
        "short_email_multiplier":      6.0,
        "recent_days_strong":          7,
        "recent_strong_multiplier":    4.0,
        "recent_days_weak":            14,
        "recent_weak_multiplier":      2.0,
    })
    claims_signal:    dict = field(default_factory=lambda: {
        "max_claims_to_consider": 30,
        "boost_per_claim":        0.6,
        "salience_weight":        2.0,
        "max_total_boost":        15.0,
    })
    scan_exclude: list = field(default_factory=lambda: [
        "/sessions/",
        "/_raw/",
        "/_pre_compression_backups/",
        "/proposals/",
        "/priming/",
        "/health/",
    ])


@dataclass
class GraphConfig:
    depth:                       int   = 2
    hop_decay:                   float = 0.6
    threshold:                   float = 0.12
    max_graph_extra_files:       int   = 3
    high_activation_threshold:   float = 0.50
    related_activation_threshold: float = 0.25


@dataclass
class WikiRetrievalConfig:
    max_pages:           int   = 4
    proper_noun_boost:   float = 3.0
    max_count_per_token: int   = 5
    use_qmd:             bool  = True   # prefer QMD BM25 backend (fallback to index scan)
    qmd_collection:      str   = "wiki" # QMD collection name for compiled wiki pages


@dataclass
class ContextBudgetConfig:
    max_total_chars:          int = 80000
    wiki_page_truncate_chars: int = 3500
    graph_block_max_high:     int = 6
    graph_block_max_related:  int = 4


@dataclass
class RetrievalConfig:
    keyword:        KeywordConfig        = field(default_factory=KeywordConfig)
    graph:          GraphConfig          = field(default_factory=GraphConfig)
    wiki:           WikiRetrievalConfig  = field(default_factory=WikiRetrievalConfig)
    context_budget: ContextBudgetConfig  = field(default_factory=ContextBudgetConfig)
    # Synonym groups for query expansion (bidirectional).
    # Any member of a group in the query pulls in all other members.
    # Example:  {"automotive": ["car", "vehicle"], "vw": ["volkswagen", "cariad"]}
    synonyms:       dict                 = field(default_factory=dict)


@dataclass
class DomainBundle:
    name:        str
    description: str       = ""
    triggers:    list      = field(default_factory=list)
    patterns:    list      = field(default_factory=list)


@dataclass
class WikiConfig:
    topics: list = field(default_factory=lambda: [
        "competition", "concepts", "decisions",
        "people", "problems", "projects", "systems",
    ])
    ingest: dict = field(default_factory=lambda: {
        "schedule_minutes": 10,
        "batch_size":        20,
        "model":             "claude-sonnet-4-5",
    })


@dataclass
class DeepWorkConfig:
    enabled:          bool = True
    specialist_model: str  = "claude-haiku-4-5"
    routing_model:    str  = "claude-haiku-4-5"
    synthesis_model:  str  = "claude-sonnet-4-5"
    specialists:      dict = field(default_factory=lambda: {
        "contrarian": {"enabled": True, "label": "Contrarian", "icon": "😈", "color": "#ef4444"},
        "cfo":        {"enabled": True, "label": "CFO",         "icon": "💰", "color": "#10b981"},
        "commercial": {"enabled": True, "label": "Commercial",  "icon": "🤝", "color": "#3b82f6"},
        "marketing":  {"enabled": True, "label": "Marketing",   "icon": "📣", "color": "#a855f7"},
        "hr":         {"enabled": True, "label": "People & Org","icon": "🧑‍🤝‍🧑","color": "#f59e0b"},
        "strategy":   {"enabled": True, "label": "Strategy",    "icon": "🎯", "color": "#8b5cf6"},
        "engineering":{"enabled": True, "label": "Engineering", "icon": "⚙️", "color": "#0891b2"},
    })


@dataclass
class DashboardConfig:
    enabled:         bool = True
    refresh_seconds: int  = 30
    show_pillars:    dict = field(default_factory=lambda: {
        "compile": True, "dream": True, "retrieve": True,
    })


@dataclass
class SystemPromptConfig:
    user_description: str  = ""
    user_tone:        str  = "Direct, execution-focused."
    # Files loaded into EVERY response regardless of query (relative to memory_path).
    # CLAUDE.md is the canonical always-on context file, updated nightly by the sleep cycle.
    always_load:      list = field(default_factory=lambda: ["CLAUDE.md", "preferences.md"])
    # Glob patterns (relative to memory_path) for calendar/agenda files.
    # The most-recently-modified match is auto-loaded every turn.
    calendar_globs:   list = field(default_factory=lambda: ["calendar*.md", "**/calendar*.md"])
    instructions:     dict = field(default_factory=dict)


@dataclass
class ChatConfig:
    """Controls which backend the dashboard uses to generate responses."""
    # "api"  — Anthropic Python SDK (requires ANTHROPIC_API_KEY env var)
    # "cli"  — Claude CLI subprocess (uses your logged-in Claude account,
    #           no separate API key needed; streams via --output-format stream-json)
    backend:   str           = "api"
    cli_bin:   Optional[str] = None    # full path to claude binary; auto-detected if None
    cli_model: Optional[str] = None    # override model for CLI (None = CLI default)


@dataclass
class EngramConfig:
    """Top-level engram configuration object."""
    identity:       IdentityConfig       = field(default_factory=IdentityConfig)
    paths:          PathsConfig          = field(default_factory=PathsConfig)
    models:         ModelsConfig         = field(default_factory=ModelsConfig)
    memory:         MemoryConfig         = field(default_factory=MemoryConfig)
    retrieval:      RetrievalConfig      = field(default_factory=RetrievalConfig)
    domain_bundles: list                 = field(default_factory=list)
    wiki:           WikiConfig           = field(default_factory=WikiConfig)
    deep_work:      DeepWorkConfig       = field(default_factory=DeepWorkConfig)
    dashboard:      DashboardConfig      = field(default_factory=DashboardConfig)
    system_prompt:  SystemPromptConfig   = field(default_factory=SystemPromptConfig)
    chat:           ChatConfig           = field(default_factory=ChatConfig)

    # Convenience shortcuts (populated by load_config)
    @property
    def memory_path(self) -> Path:
        p = self.paths.memory_path
        return Path(p) if p else Path("memory-store")

    @property
    def wiki_path(self) -> Path:
        p = self.paths.wiki_path
        return Path(p) if p else Path("wiki")

    @property
    def base_path(self) -> Path:
        p = self.paths.base_path
        return Path(p) if p else self.memory_path.parent

    @property
    def sessions_dir(self) -> Path:
        p = self.paths.sessions_dir
        return Path(p) if p else self.memory_path / "sessions"


# ─── Loader ───────────────────────────────────────────────────────────────────

_cfg_cache: Optional[EngramConfig] = None


def load_config(config_file: Optional[str] = None, *, reload: bool = False) -> EngramConfig:
    """
    Load and return the EngramConfig singleton.

    Args:
        config_file: Path to YAML config. Defaults to ENGRAM_CONFIG_FILE env var,
                     then ~/.engram/config.yaml.
        reload:      Force reload (bypass cache).
    """
    global _cfg_cache
    if _cfg_cache is not None and not reload:
        return _cfg_cache

    # Resolve config file path
    path_str = config_file or os.environ.get("ENGRAM_CONFIG_FILE", str(DEFAULT_CONFIG_FILE))
    path = Path(path_str)

    raw: dict = {}
    if path.exists():
        try:
            import yaml  # type: ignore
            raw = yaml.safe_load(path.read_text()) or {}
            print(f"[engram.config] loaded from {path}", flush=True)
        except ImportError:
            print("[engram.config] PyYAML not installed — using env vars + defaults only", flush=True)
        except Exception as e:
            print(f"[engram.config] error reading {path}: {e}", flush=True)
    else:
        print(f"[engram.config] no config file at {path} — using env vars + defaults", flush=True)

    cfg = _build_config(raw)
    _cfg_cache = cfg
    return cfg


def _build_config(raw: dict) -> EngramConfig:
    """Build EngramConfig from raw dict + env var overrides."""
    cfg = EngramConfig()

    # ── identity ──
    id_raw = raw.get("identity", {})
    cfg.identity = IdentityConfig(
        org_name     = _e("ENGRAM_ORG_NAME",     id_raw.get("org_name",     cfg.identity.org_name)),
        user_name    = _e("ENGRAM_USER_NAME",    id_raw.get("user_name",    cfg.identity.user_name)),
        user_role    = _e("ENGRAM_USER_ROLE",    id_raw.get("user_role",    cfg.identity.user_role)),
        user_email   = _e("ENGRAM_USER_EMAIL",   id_raw.get("user_email",   cfg.identity.user_email)),
        system_name  = _e("ENGRAM_SYSTEM_NAME",  id_raw.get("system_name",  cfg.identity.system_name)),
        accent_color = _e("ENGRAM_ACCENT_COLOR", id_raw.get("accent_color", cfg.identity.accent_color)),
    )

    # ── paths ──
    p_raw = raw.get("paths", {})
    cfg.paths = PathsConfig(
        memory_path  = _e("ENGRAM_MEMORY_PATH",  p_raw.get("memory_path",  cfg.paths.memory_path)),
        wiki_path    = _e("ENGRAM_WIKI_PATH",    p_raw.get("wiki_path",    cfg.paths.wiki_path)),
        inbox_src    = _e("ENGRAM_INBOX_SRC",    p_raw.get("inbox_src",    cfg.paths.inbox_src)),
        outputs_path = _e("ENGRAM_OUTPUTS_PATH", p_raw.get("outputs_path", cfg.paths.outputs_path)),
        sessions_dir = _e("ENGRAM_SESSIONS_DIR", p_raw.get("sessions_dir", cfg.paths.sessions_dir)),
        claude_bin   = _e("ENGRAM_CLAUDE_BIN",   p_raw.get("claude_bin",   cfg.paths.claude_bin)),
        base_path    = _e("ENGRAM_BASE_PATH",    p_raw.get("base_path",    cfg.paths.base_path)),
    )

    # ── models ──
    m_raw = raw.get("models", {})
    cfg.models = ModelsConfig(
        primary   = _e("ENGRAM_MODEL_PRIMARY",   m_raw.get("primary",   cfg.models.primary)),
        haiku     = _e("ENGRAM_MODEL_HAIKU",     m_raw.get("haiku",     cfg.models.haiku)),
        deep_work = _e("ENGRAM_MODEL_DEEP_WORK", m_raw.get("deep_work", cfg.models.deep_work)),
        local     = _e("ENGRAM_MODEL_LOCAL",     m_raw.get("local",     cfg.models.local)),
    )

    # ── memory ──
    mem_raw = raw.get("memory", {})
    td = mem_raw.get("tier_decay", {})
    sm = mem_raw.get("salience_modifiers", {})
    ri = mem_raw.get("ris", {})
    co = mem_raw.get("compression", {})
    sl = mem_raw.get("sleep_cycle", {})
    cfg.memory = MemoryConfig(
        tier_decay = TierDecayConfig(
            working      = float(td.get("working",      0.40)),
            episodic     = float(td.get("episodic",     0.035)),
            semantic     = float(td.get("semantic",     0.006)),
            crystallised = float(td.get("crystallised", 0.001)),
        ),
        salience_modifiers = SalienceModifiersConfig(
            is_decision          = float(sm.get("is_decision",          0.30)),
            is_risk              = float(sm.get("is_risk",              0.25)),
            source_is_user       = float(sm.get("source_is_user",       0.20)),
            active_deal          = float(sm.get("active_deal",          0.15)),
            recent_upload        = float(sm.get("recent_upload",        0.10)),
            contradicts_existing = float(sm.get("contradicts_existing", 0.20)),
        ),
        ris = RisConfig(
            retrieval_boost         = float(ri.get("retrieval_boost",         0.05)),
            max_boost               = float(ri.get("max_boost",               0.20)),
            decay_inhibition_factor = float(ri.get("decay_inhibition_factor", 0.70)),
        ),
        compression = CompressionConfig(
            min_confidence_to_keep = float(co.get("min_confidence_to_keep", 0.25)),
            min_salience_to_keep   = float(co.get("min_salience_to_keep",   0.15)),
            max_working_age_hours  = int(co.get("max_working_age_hours",    48)),
        ),
        sleep_cycle = SleepCycleConfig(
            enabled  = bool(sl.get("enabled",  True)),
            schedule = str(sl.get("schedule",  "02:00")),
            phases   = list(sl.get("phases",   ["deduplication","contradiction_resolution",
                                                 "insight_surfacing","tier_promotion","compression"])),
        ),
    )

    # ── retrieval ──
    ret_raw = raw.get("retrieval", {})
    kw  = ret_raw.get("keyword", {})
    gr  = ret_raw.get("graph", {})
    wi  = ret_raw.get("wiki", {})
    cb  = ret_raw.get("context_budget", {})
    cfg.retrieval = RetrievalConfig(
        keyword = KeywordConfig(
            max_files        = int(kw.get("max_files", 8)),
            body_read_chars  = int(kw.get("body_read_chars", 8000)),
            path_boosts      = dict(kw.get("path_boosts", cfg.retrieval.keyword.path_boosts)),
            filename_match   = dict(kw.get("filename_match", cfg.retrieval.keyword.filename_match)),
            score_components = dict(kw.get("score_components", cfg.retrieval.keyword.score_components)),
            dynamic_caps     = dict(kw.get("dynamic_caps", cfg.retrieval.keyword.dynamic_caps)),
            meeting_from_person = dict(kw.get("meeting_from_person", cfg.retrieval.keyword.meeting_from_person)),
            claims_signal    = dict(kw.get("claims_signal", cfg.retrieval.keyword.claims_signal)),
            scan_exclude     = list(kw.get("scan_exclude", cfg.retrieval.keyword.scan_exclude)),
        ),
        graph = GraphConfig(
            depth                        = int(gr.get("depth", 2)),
            hop_decay                    = float(gr.get("hop_decay", 0.6)),
            threshold                    = float(gr.get("threshold", 0.12)),
            max_graph_extra_files        = int(gr.get("max_graph_extra_files", 3)),
            high_activation_threshold    = float(gr.get("high_activation_threshold", 0.50)),
            related_activation_threshold = float(gr.get("related_activation_threshold", 0.25)),
        ),
        wiki = WikiRetrievalConfig(
            max_pages           = int(wi.get("max_pages", 4)),
            proper_noun_boost   = float(wi.get("proper_noun_boost", 3.0)),
            max_count_per_token = int(wi.get("max_count_per_token", 5)),
            use_qmd             = bool(wi.get("use_qmd", True)),
            qmd_collection      = str(wi.get("qmd_collection", "wiki")),
        ),
        context_budget = ContextBudgetConfig(
            max_total_chars          = int(cb.get("max_total_chars", 80000)),
            wiki_page_truncate_chars = int(cb.get("wiki_page_truncate_chars", 3500)),
            graph_block_max_high     = int(cb.get("graph_block_max_high", 6)),
            graph_block_max_related  = int(cb.get("graph_block_max_related", 4)),
        ),
        synonyms = dict(ret_raw.get("synonyms", {})),
    )

    # ── domain bundles ──
    bundles_raw = raw.get("domain_bundles", [])
    cfg.domain_bundles = [
        DomainBundle(
            name        = b.get("name", ""),
            description = b.get("description", ""),
            triggers    = [str(t) for t in b.get("triggers", [])],
            patterns    = [str(p) for p in b.get("patterns", [])],
        )
        for b in bundles_raw if b.get("name") and b.get("patterns")
    ]

    # ── wiki ──
    w_raw = raw.get("wiki", {})
    cfg.wiki = WikiConfig(
        topics = list(w_raw.get("topics", cfg.wiki.topics)),
        ingest = dict(w_raw.get("ingest", cfg.wiki.ingest)),
    )

    # ── deep_work ──
    dw_raw = raw.get("deep_work", {})
    sp_raw = dw_raw.get("specialists", {})
    # Start from defaults, overlay with yaml values
    specialists = dict(cfg.deep_work.specialists)
    for key, overrides in sp_raw.items():
        if key in specialists:
            specialists[key] = {**specialists[key], **overrides}
        else:
            specialists[key] = overrides
    cfg.deep_work = DeepWorkConfig(
        enabled          = bool(dw_raw.get("enabled",          True)),
        specialist_model = str(dw_raw.get("specialist_model",  cfg.models.haiku)),
        routing_model    = str(dw_raw.get("routing_model",     cfg.models.haiku)),
        synthesis_model  = str(dw_raw.get("synthesis_model",   cfg.models.primary)),
        specialists      = specialists,
    )

    # ── dashboard ──
    da_raw = raw.get("dashboard", {})
    cfg.dashboard = DashboardConfig(
        enabled         = bool(da_raw.get("enabled", True)),
        refresh_seconds = int(da_raw.get("refresh_seconds", 30)),
        show_pillars    = dict(da_raw.get("show_pillars", cfg.dashboard.show_pillars)),
    )

    # ── system_prompt ──
    sp = raw.get("system_prompt", {})
    cfg.system_prompt = SystemPromptConfig(
        user_description = str(sp.get("user_description", "")),
        user_tone        = str(sp.get("user_tone", "Direct, execution-focused.")),
        always_load      = list(sp.get("always_load", ["CLAUDE.md", "preferences.md"])),
        calendar_globs   = list(sp.get("calendar_globs", ["calendar*.md", "**/calendar*.md"])),
        instructions     = dict(sp.get("instructions", {})),
    )

    # ── chat ──
    ch_raw = raw.get("chat", {})
    cfg.chat = ChatConfig(
        backend   = str(ch_raw.get("backend",   "api")),
        cli_bin   = ch_raw.get("cli_bin",   None) or None,
        cli_model = ch_raw.get("cli_model", None) or None,
    )

    return cfg


def _e(env_key: str, fallback: Any) -> Any:
    """Return env var value if set, else fallback."""
    v = os.environ.get(env_key)
    if v is None:
        return fallback
    # Type-coerce to match fallback type
    if isinstance(fallback, bool):
        return v.lower() in ("1", "true", "yes")
    if isinstance(fallback, int):
        try:
            return int(v)
        except ValueError:
            return fallback
    if isinstance(fallback, float):
        try:
            return float(v)
        except ValueError:
            return fallback
    return v


def get_config() -> EngramConfig:
    """Alias for load_config() — returns cached singleton after first call."""
    return load_config()
