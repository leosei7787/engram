#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# engram-init — Bootstrap a new engram instance
# ─────────────────────────────────────────────────────────────────────────────
# Creates the folder structure, installs the config template, and optionally
# starts the dashboard server.
#
# Usage:
#   bash scripts/engram-init.sh
#   bash scripts/engram-init.sh --non-interactive  (use defaults, no prompts)
#
# What this does:
#   1. Asks for your name, org, and desired storage paths
#   2. Creates memory-store/ and knowledge-base/ folder skeletons
#   3. Writes ~/.engram/config.yaml from the example template
#   4. Prints the next steps (start dashboard, run watcher)
#
# Requirements: bash 4+, python3
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
EXAMPLE_CFG="$REPO_DIR/engram_config.example.yaml"
CONFIG_DIR="$HOME/.engram"
CONFIG_FILE="$CONFIG_DIR/config.yaml"

NON_INTERACTIVE=false
for arg in "$@"; do
  [[ "$arg" == "--non-interactive" ]] && NON_INTERACTIVE=true
done

# ── Colors ────────────────────────────────────────────────────────────────────
BOLD=$'\033[1m'; DIM=$'\033[2m'; RESET=$'\033[0m'
GREEN=$'\033[32m'; YELLOW=$'\033[33m'; CYAN=$'\033[36m'; RED=$'\033[31m'

header() { echo; echo "${BOLD}${CYAN}── $1${RESET}"; }
ok()     { echo "${GREEN}✓${RESET} $1"; }
info()   { echo "${DIM}  $1${RESET}"; }
warn()   { echo "${YELLOW}⚠${RESET}  $1"; }
ask()    { read -rp "${BOLD}$1${RESET} " "$2"; }

# ── Banner ────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${CYAN}╔═══════════════════════════════════════╗${RESET}"
echo "${BOLD}${CYAN}║        engram — setup wizard          ║${RESET}"
echo "${BOLD}${CYAN}╚═══════════════════════════════════════╝${RESET}"
echo
echo "This will set up engram in your home directory and create"
echo "the required folder structure."
echo

# ── Step 1: Gather config values ─────────────────────────────────────────────
header "1. Identity"

if $NON_INTERACTIVE; then
  USER_NAME="${ENGRAM_USER_NAME:-Your Name}"
  USER_ROLE="${ENGRAM_USER_ROLE:-}"
  ORG_NAME="${ENGRAM_ORG_NAME:-My Org}"
  USER_EMAIL="${ENGRAM_USER_EMAIL:-}"
else
  ask "Your name:" USER_NAME
  ask "Your role (e.g. CPO, CEO, leave blank):" USER_ROLE
  ask "Your organisation:" ORG_NAME
  ask "Your email (optional):" USER_EMAIL
fi

header "2. Storage paths"
DEFAULT_MEMORY="$HOME/engram/memory-store"
DEFAULT_WIKI="$HOME/engram/knowledge-base"
DEFAULT_INBOX="$HOME/engram/inbox"

if $NON_INTERACTIVE; then
  MEMORY_PATH="${ENGRAM_MEMORY_PATH:-$DEFAULT_MEMORY}"
  WIKI_PATH="${ENGRAM_WIKI_PATH:-$DEFAULT_WIKI}"
  INBOX_PATH="${ENGRAM_INBOX_PATH:-$DEFAULT_INBOX}"
else
  echo "Memory store (graph, episodes, decisions…):"
  ask "  Path [$DEFAULT_MEMORY]:" MEMORY_PATH
  MEMORY_PATH="${MEMORY_PATH:-$DEFAULT_MEMORY}"

  echo "Knowledge base (wiki pages):"
  ask "  Path [$DEFAULT_WIKI]:" WIKI_PATH
  WIKI_PATH="${WIKI_PATH:-$DEFAULT_WIKI}"

  echo "Inbox (drop new documents here to ingest):"
  ask "  Path [$DEFAULT_INBOX]:" INBOX_PATH
  INBOX_PATH="${INBOX_PATH:-$DEFAULT_INBOX}"
fi

header "3. Chat backend"
if $NON_INTERACTIVE; then
  BACKEND="${ENGRAM_BACKEND:-cli}"
else
  echo "How should engram call Claude?"
  echo "  ${BOLD}cli${RESET} — use the Claude CLI (no API key needed, requires claude installed)"
  echo "  ${BOLD}api${RESET} — use ANTHROPIC_API_KEY env var"
  ask "  Backend [cli]:" BACKEND
  BACKEND="${BACKEND:-cli}"
fi

# ── Step 2: Create folder structure ──────────────────────────────────────────
header "4. Creating folders"

DIRS=(
  "$MEMORY_PATH/inbox"
  "$MEMORY_PATH/working"
  "$MEMORY_PATH/episodic"
  "$MEMORY_PATH/semantic"
  "$MEMORY_PATH/crystallised"
  "$MEMORY_PATH/context"
  "$MEMORY_PATH/accounts"
  "$MEMORY_PATH/research"
  "$MEMORY_PATH/decisions"
  "$MEMORY_PATH/weekly"
  "$MEMORY_PATH/daily/emails"
  "$MEMORY_PATH/sessions"
  "$MEMORY_PATH/priming"
  "$MEMORY_PATH/archive"
  "$MEMORY_PATH/health"
  "$MEMORY_PATH/logs"
  "$WIKI_PATH/wiki/competition"
  "$WIKI_PATH/wiki/concepts"
  "$WIKI_PATH/wiki/decisions"
  "$WIKI_PATH/wiki/people"
  "$WIKI_PATH/wiki/problems"
  "$WIKI_PATH/wiki/projects"
  "$WIKI_PATH/wiki/systems"
  "$WIKI_PATH/raw/inbox"
  "$WIKI_PATH/.import"
  "$WIKI_PATH/logs"
  "$INBOX_PATH"
)

for d in "${DIRS[@]}"; do
  mkdir -p "$d"
done
ok "Folder structure created"

# ── Step 3: Write config ──────────────────────────────────────────────────────
header "5. Writing config"

mkdir -p "$CONFIG_DIR"

if [[ -f "$CONFIG_FILE" ]] && ! $NON_INTERACTIVE; then
  echo
  warn "Config already exists at $CONFIG_FILE"
  ask "  Overwrite? [y/N]:" OVERWRITE
  if [[ "${OVERWRITE:-n}" != "y" && "${OVERWRITE:-n}" != "Y" ]]; then
    echo "  Keeping existing config."
    CONFIG_FILE=""
  fi
fi

if [[ -n "$CONFIG_FILE" ]]; then
  # Write config from template, substituting values
  sed \
    -e "s|\"Acme Corp\"|\"$ORG_NAME\"|g" \
    -e "s|\"Jane Smith\"|\"$USER_NAME\"|g" \
    -e "s|\"CPO\"|\"$USER_ROLE\"|g" \
    -e "s|\"jane@acme.com\"|\"$USER_EMAIL\"|g" \
    -e "s|/path/to/memory-store|$MEMORY_PATH|g" \
    -e "s|/path/to/knowledge-base-wiki|$WIKI_PATH|g" \
    -e "s|/path/to/inbox|$INBOX_PATH|g" \
    -e "s|backend: cli|backend: $BACKEND|g" \
    "$EXAMPLE_CFG" > "$CONFIG_FILE"
  ok "Config written → $CONFIG_FILE"
fi

# ── Step 4: Verify Python ─────────────────────────────────────────────────────
header "6. Checking Python environment"

if python3 -c "import engram" 2>/dev/null; then
  ok "engram package is importable"
else
  warn "engram not importable — run from the repo root:"
  info "  pip install -e ."
  info "  or: pip install -r requirements.txt"
fi

if python3 -c "import flask" 2>/dev/null; then
  ok "Flask available (dashboard ready)"
else
  warn "Flask not installed — dashboard won't start until you run:"
  info "  pip install flask"
fi

# ── Done ─────────────────────────────────────────────────────────────────────
echo
echo "${BOLD}${GREEN}All done!${RESET}"
echo
echo "Next steps:"
echo
echo "  ${BOLD}Start the dashboard:${RESET}"
echo "    python3 engram/dashboard/server.py"
echo "    open http://localhost:7090"
echo
echo "  ${BOLD}Start the inbox watcher:${RESET}"
echo "    python3 -m engram.ingest.watcher \\"
echo "      --inbox \"$INBOX_PATH\" \\"
echo "      --memory \"$MEMORY_PATH\" \\"
echo "      --interval 60"
echo
echo "  ${BOLD}Drop documents into your inbox:${RESET}"
echo "    $INBOX_PATH/"
echo
echo "  ${BOLD}Config location:${RESET}"
echo "    $CONFIG_FILE"
echo
