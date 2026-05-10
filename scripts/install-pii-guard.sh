#!/usr/bin/env bash
# Install the pre-commit guard against PII leaks.
#
# This sets up a git hook that runs scripts/check-no-pii.py before every
# commit. The hook reads patterns from ~/.engram/scrub_patterns.txt — fill
# that file in with names / emails / customer accounts you don't want
# appearing in committed source.
#
# Per-repo install (this engram clone only):
#   bash scripts/install-pii-guard.sh
#
# Global install (all repos under ~/.git-hooks/):
#   bash scripts/install-pii-guard.sh --global
set -euo pipefail

GLOBAL=0
[[ "${1:-}" == "--global" ]] && GLOBAL=1

REPO_ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"

if [[ "$GLOBAL" == "1" ]]; then
  HOOKS_DIR="$HOME/.git-hooks"
  echo "Installing globally to $HOOKS_DIR"
else
  HOOKS_DIR="$REPO_ROOT/.git/hooks"
  echo "Installing per-repo at $HOOKS_DIR"
fi
mkdir -p "$HOOKS_DIR"

cat > "$HOOKS_DIR/pre-commit" <<'EOF'
#!/usr/bin/env bash
# Auto-installed by scripts/install-pii-guard.sh.
# Edit ~/.engram/scrub_patterns.txt to manage your watchlist.
ROOT="$(git rev-parse --show-toplevel 2>/dev/null || pwd)"
SCRIPT="$ROOT/scripts/check-no-pii.py"
if [[ -f "$SCRIPT" ]]; then
  python3 "$SCRIPT" || exit $?
fi
EOF
chmod +x "$HOOKS_DIR/pre-commit"

if [[ "$GLOBAL" == "1" ]]; then
  git config --global core.hooksPath "$HOOKS_DIR"
  echo "  ✓ git config --global core.hooksPath = $HOOKS_DIR"
fi

# Make sure the watchlist exists (empty by default — user fills in).
WATCHLIST="$HOME/.engram/scrub_patterns.txt"
if [[ ! -f "$WATCHLIST" ]]; then
  mkdir -p "$HOME/.engram"
  cp "$REPO_ROOT/scripts/scrub_patterns.example.txt" "$WATCHLIST"
  echo "  ✓ created $WATCHLIST (edit it with your real watchlist)"
else
  echo "  ⊘ $WATCHLIST already exists — left untouched"
fi

echo
echo "Pre-commit guard installed."
echo "Edit $WATCHLIST to add names / emails / accounts you don't want committed."
echo "Bypass once: git commit --no-verify"
