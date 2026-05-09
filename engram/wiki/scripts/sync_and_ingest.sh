#!/bin/bash
# Sync an inbox folder → raw/inbox, then ingest each new file immediately.
# Uses wiki-ingest-per-note per file — no batch partitioning, no manual steps.
# Runs automatically via launchd (or cron) every 10 minutes.
#
# Configuration — set these environment variables or edit the defaults below:
#   ENGRAM_INBOX_SRC   : source directory to sync from (e.g. OneDrive/ClaudeContext)
#   ENGRAM_WIKI_PATH   : root of the knowledge-base-wiki directory
#   ENGRAM_CLAUDE_BIN  : path to the claude CLI binary
#   ENGRAM_MODEL       : Claude model to use (default: claude-sonnet-4-5)

set -euo pipefail

CLAUDE_CONTEXT="${ENGRAM_INBOX_SRC:-/path/to/your/inbox}"
WIKI_ROOT="${ENGRAM_WIKI_PATH:-/path/to/knowledge-base-wiki}"
INBOX="$WIKI_ROOT/raw/inbox"
IMPORT_DIR="$WIKI_ROOT/.import"
LOG="$WIKI_ROOT/logs/sync.log"
LOCKFILE="$IMPORT_DIR/sync.lock"
CLAUDE_BIN="${ENGRAM_CLAUDE_BIN:-$(which claude)}"
MODEL="${ENGRAM_MODEL:-claude-sonnet-4-5}"

mkdir -p "$INBOX" "$IMPORT_DIR" "$(dirname "$LOG")"

# ── Lock: prevent overlapping runs ──────────────────────────────────────────
if [ -f "$LOCKFILE" ]; then
  LOCK_PID=$(cat "$LOCKFILE" 2>/dev/null || echo "")
  if [ -n "$LOCK_PID" ] && kill -0 "$LOCK_PID" 2>/dev/null; then
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Already running (PID $LOCK_PID) — skipping" >> "$LOG"
    exit 0
  fi
  # Stale lock — remove it
  rm -f "$LOCKFILE"
fi
echo $$ > "$LOCKFILE"
trap 'rm -f "$LOCKFILE"' EXIT

# ── Sync: OneDrive ClaudeContext → raw/inbox ─────────────────────────────────
CHANGES=$(rsync -a \
  --exclude="_processed/" \
  --include="*.md" \
  --exclude="*" \
  --itemize-changes \
  "$CLAUDE_CONTEXT/" "$INBOX/")

NEW_FILES=$(echo "$CHANGES" | grep "^>f" | awk '{print $2}' | grep -v '^$' || true)
NEW_COUNT=$(echo "$NEW_FILES" | grep -c "." 2>/dev/null || true)

TS=$(date "+%Y-%m-%d %H:%M:%S")
echo "[$TS] rsync done — $NEW_COUNT new/updated file(s)" >> "$LOG"

[ -z "$NEW_FILES" ] && exit 0

# ── Per-file ingest ───────────────────────────────────────────────────────────
# Each file gets its own Claude session via wiki-ingest-per-note.
# Logs go to a timestamped batch-log so finalize can merge them.
STAMP=$(date '+%Y%m%d-%H%M%S')
BATCH_LOG_NAME="batch-log-auto-${STAMP}.jsonl"

echo "[$TS] Ingesting $NEW_COUNT file(s) → $BATCH_LOG_NAME" >> "$LOG"

cd "$WIKI_ROOT"

while IFS= read -r FILE; do
  [ -z "$FILE" ] && continue
  FILEPATH="raw/inbox/$FILE"

  # Skip if file no longer exists (rsync race)
  [ -f "$WIKI_ROOT/$FILEPATH" ] || { echo "[$(date '+%Y-%m-%d %H:%M:%S')] Skipping missing: $FILEPATH" >> "$LOG"; continue; }

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Ingesting: $FILEPATH" >> "$LOG"

  "$CLAUDE_BIN" --model "$MODEL" -p \
    "Invoke \`wiki-ingest-per-note\` before processing. Write session logs to .import/${BATCH_LOG_NAME}. Use session number 99. Then ingest this file: ${FILEPATH}" \
    >> "$LOG" 2>&1 || true

  echo "[$(date '+%Y-%m-%d %H:%M:%S')] Done: $FILEPATH" >> "$LOG"
done <<< "$NEW_FILES"

# ── Finalize: merge batch log → wiki/log.jsonl, rebuild indexes ───────────────
echo "[$(date '+%Y-%m-%d %H:%M:%S')] Finalizing…" >> "$LOG"

"$CLAUDE_BIN" --model "$MODEL" -p \
  "Invoke \`wiki-finalize-ingest\` before doing anything. Finalize the ingest: merge all batch logs from .import/ into wiki/log.jsonl and rebuild all topic indexes." \
  >> "$LOG" 2>&1 || true

echo "[$(date '+%Y-%m-%d %H:%M:%S')] Sync+ingest complete" >> "$LOG"
