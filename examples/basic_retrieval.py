"""
examples/basic_retrieval.py — Basic engram retrieval usage

Demonstrates:
  1. Loading config from ~/.engram/config.yaml
  2. Running a memory scan
  3. Using the result to build a system prompt context block

Prerequisites:
  1. Create ~/.engram/config.yaml (copy from engram_config.example.yaml)
  2. Set memory_path and wiki_path in your config
  3. pip install engram pyyaml
"""

from pathlib import Path
from engram.retrieval import memory_scan, load_config

# ── 1. Load config ────────────────────────────────────────────────────────────
# Reads ~/.engram/config.yaml by default.
# Override with: load_config("/path/to/my-config.yaml")
cfg = load_config()

print(f"Loaded config for: {cfg.identity.org_name} / {cfg.identity.user_name}")
print(f"Memory path: {cfg.memory_path}")
print(f"Wiki path:   {cfg.wiki_path}")
print()

# ── 2. Run a memory scan ──────────────────────────────────────────────────────
query = "What's the status of the AMX counter-offer?"

result = memory_scan(query, cfg)

print(f"Query: {query!r}")
print()
print(f"Direct files ({len(result['direct'])}):")
for f in result["direct"]:
    print(f"  {f}")

print(f"\nGraph-surfaced files ({len(result['graph'])}):")
for f in result["graph"]:
    print(f"  {f}")

print(f"\nWiki pages ({len(result['wiki'])}):")
for f in result["wiki"]:
    print(f"  {Path(f).name}")

print(f"\nSuggested (not loaded, {len(result['suggestions'])}):")
for f in result["suggestions"][:3]:
    print(f"  {f}")

# ── 3. Build a context block for LLM injection ───────────────────────────────
# In your actual app, you'd load the file contents and inject them into the
# system prompt alongside result["graph_context"].
if result["graph_context"]:
    print(f"\nGraph context block ({len(result['graph_context'])} chars):")
    print(result["graph_context"][:500] + "..." if len(result["graph_context"]) > 500 else result["graph_context"])
