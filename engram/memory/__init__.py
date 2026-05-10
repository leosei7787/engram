"""
engram.memory — Living Cognitive Memory System
==============================================

Implements neuroscience-inspired memory primitives for LLM assistants:

- Four-tier memory hierarchy (working / episodic / semantic / crystallised)
- Salience scoring (importance, separate from confidence)
- Retrieval-Induced Strengthening (RIS) — retrieval itself strengthens memory
- Contradiction detection and tracking engine
- Open questions with proactive surfacing
- Session priming vectors
- Reconstructive synthesis (not retrieval concatenation)
- Compression-based forgetting
- 5-phase nightly sleep / reconsolidation cycle
- Source credibility hierarchy
- Memory communities (Louvain graph clustering)
- Memory health visibility

Each subsystem is in its own module. The host application wires them via
the path configuration below (controlled by ENGRAM_MEMORY_PATH env var).

Configure:
  export ENGRAM_MEMORY_PATH=/path/to/your/memory/store
"""

import os
from pathlib import Path

# All memory artefacts live under ENGRAM_MEMORY_PATH.
# Default: ./memory-store relative to the project root.
_DEFAULT_MEMORY = Path(__file__).parent.parent.parent / "memory-store"
MEMORY_PATH = Path(os.environ.get("ENGRAM_MEMORY_PATH", str(_DEFAULT_MEMORY)))

# Standard file layout within the memory store
GRAPH_FILE          = MEMORY_PATH / "graph.json"
OPEN_QUESTIONS_FILE = MEMORY_PATH / "open_questions.json"
CONTRADICTIONS_FILE = MEMORY_PATH / "contradictions.json"
COMMUNITIES_FILE    = MEMORY_PATH / "communities.json"
PRIMING_FILE        = MEMORY_PATH / "priming" / "session_priming.json"
HEALTH_FILE         = MEMORY_PATH / "health" / "health_snapshot.json"
AUDIT_LOG           = MEMORY_PATH / "health" / "audit_log.jsonl"
SLEEP_STATUS        = MEMORY_PATH / ".sleep_cycle_status.json"
COST_LOG            = MEMORY_PATH / "health" / "cost_log.jsonl"

# Backwards-compatible V3_ aliases. sleep_cycle.py and any older code that
# pre-dates the rename still imports these by their previous names. New code
# should use the un-prefixed names above.
V3_GRAPH           = GRAPH_FILE
V3_OPEN_QUESTIONS  = OPEN_QUESTIONS_FILE
V3_CONTRADICTIONS  = CONTRADICTIONS_FILE
V3_COMMUNITIES     = COMMUNITIES_FILE
V3_PRIMING         = PRIMING_FILE
V3_HEALTH          = HEALTH_FILE
V3_AUDIT_LOG       = AUDIT_LOG
V3_SLEEP_STATUS    = SLEEP_STATUS
V3_COST_LOG        = COST_LOG

__all__ = [
    "MEMORY_PATH",
    "GRAPH_FILE", "OPEN_QUESTIONS_FILE", "CONTRADICTIONS_FILE",
    "COMMUNITIES_FILE", "PRIMING_FILE", "HEALTH_FILE",
    "AUDIT_LOG", "SLEEP_STATUS", "COST_LOG",
    "V3_GRAPH", "V3_OPEN_QUESTIONS", "V3_CONTRADICTIONS",
    "V3_COMMUNITIES", "V3_PRIMING", "V3_HEALTH",
    "V3_AUDIT_LOG", "V3_SLEEP_STATUS", "V3_COST_LOG",
]
