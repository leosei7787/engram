#!/usr/bin/env bash
# Removes all QMD collections and wipes the index database.
set -euo pipefail

FORCE=false
for arg in "$@"; do
  case "$arg" in
    --force) FORCE=true ;;
  esac
done

QMD_DB_DIR="${HOME}/.cache/qmd"

if [[ "$FORCE" != true ]]; then
  echo "This will remove all QMD collections and delete the index database at:"
  echo "  ${QMD_DB_DIR}/index.sqlite"
  echo ""
  printf "Continue? [y/N] "
  read -r -n 1 confirm
  echo
  [[ "${confirm}" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 0; }
fi

echo ""
echo "=== Removing collections ==="
collections=$(qmd collection list 2>/dev/null | awk '/^[^ ]/ && NR>1 {print $1}')

if [[ -z "$collections" ]]; then
  echo "  No collections registered."
else
  while IFS= read -r name; do
    echo "  [remove] $name"
    qmd collection remove "$name"
  done <<< "$collections"
fi

echo ""
echo "=== Deleting index database ==="
for f in "${QMD_DB_DIR}/index.sqlite" \
          "${QMD_DB_DIR}/index.sqlite-shm" \
          "${QMD_DB_DIR}/index.sqlite-wal"; do
  if [[ -f "$f" ]]; then
    rm "$f"
    echo "  deleted: $f"
  fi
done

echo ""
echo "Done. Run scripts/qmd-sync-collections.sh to re-register collections."
