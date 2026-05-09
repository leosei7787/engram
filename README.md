# engram

> **Compiled knowledge with continuous reconsolidation and active context assembly.**

engram is an open-source system for building a living knowledge base that your LLM assistant reasons from — not by retrieving document chunks at query time, but by maintaining a continuously refined, structured knowledge store with a Haiku-powered Active Context Manager that curates what actually enters the context window.

---

## Why engram?

**The RAG era is ending.** Retrieving chunks at inference time is slow, lossy, and context-hungry. The better model: *compile knowledge once, retrieve structure at query time, curate actively per message.*

engram implements this in three pillars:

| Pillar | What it does |
|---|---|
| **Compile** | Ingests raw documents → structured wiki pages + knowledge graph |
| **Dream** | Nightly reconsolidation: dedup, contradiction resolution, insight surfacing |
| **Retrieve** | Active context assembly: wide scan → Haiku curator → per-message monitor |

This mirrors how biological memory works: continuous encoding, offline consolidation during sleep, fast pattern-matched retrieval with active working memory management.

---

## Architecture

```
raw documents (inbox/)
     │
     ▼ (Python watcher — continuous, no cron needed)
     ├── PII Redaction (email, phone, SSN, custom patterns)
     ▼
wiki/                          memory-store/
  competition/                   graph.json          ← entity graph
  decisions/                     open_questions.json
  people/                        contradictions.json
  projects/                      session_priming.json
  systems/                       communities.json
  ...                            health/
                                   health_snapshot.json
     │                               │
     └──────────────┬───────────────┘
                    │
              [Dream cycle]
              5-phase nightly reconsolidation
                    │
                    ▼
         [Active Context Manager]
         1. Wide scan     → top-30 keyword + graph + wiki candidates
         2. Haiku curator → selects best 10 for context window
         3. Drift detect  → skip re-curation when topic is stable
         4. Haiku monitor → adds / removes files as conversation evolves
                    │
                    ▼
              LLM response with grounded, actively managed context
```

---

## Quick start

```bash
# 1. Clone
git clone https://github.com/leosei7787/engram
cd engram

# 2. Install dependencies
pip install -r requirements.txt

# 3. Run the setup wizard
bash scripts/engram-init.sh
# → Creates ~/.engram/config.yaml and the full folder skeleton

# 4. Start the dashboard
python3 engram/dashboard/server.py
open http://localhost:7090
```

That's it. Drop `.md`, `.txt`, `.eml`, or `.vtt` files into your inbox folder and the watcher compiles them automatically.

---

## Setup wizard

`scripts/engram-init.sh` walks you through:

- Your name, role, and organisation (injected into system prompts)
- Storage paths for memory-store, knowledge-base, and inbox
- Chat backend (`cli` — uses your `claude` installation, or `api` — uses `ANTHROPIC_API_KEY`)

It creates the full folder skeleton and writes `~/.engram/config.yaml` from the example template. Run with `--non-interactive` for CI / headless environments.

---

## Configuration

All settings live in `~/.engram/config.yaml` (or the path in `ENGRAM_CONFIG_FILE`). See `engram_config.example.yaml` for the full reference with inline documentation.

**Override priority** (highest → lowest):
1. `ENGRAM_*` environment variables
2. `~/.engram/config.yaml`
3. Hardcoded defaults in `engram/retrieval/config.py`

Key sections:

| Section | What it controls |
|---|---|
| `identity` | Org name, user name/role, dashboard accent colour |
| `paths` | memory_path, wiki_path, inbox_src |
| `models` | primary, haiku, deep_work, local (Ollama) |
| `memory` | Tier decay rates, salience modifiers, RIS, compression |
| `retrieval` | Keyword scan, graph spread, wiki scan, context budget, synonyms |
| `curator` | Wide scan size, max context files, drift threshold, monitor |
| `ingest` | Watcher interval, extensions, redaction rules |
| `domain_bundles` | Force-load file patterns on trigger words |
| `deep_work` | Multi-agent advisory team (specialists + routing) |
| `dashboard` | Port, refresh rate |

---

## Active Context Manager

The retrieval pipeline runs in three phases per message:

**1. Wide scan** — keyword overlap, graph spreading activation, wiki BM25 (or QMD if installed). Returns up to 30 ranked candidates with short snippets.

**2. Haiku curator** — given the candidates + session priming entities + open questions, Haiku picks the best ≤10 files and explains why. Result is streamed into the sidebar before the main response begins.

**3. Drift detection** — Jaccard similarity between consecutive queries. If the topic hasn't shifted (above threshold), curation is skipped and the existing context is reused. Configurable via `curator.drift_skip_threshold`.

**4. Haiku monitor** — runs after the assistant response. Checks whether the conversation opened new topics and adds/removes files from the active context accordingly.

The dashboard sidebar shows the current context set in real time with controls to pin files (kept across topic changes), remove files, and inject raw documents directly into the context window.

---

## Memory system

`engram/memory/` implements neuroscience-inspired primitives:

- **Four-tier hierarchy**: working → episodic → semantic → crystallised
- **Salience scoring**: separate from confidence; modulates decay rate
- **Priority floor**: high-priority entities decay 10× slower (`priority_floor: true`)
- **Retrieval-Induced Strengthening (RIS)**: retrieval itself strengthens memory
- **Forgetting curve**: exponential decay, slower for high-salience facts
- **Contradiction engine**: detects and tracks conflicting claims
- **Rejected-claims registry** (`MEMORY/.rejected_claims.json`): user resolutions persist as ground truths. Cascades across related contradictions (e.g. "Leo reports_to Mike" auto-dismisses every other "Leo reports_to *" claim). Future extractions check this registry before generating new contradictions.
- **Source-quality filter**: org-structure relations (`reports_to`, `manages`, `ceo_of`...) are only trusted from authoritative sources (`CLAUDE.md`, `context/people.md`, `decisions/*`). Random emails and transcripts can't fabricate reporting lines anymore.
- **Open questions**: surfaces gaps; fed to the curator as priors
- **Session priming**: `session_priming.json` hot-starts activation for recurring entities
- **Reconstructive synthesis**: generates coherent briefings, not concatenated chunks
- **5-phase sleep cycle**: nightly offline consolidation (deduplication → contradiction resolution → insight surfacing → tier promotion → compression)
- **Source credibility**: tiered trust from user statements → inferred facts
- **Community detection**: Louvain clustering for related entity groups

---

## Continuous ingestion

The Python watcher (`engram/ingest/watcher.py`) polls an inbox folder and compiles new documents automatically — no launchd, cron, or systemd setup needed. **It auto-starts with the dashboard** when `paths.inbox_src` is set in config.yaml.

```bash
# Start manually
python3 -m engram.ingest.watcher \
  --inbox ~/engram/inbox \
  --memory ~/engram/memory-store \
  --interval 60
```

The watcher tracks already-processed files by content hash (`.watcher_seen.json`) so re-runs are safe. The dashboard exposes a "Watcher" chip showing live ingest counts and a `/api/watcher-rescan` endpoint to wipe the registry and re-process everything.

### Email noise stripping (`engram/ingest/cleaner.py`)

Raw HTML email is 90%+ CSS, tracking pixels, and marketing junk. The cleaner runs on every file the watcher picks up:

1. **Marketing classifier** — sender-domain + subject-pattern + body-heuristic. Marketing emails (Uber Eats, airline upsells, newsletters) are *silently dropped* with a reason logged in `watcher_status.skipped_recent`.
2. **HTML/CSS stripper** — removes `<style>`/`<script>`/`<svg>` blocks, decodes entities, collapses whitespace.
3. **Reply-chain truncation** — cuts everything below the first `On <date> wrote:` boundary so threads don't compound.
4. **Header extraction** — pulls From/Subject/Date into clean markdown frontmatter.

Typical reduction: 40 KB raw HTML email → 2 KB signal. Real measurement on 1,263-file inbox: **88 % size reduction, 37 marketing emails dropped automatically**.

POST `/api/clean-emails` runs the cleaner over the entire `daily/emails/` folder retroactively. A backup folder is created before any destructive change.

Optional local-LLM summarization (off by default):

```bash
export ENGRAM_LOCAL_LLM=ollama
export ENGRAM_LOCAL_LLM_MODEL=llama3.2:3b
```

The cleaner will then pipe each cleaned email through Ollama for a 3-5 bullet summary. Falls back silently if Ollama isn't installed.

### PII Redaction

Enable pre-compilation redaction to strip sensitive data before it enters the knowledge store:

```yaml
ingest:
  redaction:
    enabled: true
    builtin_rules:
      email:        true   # jane@company.com → [REDACTED:email]
      phone:        true
      ssn:          true
      credit_card:  false
      compensation: false  # €120k / $85,000 — enable for HR docs
    rules:
      - name: internal_code
        pattern: 'PRJ-\d{4,6}'
        label: internal_project_code
```

Redaction is applied at ingest — documents in the knowledge store are already clean.

---

## Wiki system

`engram/wiki/scripts/` provides the ingestion pipeline:

- `sync_and_ingest.sh` — syncs an inbox folder and ingests new files via Claude
- `wiki-create-index-pages.py` — rebuilds `_index.md` per topic
- `wiki-lint-check.py` — validates `[[wikilink]]` references
- `wiki_batch_write.py` — writes a JSON manifest of pages to disk

The wiki format uses **Obsidian-compatible `[[wikilinks]]`** for cross-referencing.

---

## Dashboard

`python3 engram/dashboard/server.py` starts a local web UI at `http://localhost:7090`.

Features:
- **Chat** — streamed responses via Claude CLI or Anthropic API
- **Active context sidebar** — live view of files in context; pin, remove, or add raw documents
- **Context reasoning** — Haiku's curation rationale shown per query
- **Stats panel** — memory-store health, graph entity counts, wiki page counts, sleep cycle status
- **Deep Work mode** — multi-specialist advisory panel for complex strategic questions

---

## Folder structure

```
engram/
  dashboard/       Flask server + single-page UI
  ingest/          Watcher, redactor, compilation pipeline
  memory/          Decay, salience, sleep cycle, graph primitives
  retrieval/       Config, keyword scan, graph spread, wiki, curator
  wiki/            Wiki ingestion scripts

memory-store/      Created by engram-init.sh (not committed)
  episodic/
  semantic/
  crystallised/
  working/
  accounts/
  decisions/
  context/
  sessions/
  priming/
  health/
  logs/

knowledge-base/    Created by engram-init.sh (not committed)
  wiki/
    competition/
    concepts/
    decisions/
    people/
    problems/
    projects/
    systems/
```

---

## Requirements

- Python 3.10+
- `pip install -r requirements.txt` (`flask`, `pyyaml`, `networkx`, `python-louvain`, `watchdog`, `anthropic`)
- Claude CLI (`claude`) — required for `cli` backend; install from [claude.ai/code](https://claude.ai/code)
- Or set `ANTHROPIC_API_KEY` and use `backend: api`

---

## Relation to Anthropic's Memory & Dreams APIs

engram predates Anthropic's Memory Stores and Dreams APIs and implements a compatible philosophy:

- The **Compile** pillar maps to Anthropic's Memory Stores concept
- The **Dream** pillar implements the same reconsolidation loop as Anthropic Dreams
- The **Retrieve** pillar is a local, latency-optimized alternative to API-based retrieval

When Anthropic's APIs reach GA, engram's modules can serve as the pre-processing and post-processing layer around them.

---

## Status

V2 — Active development. Core memory modules are battle-tested in production. Active Context Manager, continuous watcher, and redaction layer are new in V2.

Contributions welcome — especially:
- Adapters for different LLM providers (OpenAI, Gemini, local via Ollama)
- Alternative graph backends (beyond the current JSON flat-file)
- Evaluation harness for retrieval quality

---

## License

MIT
