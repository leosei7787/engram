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
raw documents (inbox/)  +  calendar.ics  +  outgoing email signals
        │
        ▼ (Python watcher — continuous, no cron needed)
        ├── PII Redaction (email, phone, SSN, custom patterns)
        ├── Email cleaner (HTML strip, reply truncation, marketing drop)
        ├── Tone-of-voice updater (learns user's writing style)
        ├── Calendar extractor (Haiku: events → meeting nodes + edges)
        └── Deadline / signal extractor (Haiku over recent inputs)
        │
        ▼
     wiki/                            MEMORY/
       wiki/                            daily/             ← cleaned inputs
         competition/                   weekly/
         concepts/                      sessions/          ← chat transcripts
         decisions/    ◄── canonical    signals/           ← extracted JSON
         people/         entity store   proposals/         ← review queue
         problems/                      pinned/            ← user-pinned replies
         projects/                      priming/           ← session state
         systems/                       graph.json         ← entity graph
                                        contradictions.json
        │                                  │
        └──────────────────┬──────────────┘
                           │
                  [Dream cycle — nightly]
                  5 phases: dedup → contradictions → insights →
                  tier promotion → compression → session harvest
                           │
                           ▼
              [Active Context Manager — per message]
              1. Wide scan      → top-30 keyword + graph + wiki candidates
              2. Haiku curator  → picks best ≤10 for context window
              3. Drift detect   → skip re-curation when topic is stable
              4. Haiku monitor  → adds / removes files as conversation evolves
                           │
                           ▼
                  LLM response with grounded, actively managed context
```

**Wiki vs MEMORY split:**
- **wiki/** — canonical entity records (people, decisions, projects, concepts, competition, problems, systems). Title Case filenames. The user browses and edits this directly via the dashboard's Browse tab.
- **MEMORY/** — pipeline inputs (cleaned emails, weekly notes, chat transcripts) and runtime state (signal JSON, proposal queue, graph, contradictions, pinned replies, system priming).

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

That's it. Drop `.md`, `.txt`, `.eml`, `.vtt`, `.pdf`, `.docx`, `.pptx`, `.ics`, or `.csv` files into your inbox folder and the watcher compiles them automatically.

---

## Setup wizard

`scripts/engram-init.sh` walks you through:

- Your name, role, and organisation (injected into system prompts)
- Storage paths for memory-store, knowledge-base, and inbox
- Chat backend (`cli` — uses your `claude` installation, or `api` — uses `ANTHROPIC_API_KEY`)

It creates the full folder skeleton and writes `~/.engram/config.yaml` from the example template. Run with `--non-interactive` for CI / headless environments.

### Maintenance scripts

- `scripts/prune-stale-graph.py` — sweeps `MEMORY/graph.json` of source pointers to files that no longer exist on disk. Dry-run by default; `--apply` writes with a timestamped backup.
- `scripts/dedup-proposals.py` — canonicalises person paths and collapses semantically-overlapping proposals in `MEMORY/proposals/index.json`. Dry-run by default; `--apply` to write.
- `scripts/archive-memory-entities.py` — opt-in helper that moves legacy `MEMORY/{accounts,decisions}/` entity stubs into `MEMORY/_archive/<timestamp>/` after the wiki has taken over as canonical. Dry-run by default; `--apply` to move.
- `scripts/extract-deadlines.py` — one-shot AI deadline extraction over recent emails (also auto-runs in the background when fresh email arrives).
- `scripts/bootstrap-tone-of-voice.py` — seeds `MEMORY/context/tone_of_voice.md` from a sample of outgoing emails.
- `scripts/run-dream-cycle.py` — manually trigger the nightly reconsolidation pass (otherwise scheduled by the built-in scheduler thread).
- `scripts/install-pii-guard.sh` — installs a pre-commit hook that blocks commits containing names / emails / accounts matching `~/.engram/scrub_patterns.txt`. See `scripts/check-no-pii.py` and `scripts/scrub_patterns.example.txt` for the watchlist format.

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
| `paths` | memory_path, wiki_path, inbox_src, outputs_path |
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

The dashboard sidebar shows the current context set in real time with controls to pin files (kept across topic changes), remove files, and inject raw documents directly into the context window. Multiple documents can be uploaded or dragged in at once.

---

## Memory system

`engram/memory/` implements neuroscience-inspired primitives:

- **Four-tier hierarchy**: working → episodic → semantic → crystallised
- **Salience scoring**: separate from confidence; modulates decay rate
- **Priority floor**: high-priority entities decay 10× slower (`priority_floor: true`)
- **Retrieval-Induced Strengthening (RIS)**: retrieval itself strengthens memory
- **Forgetting curve**: exponential decay, slower for high-salience facts
- **Contradiction engine**: detects and tracks conflicting claims
- **Rejected-claims registry** (`MEMORY/.rejected_claims.json`): user resolutions persist as ground truths. Cascades across related contradictions. Future extractions check this registry before generating new contradictions.
- **Source-quality filter**: org-structure relations (`reports_to`, `manages`, `ceo_of`...) are only trusted from authoritative sources. Random emails and transcripts can't fabricate reporting lines anymore.
- **Open questions**: surfaces gaps; fed to the curator as priors
- **Session priming**: `session_priming.json` hot-starts activation for recurring entities
- **Reconstructive synthesis**: generates coherent briefings, not concatenated chunks
- **5-phase sleep cycle**: nightly offline consolidation (deduplication → contradiction resolution → insight surfacing → tier promotion → compression), with a hook that also harvests proposals from recent chat sessions
- **Source credibility**: tiered trust from user statements → inferred facts
- **Community detection**: Louvain clustering for related entity groups

### AI-driven signal extraction

Three Haiku-driven extractors keep `MEMORY/signals/` populated; the dashboard reads JSON only, so the request path is free of regex scoring:

- **`engram/memory/signal_extractor.py`** — deadlines pulled from recent emails (`signals/deadlines.json`). Refreshed automatically when new email lands.
- **`engram/memory/calendar_extractor.py`** — parses `calendar.ics`, classifies each event (high-stakes / personal / routine), resolves attendees against the entity graph, detects recurring meetings, and writes meeting nodes + edges into `graph.json`. Past occurrences of a recurring meeting are linked as sources. Refresh via `/api/calendar/refresh` or wait for the watcher's 60-second mtime check.
- **`engram/memory/tone_extractor.py`** — learns the user's writing style from outgoing emails (`MEMORY/context/tone_of_voice.md`). The tone file is auto-loaded on every chat turn.

### Conversation harvesting

`engram/memory/session_harvester.py` logs every chat turn to `MEMORY/sessions/<YYYY-MM>/chat_<id>.md` so past conversations become part of future retrieval, and runs a Haiku pass over each exchange to extract decisions, commitments, facts, and open questions. Each extracted item is queued in `MEMORY/proposals/index.json` for review.

Person/decision proposals target wiki paths (`wiki/people/<Canonical Name>.md`, `wiki/decisions/chat_harvest.md`) so accepted proposals land in the canonical store. Open questions stay in `MEMORY/open_questions.json`; free-form notes stay in `MEMORY/daily/notes/`.

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

The watcher tracks already-processed files by content hash (`.watcher_seen.json`) so re-runs are safe. Files seen on prior runs but still sitting in the inbox are caught up to the `_processed/` archive on the next scan. The dashboard exposes a "Watcher" chip showing live ingest counts and a `/api/watcher-rescan` endpoint to wipe the registry and re-process everything.

### Email noise stripping (`engram/ingest/cleaner.py`)

Raw HTML email is 90%+ CSS, tracking pixels, and marketing junk. The cleaner runs on every file the watcher picks up:

1. **Marketing classifier** — sender-domain + subject-pattern + body-heuristic. Marketing emails are *silently dropped* with a reason logged in `watcher_status.skipped_recent`.
2. **HTML/CSS stripper** — removes `<style>`/`<script>`/`<svg>` blocks, decodes entities, collapses whitespace.
3. **Reply-chain truncation** — cuts everything below the first `On <date> wrote:` boundary so threads don't compound.
4. **Header extraction** — pulls From/Subject/Date into clean markdown frontmatter.

Typical reduction: 40 KB raw HTML email → 2 KB signal. Real measurement on a 1,263-file inbox: **88 % size reduction, 37 marketing emails dropped automatically**. Source mtimes are preserved across cleaning so Recent Activity still shows the original arrival time.

POST `/api/clean-emails` runs the cleaner over the entire `daily/emails/` folder retroactively. A backup folder is created before any destructive change.

Optional local-LLM summarization (off by default):

```bash
export ENGRAM_LOCAL_LLM=ollama
export ENGRAM_LOCAL_LLM_MODEL=llama3.2:3b
```

The cleaner will then pipe each cleaned email through Ollama for a 3-5 bullet summary. Falls back silently if Ollama isn't installed.

### PII Redaction & PII guard

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

For preventing accidental commits of identifying information into the source tree (e.g. real names, account codes, internal codenames), install the pre-commit guard:

```bash
bash scripts/install-pii-guard.sh
# then edit ~/.engram/scrub_patterns.txt — one pattern per line, prefix `re:` for regex
```

The hook scans staged content and refuses commits matching any watchlist entry.

---

## Wiki system

**The wiki is canonical for entity records.** Each topic directory under `wiki/wiki/` (`competition`, `concepts`, `decisions`, `people`, `problems`, `projects`, `systems`) holds one markdown page per entity, with Title Case filenames and Obsidian-compatible `[[wikilinks]]` for cross-referencing.

`engram/wiki/scripts/` provides the ingestion pipeline:

- `sync_and_ingest.sh` — syncs an inbox folder and ingests new files via Claude into wiki pages
- `wiki-create-index-pages.py` — rebuilds `_index.md` per topic
- `wiki-lint-check.py` — validates `[[wikilink]]` references
- `wiki_batch_write.py` — writes a JSON manifest of pages to disk
- `convert-eml-to-md.py` / `convert-vtt-to-md.py` — pre-processors for `.eml` and `.vtt`

Retrieval uses QMD BM25 if available (`engram/retrieval/wiki.py`) and falls back to a stem-aware `_index.md` token scan otherwise.

---

## Dashboard

`python3 engram/dashboard/server.py` starts a local web UI at `http://localhost:7090`.

Four tabs:

- **Chat** (`/`) — streamed responses via Claude CLI or Anthropic API. Includes:
  - Live active-context sidebar (pin / remove / inject raw docs, multi-file upload + drag-drop)
  - Human-style chat interruption: keep typing while a response streams; AI classifies the new message as pivot vs. continuation
  - Per-query Haiku curation rationale streamed into the sidebar before the response
  - Deep Work mode for multi-specialist advisory output
  - Copy-to-clipboard preserves formatting on paste
  - Pin a chat reply (📌) to keep it visible in Top of Mind; click the pin to restore the full conversation
  - The tone-of-voice file is auto-loaded on every turn so replies sound like you

- **Top of Mind** (`/top-of-mind`) — focused executive view, AI-extracted (no request-path regex scoring):
  - **High-stakes events** — meetings classified by Haiku in the calendar extractor, scored by attendees, account refs, recurrence pattern, and time-of-day
  - **Pending proposals** — memory-writes awaiting save / skip from `MEMORY/proposals/index.json`, ranked by salience
  - **Deadlines** — phrases extracted from recent emails by the signal extractor, with click-through to the source
  - **💬 Chat about this** button on every card — opens a chat seeded with that item's context

- **Browse** (`/browse`) — Obsidian-style file tree over the curated wiki content. Shows the seven topic directories under `wiki/wiki/` plus the top-level index. Click any page to read it; toggle edit mode to make changes inline. Backlinks panel shows incoming `[[wikilinks]]`.

- **Engram Health** (`/health`) — three-pillar dashboard for memory-store health, graph entity counts, wiki page counts, sleep cycle status, recent activity (raw inbox arrivals + cleaned MEMORY inputs + wiki edits), pending proposals review, and contradictions / open-questions queues.

---

## Folder structure

```
engram/
  dashboard/       Flask server + single-page UI (Chat, Top of Mind, Browse, Health)
  ingest/          Watcher, redactor, cleaner, ICS parser, compilation pipeline
  memory/          Decay, salience, sleep cycle, graph, signal extractors,
                   session harvester, tone extractor
  retrieval/       Config, keyword scan, graph spread, wiki scan, curator, pipeline
  wiki/            Wiki ingestion scripts and helpers
  export/          Markdown → DOCX / PPTX / PDF converters

MEMORY/           Pipeline inputs + runtime state (not committed)
  daily/             cleaned emails, slack, daily notes
  weekly/            weekly digests
  sessions/          per-session chat transcripts (curator-indexed)
  signals/           AI-extracted JSON (deadlines, calendar, …)
  proposals/         review queue for chat-harvested writes
  pinned/            user-pinned chat replies
  priming/           session_priming.json, hot-start state
  context/           tone_of_voice.md and a small set of long-lived notes
  graph.json         entity graph (people, accounts, meetings, decisions, projects)
  open_questions.json
  contradictions.json
  _archive/          legacy entity folders moved aside post-wiki-consolidation

wiki/             Canonical entity records (not committed)
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
- `pip install -r requirements.txt` — Flask, PyYAML, NetworkX, python-louvain, watchdog, anthropic, pypdf, python-docx, python-pptx
- Claude CLI (`claude`) — required for `cli` backend; install from [claude.ai/code](https://claude.ai/code)
- Or set `ANTHROPIC_API_KEY` and use `backend: api`
- Optional: `qmd` CLI for BM25 wiki retrieval; Ollama for local-LLM email summaries

---

## Relation to Anthropic's Memory & Dreams APIs

engram predates Anthropic's Memory Stores and Dreams APIs and implements a compatible philosophy:

- The **Compile** pillar maps to Anthropic's Memory Stores concept
- The **Dream** pillar implements the same reconsolidation loop as Anthropic Dreams
- The **Retrieve** pillar is a local, latency-optimized alternative to API-based retrieval

When Anthropic's APIs reach GA, engram's modules can serve as the pre-processing and post-processing layer around them.

---

## Status

V3 — Active development. Core memory modules are battle-tested in production. V3 adds the wiki/MEMORY split (wiki canonical for entity records), AI-first signal extraction across deadlines / calendar / tone, conversation harvesting + pinned-answer restore, the Browse tab, and the PII pre-commit guard.

Contributions welcome — especially:
- Adapters for different LLM providers (OpenAI, Gemini, local via Ollama)
- Alternative graph backends (beyond the current JSON flat-file)
- Evaluation harness for retrieval quality

---

## License

MIT
