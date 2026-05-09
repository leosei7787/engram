"""
engram.wiki — Compiled knowledge wiki tooling.

The wiki is a structured directory of Markdown files organized by topic,
generated from raw documents via Claude-powered ingestion scripts.

Structure:
  wiki/
    <topic>/
      _index.md        — topic index with [[wikilinks]]
      <page>.md        — compiled knowledge page

Scripts in engram/wiki/scripts/:
  sync_and_ingest.sh   — sync inbox → ingest new files via Claude
  wiki_batch_write.py  — write a JSON manifest of pages to disk
  wiki-create-index-pages.py — rebuild _index.md files per topic
  wiki-lint-check.py   — validate internal [[wikilink]] references
"""
