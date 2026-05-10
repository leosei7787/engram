#!/usr/bin/env python3
"""
Pre-commit guard against accidentally committing personally-identifying or
org-specific strings (real names, customer accounts, internal codenames,
email addresses).

How to install as a git pre-commit hook:

    git config --global core.hooksPath ~/.git-hooks
    mkdir -p ~/.git-hooks
    cat > ~/.git-hooks/pre-commit <<'EOF'
    #!/usr/bin/env bash
    cd "$(git rev-parse --show-toplevel)" || exit 0
    if [ -f scripts/check-no-pii.py ]; then
      python scripts/check-no-pii.py || exit $?
    fi
    EOF
    chmod +x ~/.git-hooks/pre-commit

Or per-repo (`./.git/hooks/pre-commit`) if you'd rather not change global
config. Either way, the script reads its watchlist from
``~/.engram/scrub_patterns.txt``. One pattern per line; lines starting with
``#`` are comments. Empty file or missing file = no scanning. Patterns are
case-insensitive substrings by default; prefix with ``re:`` to use a regex.

Bypass for a single commit:  ``git commit --no-verify``  (use sparingly).
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path


WATCHLIST_PATH = Path.home() / ".engram" / "scrub_patterns.txt"


def _load_watchlist() -> list[tuple[re.Pattern, str]]:
    """Return [(compiled_regex, original_pattern_string), ...]."""
    if not WATCHLIST_PATH.exists():
        return []
    out: list[tuple[re.Pattern, str]] = []
    for raw in WATCHLIST_PATH.read_text(errors="ignore").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("re:"):
            try:
                out.append((re.compile(line[3:], re.IGNORECASE), line))
            except re.error:
                continue
        else:
            out.append((re.compile(re.escape(line), re.IGNORECASE), line))
    return out


def _staged_files() -> list[Path]:
    """Files staged for commit, filtered to text-likely extensions."""
    res = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True, check=False,
    )
    paths: list[Path] = []
    text_exts = {".py", ".md", ".sh", ".yaml", ".yml", ".toml", ".json",
                 ".txt", ".html", ".js", ".ts", ".css"}
    for line in res.stdout.strip().splitlines():
        if not line:
            continue
        p = Path(line)
        if p.suffix.lower() in text_exts and p.exists():
            paths.append(p)
    return paths


def _staged_diff_for(path: Path) -> str:
    """Get just the additions (lines starting with '+') for one file's diff.

    We scan additions only — the watchlist is for *new* leaks. Existing
    pre-existing strings in unchanged code are ignored (they're already
    in the repo; flagging them on every commit would be noise).
    """
    res = subprocess.run(
        ["git", "diff", "--cached", "-U0", str(path)],
        capture_output=True, text=True, check=False,
    )
    added: list[str] = []
    for line in res.stdout.splitlines():
        if line.startswith("+++") or line.startswith("---"):
            continue
        if line.startswith("+"):
            added.append(line[1:])
    return "\n".join(added)


def main() -> int:
    rules = _load_watchlist()
    if not rules:
        # No watchlist → no scanning. Quietly succeed so the hook never
        # blocks a fresh checkout where the user hasn't set this up yet.
        return 0

    files = _staged_files()
    if not files:
        return 0

    hits: list[tuple[Path, str, str]] = []   # (file, pattern, sample_line)
    for f in files:
        diff = _staged_diff_for(f)
        if not diff:
            continue
        for rx, original in rules:
            m = rx.search(diff)
            if m:
                # Find the line containing the match for nicer output
                for ln in diff.splitlines():
                    if rx.search(ln):
                        hits.append((f, original, ln.strip()[:120]))
                        break

    if not hits:
        return 0

    print("\n🚫  Pre-commit guard: forbidden pattern(s) found in staged additions\n",
          file=sys.stderr)
    for f, pat, sample in hits:
        print(f"  • {f}", file=sys.stderr)
        print(f"      pattern: {pat}", file=sys.stderr)
        print(f"      line:    {sample}", file=sys.stderr)
        print("", file=sys.stderr)
    print(f"Watchlist: {WATCHLIST_PATH}", file=sys.stderr)
    print("Bypass (use rarely): git commit --no-verify", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
