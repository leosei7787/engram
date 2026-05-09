"""
Salience scoring — separate from confidence.

Salience = how important is this fact to the user right now?
Confidence = how factually certain are we?

Salience modulates decay (high salience decays slower) and ranking
(high salience displaces low-salience items in context budget).
"""
from .schemas import Salience, _now


SALIENCE_MODIFIERS = {
    "is_decision":             0.30,   # tied to a decision event
    "is_risk":                 0.25,   # risk node
    "source_is_user":           0.20,   # the user themselves said this
    "active_deal":             0.15,   # involves an active deal node
    "recent_upload":           0.10,   # uploaded this week
    "contradicts_existing":    0.20,   # contradiction makes it salient
    # "retrieved_N_times" applied dynamically: 0.05 * min(N, 4)
}


def compute_salience(
    base: float = 0.5,
    *,
    is_decision: bool = False,
    is_risk: bool = False,
    source_is_user: bool = False,
    active_deal: bool = False,
    recent_upload: bool = False,
    contradicts_existing: bool = False,
    retrieved_n_times: int = 0,
) -> Salience:
    """Build a Salience object from flags."""
    mods = {}
    if is_decision:           mods["is_decision"] = SALIENCE_MODIFIERS["is_decision"]
    if is_risk:               mods["is_risk"] = SALIENCE_MODIFIERS["is_risk"]
    if source_is_user:         mods["source_is_user"] = SALIENCE_MODIFIERS["source_is_user"]
    if active_deal:           mods["active_deal"] = SALIENCE_MODIFIERS["active_deal"]
    if recent_upload:         mods["recent_upload"] = SALIENCE_MODIFIERS["recent_upload"]
    if contradicts_existing:  mods["contradicts_existing"] = SALIENCE_MODIFIERS["contradicts_existing"]
    if retrieved_n_times > 0:
        mods["retrieved_n_times"] = 0.05 * min(retrieved_n_times, 4)

    computed = max(0.0, min(1.0, base + sum(mods.values())))
    return Salience(base=base, modifiers=mods, computed=computed)


def update_retrieval_modifier(salience_dict: dict, retrieved_n_times: int) -> dict:
    """Update the retrieved_n_times modifier in place; returns updated dict."""
    s = Salience.from_dict(salience_dict)
    s.modifiers["retrieved_n_times"] = 0.05 * min(retrieved_n_times, 4)
    s.computed = max(0.0, min(1.0, s.base + sum(s.modifiers.values())))
    return s.to_dict()


def infer_entity_salience(entity: dict, graph: dict) -> Salience:
    """Heuristic salience for an entity based on its type, sources, edges."""
    etype = (entity.get("type") or "").lower()
    name = (entity.get("name") or "").lower()
    sources = entity.get("sources") or []

    base = 0.5
    is_decision = etype in ("decision", "decisionrecord", "commitment")
    is_risk = etype == "risk" or "risk" in name
    source_is_user = any("/sessions/" in s for s in sources)
    active_deal = etype in ("deal", "opportunity", "contract", "partnership")
    recent_upload = bool(sources and "/daily/" in sources[0])

    return compute_salience(
        base=base,
        is_decision=is_decision,
        is_risk=is_risk,
        source_is_user=source_is_user,
        active_deal=active_deal,
        recent_upload=recent_upload,
        retrieved_n_times=int(entity.get("activation_count", 0)),
    )


def effective_decay_rate(base_decay_rate: float, salience_computed: float) -> float:
    """High-salience facts decay 70% slower than low-salience."""
    return base_decay_rate * (1.0 - salience_computed * 0.7)
