"""
Session priming vector — recent activations boost future retrieval.

Maintained in memory across queries within a session. Does NOT persist
across sessions (session-scoped, like working memory).
"""
import json
import threading
from pathlib import Path
from typing import Optional


# In-memory priming store: {session_id: PrimingVector}
_PRIMING: dict = {}
_LOCK = threading.Lock()


class PrimingVector:
    """
    Per-session priming vector.

    nodes: dict[node_id → priming_strength]
    Decays per query: priming halves roughly every 2-3 queries.
    """
    def __init__(self, session_id: str, decay_per_query: float = 0.3):
        self.session_id = session_id
        self.nodes: dict[str, float] = {}
        self.decay_per_query = decay_per_query
        self.update_count = 0

    def update(self, activated_nodes: dict):
        """Decay existing, then add new activations."""
        with _LOCK:
            # Decay
            self.nodes = {k: v * (1 - self.decay_per_query) for k, v in self.nodes.items()}
            # Add new (use 0.5 multiplier to avoid runaway)
            for node, strength in (activated_nodes or {}).items():
                self.nodes[node] = max(self.nodes.get(node, 0), float(strength) * 0.5)
            # Prune weak
            self.nodes = {k: v for k, v in self.nodes.items() if v > 0.05}
            self.update_count += 1

    def inject(self, seed_nodes: dict) -> dict:
        """Boost seeds that overlap with priming; soft-inject primed not in seeds."""
        boosted = dict(seed_nodes or {})
        with _LOCK:
            for node, priming in self.nodes.items():
                if node in boosted:
                    boosted[node] = min(1.0, boosted[node] + priming * 0.3)
                else:
                    boosted[node] = priming * 0.15
        return boosted

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "nodes": dict(self.nodes),
            "update_count": self.update_count,
        }


def get_or_create(session_id: str) -> PrimingVector:
    if not session_id:
        session_id = "default"
    with _LOCK:
        if session_id not in _PRIMING:
            _PRIMING[session_id] = PrimingVector(session_id)
        return _PRIMING[session_id]


def get(session_id: str) -> Optional[PrimingVector]:
    with _LOCK:
        return _PRIMING.get(session_id or "default")


def clear(session_id: str):
    with _LOCK:
        _PRIMING.pop(session_id or "default", None)


def snapshot_all(path: Path):
    """Optional: persist all current priming vectors to disk (debugging)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with _LOCK:
        data = {sid: pv.to_dict() for sid, pv in _PRIMING.items()}
    path.write_text(json.dumps(data, indent=2))


def load_snapshot(path: Path):
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
    except Exception:
        return
    with _LOCK:
        for sid, payload in data.items():
            pv = PrimingVector(sid)
            pv.nodes = {k: float(v) for k, v in payload.get("nodes", {}).items()}
            pv.update_count = int(payload.get("update_count", 0))
            _PRIMING[sid] = pv
