"""
Model routing — prefer local for routine ops, cloud for synthesis quality.
Cost tracking with daily budget alerts.
"""
import json
import time
from pathlib import Path


# Operation → (preferred_local_model, fallback_cloud_model_or_None)
ROUTING = {
    # Always local — fast, deterministic-ish
    "query_classification":   ("llama3.1:latest",  None),
    "ingestion_classify":     ("llama3.1:latest",  None),
    "memory_proposal_detect": ("llama3.1:latest",  None),
    "open_question_extract":  ("llama3.1:latest",  None),

    # Local preferred, cloud fallback
    "graph_extraction":       ("qwen2.5:14b",      "claude-haiku-4-5"),
    "episodic_harvest":       ("qwen2.5:14b",      "claude-haiku-4-5"),
    "graph_enrichment":       ("qwen2.5:14b",      None),
    "graph_synthesis":        ("qwen2.5:14b",      None),
    "ingestion_condense":     ("qwen2.5:14b",      "claude-haiku-4-5"),
    "reconstructive_synthesis": ("qwen2.5:14b",    "claude-haiku-4-5"),
    "community_label":        ("qwen2.5:14b",      None),

    # Always cloud
    "memory_compression":     (None,               "claude-haiku-4-5"),
    "contradiction_resolve":  (None,               "claude-haiku-4-5"),
    "crystallised_summary":   (None,               "claude-haiku-4-5"),

    # Primary chat
    "primary_worker":         (None,               "claude-opus-4-7"),
}


COST_CONTROLS = {
    "cloud_budget_per_day_usd":  2.00,
    "compression_max_per_night": 10,
    "haiku_fallback_timeout_s":  45,
    "alert_threshold":           0.80,   # alert at 80% of budget
}


# Per-token costs in USD per 1K tokens. Input and output are billed differently.
# These are estimates calibrated to public Anthropic pricing — actual billed
# amounts come from console.anthropic.com / the Admin API and may differ
# slightly (e.g. prompt caching, batch discounts).
COST_RATES = {
    # model_name : {"input": $/1k input tokens, "output": $/1k output tokens}
    "claude-opus-4-7":         {"input": 0.015,  "output": 0.075 },
    "claude-opus-4":           {"input": 0.015,  "output": 0.075 },
    "claude-sonnet-4-6":       {"input": 0.003,  "output": 0.015 },
    "claude-sonnet-4-5":       {"input": 0.003,  "output": 0.015 },
    "claude-3-5-sonnet":       {"input": 0.003,  "output": 0.015 },
    "claude-haiku-4-5":        {"input": 0.0008, "output": 0.004 },
    "claude-haiku-4-5-20251001": {"input": 0.0008, "output": 0.004 },
    "claude-3-5-haiku":        {"input": 0.0008, "output": 0.004 },
}


def _resolve_rate(model: str) -> dict:
    """Find best-matching rate by prefix. Default to sonnet pricing."""
    if not model:
        return {"input": 0.003, "output": 0.015}
    m = model.lower()
    if m in COST_RATES:
        return COST_RATES[m]
    # Prefix-match
    for k, v in COST_RATES.items():
        if m.startswith(k) or k.startswith(m):
            return v
    # Family fallback
    if "opus" in m:    return {"input": 0.015,  "output": 0.075}
    if "haiku" in m:   return {"input": 0.0008, "output": 0.004}
    return {"input": 0.003, "output": 0.015}   # sonnet default


def select_model(operation: str, available_local_models: list = None,
                 force_cloud: bool = False) -> tuple[str, str]:
    """
    Returns (model_id, kind) where kind ∈ {"local", "cloud"}.
    Falls back to cloud if local unavailable.
    """
    if operation not in ROUTING:
        return ("llama3.1:latest", "local")

    local, cloud = ROUTING[operation]
    available = set(available_local_models or [])

    if force_cloud and cloud:
        return (cloud, "cloud")

    if local:
        # Match prefix (e.g. qwen2.5:14b in available)
        for m in available:
            if local in m or m.startswith(local.split(":")[0]):
                return (m, "local")
        # Local preferred but not available
        if cloud:
            return (cloud, "cloud")
        # No fallback — try first available
        if available:
            return (next(iter(available)), "local")

    if cloud:
        return (cloud, "cloud")

    return ("llama3.1:latest", "local")


# ─── Cost tracking ────────────────────────────────────────────────────────
def log_cost(cost_log_path: Path, model: str, operation: str,
             input_tokens: int = 0, output_tokens: int = 0,
             estimated_cost: float = None):
    """Append one line to the cost log (JSONL). Uses input/output rates separately."""
    if estimated_cost is None:
        rate = _resolve_rate(model)
        estimated_cost = (
            (input_tokens  / 1000.0) * rate["input"]
          + (output_tokens / 1000.0) * rate["output"]
        )

    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "date": time.strftime("%Y-%m-%d"),
        "model": model,
        "operation": operation,
        "input_tokens":  int(input_tokens),
        "output_tokens": int(output_tokens),
        "cost_usd": round(float(estimated_cost), 6),
    }
    cost_log_path.parent.mkdir(parents=True, exist_ok=True)
    with cost_log_path.open("a") as f:
        f.write(json.dumps(entry) + "\n")


def daily_cost_summary(cost_log_path: Path, days: int = 7) -> dict:
    """Aggregate cost log for the last `days` days."""
    if not cost_log_path.exists():
        return {"total_usd": 0.0, "by_model": {}, "by_operation": {}, "by_day": {}}
    cutoff = time.time() - days * 86400
    by_model: dict = {}
    by_op: dict = {}
    by_day: dict = {}
    total = 0.0
    n = 0
    with cost_log_path.open() as f:
        for line in f:
            try:
                e = json.loads(line)
            except Exception:
                continue
            try:
                ts = time.mktime(time.strptime(e.get("ts", "")[:19], "%Y-%m-%dT%H:%M:%S"))
            except Exception:
                continue
            if ts < cutoff:
                continue
            cost = float(e.get("cost_usd", 0))
            total += cost
            n += 1
            m = e.get("model", "?")
            op = e.get("operation", "?")
            d = e.get("date", "?")
            by_model.setdefault(m, {"calls": 0, "cost": 0.0})
            by_model[m]["calls"] += 1
            by_model[m]["cost"] += cost
            by_op.setdefault(op, {"calls": 0, "cost": 0.0})
            by_op[op]["calls"] += 1
            by_op[op]["cost"] += cost
            by_day[d] = by_day.get(d, 0.0) + cost

    return {
        "days":   days,
        "total_usd": round(total, 4),
        "calls":  n,
        "by_model":    {k: {**v, "cost": round(v["cost"], 4)} for k, v in by_model.items()},
        "by_operation": {k: {**v, "cost": round(v["cost"], 4)} for k, v in by_op.items()},
        "by_day":  {k: round(v, 4) for k, v in by_day.items()},
    }


def check_budget_status(cost_log_path: Path) -> dict:
    """Returns {status: ok|warn|over, today_usd, budget, pct}."""
    today = time.strftime("%Y-%m-%d")
    summary = daily_cost_summary(cost_log_path, days=1)
    today_cost = summary["by_day"].get(today, 0.0)
    budget = COST_CONTROLS["cloud_budget_per_day_usd"]
    pct = today_cost / budget if budget > 0 else 0
    status = "ok"
    if pct > 1.0:    status = "over"
    elif pct > COST_CONTROLS["alert_threshold"]: status = "warn"
    return {
        "status":   status,
        "today_usd": round(today_cost, 4),
        "budget":   budget,
        "pct":      round(pct * 100, 1),
    }
