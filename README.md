# engram

> **Compiled knowledge with continuous reconsolidation and active context assembly.**

engram is an open-source system for building a living knowledge base that your LLM assistant can reason from — not by retrieving documents at query time, but by maintaining a continuously refined, structured knowledge store.

---

## Why engram?

**The RAG era is ending.** Retrieving chunks at inference time is slow, lossy, and context-hungry. The better model: *compile knowledge once, retrieve structure at query time.*

engram implements this in three pillars:

| Pillar | What it does |
|---|---|
| **Compile** | Ingests raw documents → structured wiki pages + knowledge graph |
| **Dream** | Nightly reconsolidation: dedup, contradiction resolution, insight surfacing |
| **Retrieve** | Zero-latency context assembly: keyword scan + graph spread + wiki lookup |

This mirrors how biological memory works: continuous encoding, offline consolidation during sleep, fast pattern-matched retrieval.

---

## Architecture

```
raw documents
     │
     ▼ (sync_and_ingest.sh + Claude)
wiki/                          memory-store/
  competition/                   graph.json          ← entity graph
    _index.md                    open_questions.json
    Waymo.md                     contradictions.json
    Here.md                      communities.json
  decisions/                     health/
    _index.md                      health_snapshot.json
    ...                          priming/
  ...                              session_priming.json
     │                               │
     └──────────────┬───────────────┘
                    │
              [Dream cycle]
              5-phase nightly reconsolidation
                    │
                    ▼
              [Retrieve pipeline]
              1. Keyword scan → ranked files
              2. Graph spread → activated entities
              3. Wiki scan    → relevant compiled pages
              4. Budget       → salience-ranked context window
                    │
                    ▼
              LLM response with grounded context
```

---

## Memory system

`engram/memory/` implements neuroscience-inspired primitives:

- **Four-tier hierarchy**: working → episodic → semantic → crystallised
- **Salience scoring**: separate from confidence; modulates decay rate
- **Retrieval-Induced Strengthening (RIS)**: retrieval itself strengthens memory
- **Forgetting curve**: exponential decay, slower for high-salience facts
- **Contradiction engine**: detects and tracks conflicting claims
- **Open questions**: surfaces gaps proactively
- **Session priming**: hot-starts activation for recurring entities
- **Reconstructive synthesis**: generates coherent briefings, not concatenated chunks
- **5-phase sleep cycle**: nightly offline consolidation (mirrors NREM/REM)
- **Source credibility**: tiered trust from user statements → inferred facts
- **Community detection**: Louvain clustering for related entity groups

---

## Wiki system

`engram/wiki/scripts/` provides the ingestion pipeline:

- `sync_and_ingest.sh` — syncs an inbox folder and ingests new files via Claude
- `wiki-create-index-pages.py` — rebuilds `_index.md` per topic
- `wiki-lint-check.py` — validates `[[wikilink]]` references
- `wiki_batch_write.py` — writes a JSON manifest of pages to disk

The wiki format uses **Obsidian-compatible `[[wikilinks]]`** for cross-referencing.

---

## Configuration

All paths are configurable via environment variables:

| Variable | Default | Description |
|---|---|---|
| `ENGRAM_MEMORY_PATH` | `./memory-store` | Root of the memory store |
| `ENGRAM_WIKI_PATH` | `./wiki` | Root of the knowledge wiki |
| `ENGRAM_MODEL` | `claude-sonnet-4-5` | Claude model for LLM calls |
| `ENGRAM_INBOX_SRC` | *(required)* | Source folder to sync from |
| `ENGRAM_CLAUDE_BIN` | `$(which claude)` | Path to claude CLI |

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/leosei7787/engram
cd engram

# 2. Configure
export ENGRAM_MEMORY_PATH=/path/to/your/memory-store
export ENGRAM_WIKI_PATH=/path/to/your/wiki
export ENGRAM_INBOX_SRC=/path/to/your/inbox

# 3. Run the sync+ingest pipeline
bash engram/wiki/scripts/sync_and_ingest.sh

# 4. Use the memory modules in your app
from engram.memory.decay import apply_decay
from engram.memory.salience import compute_salience
from engram.memory.sleep_cycle import run_sleep_cycle
```

---

## Relation to Anthropic's Memory & Dreams APIs

engram predates Anthropic's [Memory Stores](https://docs.anthropic.com/en/docs/agents-and-tools/memory-stores) and [Dreams](https://docs.anthropic.com/en/docs/agents-and-tools/dreams) APIs (currently in research preview) and implements a compatible philosophy:

- The **Compile** pillar maps to Anthropic's Memory Stores concept
- The **Dream** pillar implements the same reconsolidation loop as Anthropic Dreams
- The **Retrieve** pillar is a local, latency-optimized alternative to API-based retrieval

When Anthropic's APIs reach GA, engram's modules can serve as the pre-processing and post-processing layer around them.

---

## Status

Early release. Core memory modules are battle-tested in production. Retrieval pipeline extraction is in progress.

Contributions welcome — especially:
- Adapters for different LLM providers (OpenAI, Gemini, local)
- Alternative graph backends (beyond the current JSON flat-file)
- Evaluation harness for retrieval quality

---

## License

MIT
