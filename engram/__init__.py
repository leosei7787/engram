"""
engram — Compiled Knowledge with Continuous Reconsolidation

Three-pillar architecture:
  1. Compile   — structured wiki + knowledge graph from raw documents
  2. Dream     — nightly sleep cycle for reconsolidation and refinement
  3. Retrieve  — zero-latency multi-stage context assembly at query time

Configure via environment variables (all optional):
  ENGRAM_MEMORY_PATH   : path to memory store  (default: ./memory-store)
  ENGRAM_WIKI_PATH     : path to wiki root      (default: ./wiki)
  ENGRAM_MODEL         : Claude model for LLM calls
  ENGRAM_INBOX_SRC     : source folder for sync_and_ingest.sh
"""

__version__ = "0.1.0"
