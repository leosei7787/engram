#!/usr/bin/env bash
# Partitions un-ingested notes into batch files for parallel import sessions.
#
# Usage:
#   bash scripts/wiki-create-import-batches.sh [--max-files-per-batch N] [--force] [--help]
#
# Options:
#   --max-files-per-batch N   Maximum number of files per batch (default: 10)
#   --force                   Remove existing batch/log files before running
#   --help                    Print this help and exit
#
# Output files:
#   .import/batch-import-1.txt, .import/batch-import-2.txt, …
#   Each file contains one file path per line.
#
# Exit codes:
#   0  Success (batches created)
#   1  Invalid argument
#   2  Existing batch or log files found (use --force to override)
#   3  Nothing to ingest (no new notes found)
#
# Machine-readable summary line (always last):
#   RESULT: total=<N> new=<N> already_imported=<N> batches=<N> max_files_per_batch=<N> status=<ready|empty>
set -euo pipefail

usage() {
    grep '^#' "$0" | grep -v '^#!/' | sed 's/^# \{0,1\}//'
    exit 0
}

MAX_FILES_PER_BATCH=10
FORCE=false
while [[ $# -gt 0 ]]; do
    case "$1" in
        --max-files-per-batch) MAX_FILES_PER_BATCH="$2"; shift 2 ;;
        --force)    FORCE=true; shift ;;
        --help|-h)  usage ;;
        *) echo "ERROR: Unknown argument: $1" >&2; echo "Run with --help for usage." >&2; exit 1 ;;
    esac
done

NOTES_DIR="raw"
LOG="wiki/log.jsonl"
IMPORT_DIR=".import"

existing_batches=( $IMPORT_DIR/batch-import-*.txt )
existing_logs=( $IMPORT_DIR/batch-log-*.jsonl )
has_batches=false
has_logs=false
[[ -e "${existing_batches[0]}" ]] && has_batches=true
[[ -e "${existing_logs[0]}" ]]   && has_logs=true

if $has_batches || $has_logs; then
    if ! $FORCE; then
        echo "ERROR: Existing files found:" >&2
        $has_batches && printf '  %s\n' "${existing_batches[@]}" >&2
        $has_logs    && printf '  %s\n' "${existing_logs[@]}"    >&2
        echo "" >&2
        echo "Execute /wiki:ingest-next-batch to continue processing and" >&2
        echo "execute /wiki:finalize-ingest when all batches are done." >&2
        echo "Or use the option --force to remove the files and continue." >&2
        exit 2
    fi
    $has_batches && rm -f "${existing_batches[@]}"
    $has_logs    && rm -f "${existing_logs[@]}"
fi

log_sources=()
if [[ -f "$LOG" ]]; then
    log_sources+=("$LOG")
    log_status="found"
else
    log_status="missing (no prior imports recorded)"
fi
shopt -s nullglob
batch_logs=( "$IMPORT_DIR"/batch-log-*.jsonl )
shopt -u nullglob
log_sources+=( ${batch_logs[@]+"${batch_logs[@]}"} )

all_files=$(find "$NOTES_DIR" \( -name "*.md" -o -name "*.pdf" -o -name "*.doc" -o -name "*.docx" -o -name "*.txt" -o -name "*.vtt" -o -name "*.eml" \) | sort)

# Filter candidates: include if (a) not in log, or (b) mtime is newer than last import date
_py=$(mktemp /tmp/wiki-filter.XXXXXX.py)
cat > "$_py" << 'PYEOF'
import sys, json, os

log_files = sys.argv[1:]
files_db = set()

for logfile in log_files:
    if not os.path.isfile(logfile):
        continue
    try:
        with open(logfile) as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    fp = d.get('file', '')
                    if fp:
                        files_db.add(fp)
                except Exception:
                    pass
    except Exception:
        pass

for line in sys.stdin:
    fp = line.rstrip('\n')
    if not fp:
        continue
    if fp not in files_db:
        print(fp)
PYEOF

remaining=()
if [[ ${#log_sources[@]} -gt 0 ]]; then
    filtered=$(echo "$all_files" | python3 "$_py" "${log_sources[@]}")
else
    filtered="$all_files"
fi
rm -f "$_py"

while IFS= read -r line; do
    [[ -z "$line" ]] && continue
    remaining+=("$line")
done <<< "$filtered"

scanned=$(printf '%s\n' "$all_files" | grep -c . || true)
total=${#remaining[@]}
already_imported=$(( scanned - total ))
num_batches=$(( (total + MAX_FILES_PER_BATCH - 1) / MAX_FILES_PER_BATCH ))
[[ $total -eq 0 ]] && num_batches=0

echo "wiki/log.jsonl    : $log_status"
echo "Files scanned     : $scanned"
echo "Already imported  : $already_imported"
echo "New (un-ingested) : $total"
echo "Max files/batch   : $MAX_FILES_PER_BATCH (--max-files-per-batch)"
echo "Batches to create : $num_batches"

if [[ $total -eq 0 ]]; then
    echo "Nothing to ingest."
    echo "RESULT: total=0 new=0 already_imported=$already_imported batches=0 max_files_per_batch=$MAX_FILES_PER_BATCH status=empty"
    exit 3
fi

mkdir -p "$IMPORT_DIR"

echo ""
for idx in "${!remaining[@]}"; do
    batch=$(( idx / MAX_FILES_PER_BATCH + 1 ))
    echo "${remaining[$idx]}" >> "$IMPORT_DIR/batch-import-$batch.txt"
    printf "\r  Writing batch files... %d / %d files" $(( idx + 1 )) "$total" >&2
done
printf "\r  Writing batch files... done (%d files in %d batches)\n" "$total" "$num_batches"

echo ""
echo "RESULT: total=$total new=$total already_imported=$already_imported batches=$num_batches max_files_per_batch=$MAX_FILES_PER_BATCH status=ready"
