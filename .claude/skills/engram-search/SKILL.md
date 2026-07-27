---
name: engram-search
description: Efficient navigation of an engram-data knowledge folder — curated wiki, weighted entity graph, contradictions/open-questions log, and raw memory files. USE WHEN the user asks about people, accounts, decisions, organizational relationships, contradictions, open questions, recent context, or cross-account connections from their personal/work knowledge base. Common triggers include "what do we know about X", "who does X report to", "show me tensions about Y", "recent decisions on Z", "find connections between A and B", "what's our latest thinking on…". Apply this skill even when the user does not explicitly say "engram" — if the answer lives in that folder, prefer the tiered strategy here over greppping the whole tree.
---

# engram-search

Engram is a personal knowledge system that compiles a user's email, documents, transcripts, and decisions into four queryable layers:

1. A **curated wiki** of canonical pages, organised by topic.
2. A **weighted entity graph** (`graph.json`) — entities + relationship edges.
3. A **tensions log** — historical contradictions (`contradictions.json`) and open questions (`open_questions.json`).
4. **Raw memory files** — emails, transcripts, decisions, accounts, weekly notes, dailies.

The corpus is large (thousands of entities, often hundreds to thousands of files). Searching everything for every question is expensive and noisy. This skill teaches a tiered strategy: hit the most-curated layer first, escalate only when needed, and cite what you used.

## Step 0 — Resolve paths from config

Engram stores its paths in `~/.engram/config.yaml`. Read this first instead of hardcoding — different users (and the same user across machines) put their data in different places.

```bash
python3 -c "
import yaml, pathlib
cfg = yaml.safe_load(open(pathlib.Path.home()/'.engram/config.yaml'))
p = cfg.get('paths', {})
print('MEMORY:', p.get('memory_path'))
print('WIKI:  ', p.get('wiki_path'))
"
```

Throughout the rest of this skill, paths are written as `$MEMORY` and `$WIKI` — substitute the real paths from the config when running commands. (They aren't real shell variables unless you `export` them.)

If `yaml` isn't installed (`ModuleNotFoundError: No module named 'yaml'`), fall back to grep:
```bash
grep -E "^\s+(memory_path|wiki_path):" ~/.engram/config.yaml
```

## The tiered strategy

| Layer | When to use | How | Cost |
|---|---|---|---|
| **Wiki** | Topical recall, "what do we know about X" | Read `$WIKI/<topic>/<page>.md` | Cheap, curated, low-noise |
| **Graph** | Relationship questions, "who/how connected" | `python3` over `$MEMORY/graph.json` | Cheap, single-file load |
| **Tensions** | "What's been disputed / disagreed / left open" | `python3` over `contradictions.json` + `open_questions.json` | Cheap, small data — won't answer most questions but illuminates the ones it does |
| **Raw files** | Specific email/decision content, fine-grained recency | Targeted `grep -r` with output budgets | Expensive — keep scopes narrow |

Don't skip layers. Escalate to the next layer only when the prior layer doesn't answer. Most questions resolve from wiki + graph alone.

## Layer 1: Wiki — start here

`$WIKI/` is the curated, deduplicated answer to "what do we know." Pages are organised by topic (accounts, people, products, decisions, etc.). Browse like a normal markdown tree.

```bash
ls "$WIKI"                                  # see top-level topics
find "$WIKI" -name "*.md" | head -30        # see pages
find "$WIKI" -iname "*volvo*" -name "*.md"  # find by entity in filename
grep -rli "freemium" "$WIKI" --include="*.md" | head -10   # content match
```

Then `Read` the top 2–3 hits. If a wiki page answers the question, **stop and cite**. Don't keep searching just because more files exist.

## Layer 2: Graph — entity relationships

`$MEMORY/graph.json` shape:

```json
{
  "entities": { "<entity_id>": { "id": "...", "name": "...", "type": "account|person|product|...", "sources": [...], ... }, ... },
  "edges":    [ { "from": "<id>", "to": "<id>", "type": "reports_to|enables|works_for|...", "weight": 0.0-1.0, "confidence": 0.0-1.0, "sources": [...] }, ... ]
}
```

`entities` is a **dict keyed by id** (e.g. `account:volvo`, `person:leo_sei`). `edges` is a **list**. Sources reference paths under `MEMORY/...` — useful for citation.

### Pattern A — find an entity by partial name

```python
python3 - <<'PY'
import json
g = json.load(open('/PATH/TO/MEMORY/graph.json'))
needle = 'volvo'
hits = [(eid, e['name'], e.get('type')) for eid, e in g['entities'].items()
        if needle.lower() in e.get('name','').lower()]
for h in hits[:10]: print(h)
PY
```

### Pattern B — list edges touching an entity (sorted by weight)

```python
python3 - <<'PY'
import json
g = json.load(open('/PATH/TO/MEMORY/graph.json'))
eid = 'account:volvo'
ents = g['entities']
nbrs = []
for e in g['edges']:
    if eid not in (e['from'], e['to']): continue
    other = e['to'] if e['from'] == eid else e['from']
    nbrs.append((e['type'], other, e.get('weight', 0), e.get('confidence', 0), e.get('sources', [])))
nbrs.sort(key=lambda x: -x[2])
for typ, other, w, c, srcs in nbrs[:15]:
    name = ents.get(other, {}).get('name', other)
    print(f"  --[{typ}, w={w:.2f}, conf={c:.2f}]--> {name}   sources: {srcs[:2]}")
PY
```

### Pattern C — weak edges between well-connected nodes (creative substrate)

Low-weight edges between high-degree entities are *structurally noteworthy uncertain connections*. Useful for "what cross-account or cross-domain connections exist that aren't obvious".

```python
python3 - <<'PY'
import json
from collections import Counter
g = json.load(open('/PATH/TO/MEMORY/graph.json'))
deg = Counter()
for e in g['edges']:
    deg[e['from']] += 1; deg[e['to']] += 1
weak = []
for e in g['edges']:
    if e.get('weight', 1) <= 0.4 and deg[e['from']] >= 3 and deg[e['to']] >= 3:
        a = g['entities'].get(e['from'], {}).get('name', e['from'])
        b = g['entities'].get(e['to'],   {}).get('name', e['to'])
        weak.append((e['weight'], a, e['type'], b, deg[e['from']], deg[e['to']]))
weak.sort()
for w, a, t, b, da, db in weak[:10]:
    print(f"  {a} --[{t}, w={w:.3f}]--> {b}  (deg {da}/{db})")
PY
```

### weight vs confidence

- **weight** — how strongly the system believes the edge is *true*. Low weight = often-rejected, weak signal, or contradicted.
- **confidence** — extraction confidence at the time the edge was created. Low confidence ≠ wrong, just "we weren't sure yet."

Filter by **weight** for reliability. Filter by **weight low + degree high** for creative/lateral connections.

## Layer 3: Tensions

### Contradictions

`$MEMORY/contradictions.json` is either a JSON list or `{"contradictions": [...]}`. Each item:

```json
{
  "id": "...", "type": "factual_conflict|role_conflict",
  "severity": "low|medium|high",
  "claim_A": { "statement": "...", "source": "...", "date": "...", "weight": 0.0-1.0 },
  "claim_B": { "statement": "...", "source": "...", "date": "...", "weight": 0.0-1.0 },
  "status": "unresolved|resolved_A|resolved_B|both_true|both_false|dismissed|superseded"
}
```

Even **resolved** contradictions are useful — the paired claims encode a real tension that existed in the source documents. For creative-thinking or recall questions ("what have we disagreed about Y"), include resolved entries; for "what's the current truth" filter to unresolved or use the `resolved_A`/`resolved_B` label to pick the surviving claim.

```python
python3 - <<'PY'
import json
d = json.load(open('/PATH/TO/MEMORY/contradictions.json'))
items = d if isinstance(d, list) else d.get('contradictions', [])
needle = 'pricing'
hits = [c for c in items
        if needle.lower() in
        ((c.get('claim_A') or {}).get('statement','') + ' ' +
         (c.get('claim_B') or {}).get('statement','')).lower()]
for c in hits[:6]:
    a, b = c.get('claim_A', {}), c.get('claim_B', {})
    print(f"[{c.get('status')}] A: {a.get('statement')}  ({a.get('source')})")
    print(f"             B: {b.get('statement')}  ({b.get('source')})")
PY
```

### Open questions

`$MEMORY/open_questions.json` is similar — list or `{"questions": [...]}`. Each item has `text`, `status`, `priority`, `created_from`, `created_at`.

**Filter carefully**: the harvester sometimes scrapes markdown section headers as "questions". Keep only entries that look like real inquiries.

```python
python3 - <<'PY'
import json, re
d = json.load(open('/PATH/TO/MEMORY/open_questions.json'))
qs = d if isinstance(d, list) else d.get('questions', [])
HINTS = ('?', 'need to', 'unclear', 'confirm', 'does ', 'should ',
         'what ', 'how ', 'why ', 'when ', 'who ')
def real(q):
    t = (q.get('text') or '').lower()
    if not t.strip(): return False
    if t.lstrip('#-*> ').startswith('##'): return False
    return any(h in t for h in HINTS)
for q in [q for q in qs if real(q)][:10]:
    clean = re.sub(r'\*\*(Status|Review):\*\*.*', '', q['text'], flags=re.DOTALL).strip()
    first = next((ln for ln in clean.splitlines() if ln.strip() and not ln.lstrip().startswith('#')), clean)
    print(f"- {first[:200]}   [{q.get('priority')}, from {q.get('created_from')}]")
PY
```

## Layer 4: Raw files — fallback only

`$MEMORY/` typical subdirs:

- `daily/` — daily entries, emails, transcripts (often dated, high-volume, noisy)
- `decisions/` — semi-curated decision records
- `accounts/` — account-specific notes
- `weekly/` — weekly summaries
- `research/` — research outputs
- `episodic/` — session transcripts
- `crystallised/` — long-term high-salience extractions

Descend here only when curated layers don't answer. Use **targeted, scoped** grep with output budgets:

```bash
grep -rli "freemium" "$MEMORY/decisions" --include="*.md" --exclude-dir=_processed | head -10
grep -rli "deeproute" "$MEMORY/accounts"  --include="*.md" --exclude-dir=_processed | head -5
```

Then `Read` the top 2–3 hits, not all of them. If they don't answer, refine the pattern — don't widen the net.

**Avoid** `grep -r ... "$MEMORY"` without subdir scoping. That scans tens of thousands of files and floods context with noise.

## Efficiency rules

1. **Budget output.** `head -10` or `head -50` after every grep/find. You rarely need every match — if the top hits don't answer, refine the pattern.
2. **Read top hits, not all hits.** After `grep -li`, pick 2–3 by filename relevance and Read those. Don't read 20 files speculatively.
3. **Prefer JSON parsing over grep for the graph.** A Python heredoc against `graph.json` is faster and more correct than grepping the file's text — names get matched fuzzily, edges become first-class objects, you can sort and filter cleanly. Grep on `graph.json` only when you need a string the JSON loader would mangle.
4. **Skip `_processed/`.** The inbox watcher archives processed items there — they're duplicates of what was already ingested. Add `--exclude-dir=_processed` to grep.
5. **Cite source paths.** Always mention the file you got information from (e.g. `WIKI/accounts/volvo.md`, `MEMORY/decisions/q1_2026.md`, or `graph.json` edge sources). Engram's value is grounded knowledge — the source matters as much as the fact.

## Common question shapes

| Shape | Strategy |
|---|---|
| "What do we know about X?" | Wiki search by name → graph neighbours for connections. |
| "Who does X report to?" / org questions | Graph: find entity → list `reports_to` edges → sort by weight. |
| "What's been disputed about Y?" | `contradictions.json` filtered by needle. Don't drop resolved — they encode real tensions. |
| "Find unexpected connections between A and B (or in domain D)" | Weak-edge bridges (Pattern C), or pairs whose only shortest path runs through low-weight edges. |
| "What's our latest thinking on Z?" | Wiki page on Z → `MEMORY/decisions/` recent files → raw daily/research files only if still nothing. |
| "What's pending / unresolved?" | `open_questions.json` (filtered for real inquiries) → `proposals/` if it exists. |

## Common mistakes to avoid

- Loading everything "to be safe". Bigger context isn't better — it's slower and pollutes the answer with stale or off-topic material.
- Grepping `$MEMORY` without subdir scoping. Always narrow to `decisions/`, `accounts/`, etc.
- Treating contradictions as bugs to ignore once resolved. The pairs are creative substrate and historical context — keep them in mind for questions about disagreement or change.
- Forgetting to cite. Source-grounded answers are why engram exists; an answer without sources is indistinguishable from a hallucination.
- Re-grepping when the JSON would answer faster. If the question is structural ("neighbors of X", "edges of type Y"), reach for `graph.json`, not grep.
