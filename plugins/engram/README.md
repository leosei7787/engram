# engram plugin

Adds the `engram-search` skill, which teaches Claude a tiered strategy for navigating an engram-data knowledge folder: curated wiki → weighted entity graph → contradictions/open-questions log → raw memory files. Source paths are resolved from `~/.engram/config.yaml`, not hardcoded.

## What it does

Activates automatically when a question is answerable from the user's personal/work knowledge base — e.g. "what do we know about X", "who reports to Y", "what's been disputed about Z", "find connections between A and B", "what's our latest thinking on…".

It biases toward the most-curated layer first (wiki, then graph) and only escalates to raw files when needed, with output budgets and citation requirements throughout.

## Install

### As a local plugin (Claude Code or Cowork)

From the engram repo:

```sh
cp -R plugins/engram ~/.claude/plugins/local/engram
```

Then enable it in your Claude Code settings (or, in Cowork, add it from the plugin picker pointed at `~/.claude/plugins/local/engram`).

### Just the skill, globally

If you want the skill without the plugin wrapper:

```sh
mkdir -p ~/.claude/skills/engram-search
cp plugins/engram/skills/engram-search/SKILL.md ~/.claude/skills/engram-search/
```

## Prerequisites

- A populated `~/.engram/config.yaml` with `paths.memory_path` and `paths.wiki_path`.
- Filesystem access to those paths from the Claude session.

## Layout

```
plugins/engram/
├── .claude-plugin/plugin.json
├── README.md
└── skills/
    └── engram-search/SKILL.md
```
