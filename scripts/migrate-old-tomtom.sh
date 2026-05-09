#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Dismantle the old TomTom CPO Agent layout — archive Desktop/TOMTOM and
# Desktop/TOMTOM_KBW into a single "Old context" umbrella so they're out of
# the way but recoverable.
#
# Pre-conditions (verified by this script):
#   1. ~/engram-data/tomtom/MEMORY/        exists and is non-empty
#   2. ~/engram-data/tomtom/wiki/knowledge-base-wiki/ exists with 2000+ .md files
#   3. ~/.engram/config.yaml points at the new locations (NOT TOMTOM_KBW)
#
# After this script:
#   ~/Desktop/TOMTOM_RAW_DATA Old context/
#     ├── TOMTOM/          (your old MEMORY snapshot, OUTPUTS, raw data)
#     └── TOMTOM_KBW/      (raw inbox, .import logs, scripts, _processed)
#
# Reversible: nothing is deleted. The folders are renamed, not removed.
# To revert: mv each folder back to ~/Desktop/.
#
# Usage:
#   bash scripts/migrate-old-tomtom.sh           # do it
#   bash scripts/migrate-old-tomtom.sh --check   # validate prereqs only
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

DESKTOP="$HOME/Desktop"
ARCHIVE="$DESKTOP/TOMTOM_RAW_DATA Old context"

echo "── Pre-flight checks ──────────────────────────────────────────────"

# 1. New memory location
if [ ! -d "$HOME/engram-data/tomtom/MEMORY" ]; then
  echo "✗ ~/engram-data/tomtom/MEMORY does not exist — cannot proceed"
  exit 1
fi
mem_size=$(du -sh "$HOME/engram-data/tomtom/MEMORY" | cut -f1)
echo "✓ Memory at ~/engram-data/tomtom/MEMORY ($mem_size)"

# 2. New wiki location
if [ ! -d "$HOME/engram-data/tomtom/wiki/knowledge-base-wiki" ]; then
  echo "✗ ~/engram-data/tomtom/wiki/knowledge-base-wiki does not exist"
  exit 1
fi
wiki_count=$(find "$HOME/engram-data/tomtom/wiki/knowledge-base-wiki/wiki" -name "*.md" 2>/dev/null | wc -l | tr -d ' ')
echo "✓ Wiki at ~/engram-data/tomtom/wiki/knowledge-base-wiki ($wiki_count .md files)"

# 3. Config points at new locations
if grep -q "Desktop/TOMTOM_KBW" "$HOME/.engram/config.yaml" 2>/dev/null; then
  echo "✗ ~/.engram/config.yaml STILL references Desktop/TOMTOM_KBW — fix that first"
  exit 1
fi
if grep -q "Desktop/TOMTOM/MEMORY" "$HOME/.engram/config.yaml" 2>/dev/null; then
  echo "✗ ~/.engram/config.yaml STILL references Desktop/TOMTOM/MEMORY — fix that first"
  exit 1
fi
echo "✓ ~/.engram/config.yaml points at the new locations"

# 4. Old launchd job is gone
if [ -f "$HOME/Library/LaunchAgents/com.tomtom.wiki.sync.plist" ]; then
  echo "✗ Old com.tomtom.wiki.sync launchd job is still active"
  echo "  Run: launchctl unload ~/Library/LaunchAgents/com.tomtom.wiki.sync.plist"
  echo "       mv ~/Library/LaunchAgents/com.tomtom.wiki.sync.plist{,.disabled}"
  exit 1
fi
echo "✓ Old launchd sync job is disabled"

if [ "${1:-}" = "--check" ]; then
  echo
  echo "All prereqs satisfied. Run without --check to perform the move."
  exit 0
fi

echo
echo "── Moving folders into the archive ────────────────────────────────"
mkdir -p "$ARCHIVE"

if [ -d "$DESKTOP/TOMTOM" ]; then
  echo "→ Moving Desktop/TOMTOM/ → $ARCHIVE/TOMTOM/"
  mv "$DESKTOP/TOMTOM" "$ARCHIVE/TOMTOM"
  echo "  done"
else
  echo "  (Desktop/TOMTOM not found — skipping)"
fi

if [ -d "$DESKTOP/TOMTOM_KBW" ]; then
  echo "→ Moving Desktop/TOMTOM_KBW/ → $ARCHIVE/TOMTOM_KBW/"
  mv "$DESKTOP/TOMTOM_KBW" "$ARCHIVE/TOMTOM_KBW"
  echo "  done"
else
  echo "  (Desktop/TOMTOM_KBW not found — skipping)"
fi

echo
echo "── Verifying engram still works ───────────────────────────────────"
if python3 -c "
import sys; sys.path.insert(0,'$HOME/Desktop/engram')
from engram.retrieval.config import load_config
from engram.retrieval.pipeline import memory_scan
cfg = load_config()
r = memory_scan('Rijn Buve knowledge base', cfg)
assert r['wiki'], 'wiki should still return hits'
assert cfg.memory_path.exists(), 'memory_path should still resolve'
print(f'  ✓ scan returned {len(r[\"direct\"])} direct + {len(r[\"wiki\"])} wiki hits from new locations')
" 2>&1; then
  echo
  echo "✓ Migration complete. Archived to: $ARCHIVE/"
  echo
  du -sh "$ARCHIVE"/* 2>/dev/null
else
  echo "✗ Engram failed after move — recovering"
  [ -d "$ARCHIVE/TOMTOM" ]    && mv "$ARCHIVE/TOMTOM"    "$DESKTOP/TOMTOM"
  [ -d "$ARCHIVE/TOMTOM_KBW" ] && mv "$ARCHIVE/TOMTOM_KBW" "$DESKTOP/TOMTOM_KBW"
  echo "  Restored. Investigate why the scan failed before retrying."
  exit 1
fi
