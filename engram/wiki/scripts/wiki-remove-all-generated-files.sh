#!/usr/bin/env bash
set -euo pipefail

FORCE=false

usage() {
  cat <<EOF
Usage: $(basename "$0") [--force] [--help]

Clears all content from raw/-/converted/, wiki/ and .import/, preserving .gitkeep files.

Options:
  --force   Skip confirmation prompt
  --help    Show this help message
EOF
}

for arg in "$@"; do
  case "$arg" in
    --help) usage; exit 0 ;;
    --force) FORCE=true ;;
    *) echo "Unknown option: $arg" >&2; usage >&2; exit 1 ;;
  esac
done

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

if [[ "$FORCE" != true ]]; then
  printf "This will delete all files in raw/-/converted/, wiki/ and .import/ (keeping .gitkeep). Continue? [y/N] "
  read -r -n 1 answer
  echo
  if [[ "$answer" != "y" ]]; then
    echo "Aborted."
    exit 0
  fi
fi

find "$ROOT/wiki" -mindepth 1 ! -name ".gitkeep" -delete
find "$ROOT/.import" -mindepth 1 ! -name ".gitkeep" -delete
find "$ROOT/raw" -name converted -type d -exec rm -rf "{}" \;
"$SCRIPT_DIR/qmd-reset-collections.sh" --force

echo "Done; comverted/, wiki/ and .import/ removed; QMD reset."
echo "Run prompt 'ingest new notes' in the LLM to recreate the wiki."
