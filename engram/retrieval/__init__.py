"""
engram.retrieval — Zero-latency multi-stage context assembly.

Retrieval pipeline stages:
  1. Keyword scan   — fast BM25-style file scoring
  2. Graph spread   — spreading activation over entity graph
  3. Wiki scan      — index-based wiki page retrieval
  4. Context budget — salience + decay ranked context selection
"""
