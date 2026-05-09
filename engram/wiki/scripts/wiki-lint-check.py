#!/usr/bin/env python3
"""
wiki-lint-check.py — Scan Markdown files for broken internal and external links.

Output is structured JSON designed for AI consumption:
  {
    "broken_links": [
      {
        "file": "relative/path/to/file.md",
        "line": 12,
        "type": "wikilink|markdown|image",
        "raw": "[[target]]",
        "target": "target",
        "reason": "file not found"
      },
      ...
    ],
    "summary": { "files_checked": N, "links_checked": N, "broken": N, "skipped_external": N },
    "errors": [ "...", ... ]
  }

Usage:
  python3 check-broken-links.py [OPTIONS] [ROOT_DIR]

Options:
  --help, -h          Show this help message and exit
  --external          Also check HTTP/HTTPS links (slow; requires network)
  --timeout N         Timeout in seconds for external requests (default: 5)
  --include-images    Also check embedded images (![[...]])
  --format text|json  Output format: 'text' for human-readable, 'json' for AI (default: json)
  --quiet             Suppress progress messages on stderr

ROOT_DIR defaults to the directory containing this script's parent.
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
from pathlib import Path

# ---------------------------------------------------------------------------
# Link extraction
# ---------------------------------------------------------------------------

# Matches [[target]], [[target|alias]], [[target#anchor|alias]] — Obsidian wikilinks.
# Captures only the target portion (before any | or # delimiter).
# A single ']' that is NOT followed by another ']' is allowed inside the target
# (e.g. [[example [1] of a note]]), while ']]' ends the link.
# '\|' (backslash-pipe) is also treated as a separator, as required inside markdown tables.
RE_WIKILINK = re.compile(r'(?<!!)\[\[((?:[^\]|#\n\\]|\\(?!\|)|\](?!\]))+)')
# Matches ![[target]] — Obsidian image embeds (same bracket rule applies)
RE_IMAGE_EMBED = re.compile(r'!\[\[((?:[^\]|#\n\\]|\\(?!\|)|\](?!\]))+)')
# Matches [text](target) — standard markdown links; skips http/https separately
RE_MDLINK = re.compile(r'(?<!!)\[[^\]]*\]\(([^)#\n]+?)(?:#[^)]*)?\)')
# Matches ![alt](target) — standard markdown images
RE_MDIMAGE = re.compile(r'!\[[^\]]*\]\(([^)#\n]+?)(?:#[^)]*)?\)')


CURLY_TO_STRAIGHT = str.maketrans({
    '‘': "'",  # '  LEFT SINGLE QUOTATION MARK
    '’': "'",  # '  RIGHT SINGLE QUOTATION MARK
    '“': '"',  # "  LEFT DOUBLE QUOTATION MARK
    '”': '"',  # "  RIGHT DOUBLE QUOTATION MARK
})
_CURLY_RE = re.compile(r'[‘’“”]')


def is_external(target: str) -> bool:
    return target.startswith(("http://", "https://", "ftp://", "mailto:"))


def strip_frontmatter(content: str) -> tuple[str, int]:
    """
    If content starts with a YAML frontmatter block (--- ... ---), replace it
    with blank lines so line numbers are preserved. Returns (modified_content, fm_end_line).
    """
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return content, 0
    for i, line in enumerate(lines[1:], 1):
        if line.strip() in ("---", "..."):
            blanked = [""] * (i + 1)
            return "\n".join(blanked) + "\n" + "".join(lines[i + 1:]), i + 1
    return content, 0  # unclosed frontmatter — don't strip


def extract_links(content: str, include_images: bool, skip_frontmatter: bool = False):
    """Yield (line_number, type, raw_match, target) for every link in content."""
    if skip_frontmatter:
        content, _ = strip_frontmatter(content)
    lines = content.splitlines()
    for lineno, line in enumerate(lines, 1):
        # Obsidian wikilinks
        for m in RE_WIKILINK.finditer(line):
            target = m.group(1).strip()
            if line[m.end():].startswith('|(broken link)') or line[m.end():].startswith('\\|(broken link)'):
                continue  # already marked by --remove-broken-links; skip
            yield lineno, "wikilink", m.group(0), target
        # Obsidian image embeds
        if include_images:
            for m in RE_IMAGE_EMBED.finditer(line):
                target = m.group(1).strip()
                yield lineno, "image", m.group(0), target
        # Standard markdown links
        for m in RE_MDLINK.finditer(line):
            target = m.group(1).strip()
            yield lineno, "markdown", m.group(0), target
        # Standard markdown images
        if include_images:
            for m in RE_MDIMAGE.finditer(line):
                target = m.group(1).strip()
                yield lineno, "image", m.group(0), target


# ---------------------------------------------------------------------------
# Resolution helpers
# ---------------------------------------------------------------------------

KNOWN_EXTENSIONS = {".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".webp"}

# Characters that are often replaced by '_' when a title becomes a filename.
_PROBLEMATIC_CHARS = re.compile(r'[_:?*|"<>\\]')


def normalize_name(name: str) -> str:
    """Canonical form for fuzzy matching.

    Replaces '_' and chars typically substituted with '_' in filenames with a
    space, then collapses whitespace. This makes '[[foo: bar]]', '[[foo bar]]',
    and the file 'foo_ bar.md' all map to the same key.
    """
    return re.sub(r'\s+', ' ', _PROBLEMATIC_CHARS.sub(' ', name)).strip().lower()


def build_normalized_index(root: Path) -> dict[str, list[Path]]:
    """Map normalize_name(stem) -> list of .md paths, for fuzzy wikilink matching."""
    index: dict[str, list[Path]] = {}
    for p in root.rglob("*.md"):
        if should_skip_md(p, root):
            continue
        key = normalize_name(p.stem)
        index.setdefault(key, []).append(p)
    return index


def find_normalized_match(target: str, root: Path, norm_index: dict[str, list[Path]]) -> "str | None":
    """Try to match a broken wikilink target by normalizing problematic characters.

    Returns the corrected link text (stem, or relative path if the original
    target included a directory) if exactly one file matches, else None.
    """
    candidate = Path(target)
    has_known_ext = candidate.suffix.lower() in KNOWN_EXTENSIONS
    name = candidate.stem if has_known_ext else candidate.name
    key = normalize_name(name)
    if not key:
        return None
    # If the target includes a directory prefix, restrict the search to that subdir.
    if candidate.parent != Path("."):
        subdir = root / candidate.parent
        if subdir.is_dir():
            for p in subdir.glob("*.md"):
                if normalize_name(p.stem) == key:
                    return str(candidate.parent / p.stem)
        return None
    # Vault-wide fuzzy match — only accept a unique result to avoid false fixes.
    matches = norm_index.get(key, [])
    if len(matches) == 1:
        return matches[0].stem
    return None


def fix_wikilinks_in_file(file_path: Path, fixes: list) -> int:
    """Replace wikilink targets in-place; returns the number of substitutions made."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for old_target, new_target in fixes:
        pattern = re.compile(r'(?<!!)\[\[' + re.escape(old_target) + r'(?=[\]|#\n])')
        content, n = pattern.subn(f'[[{new_target}', content)
        count += n
    if count:
        file_path.write_text(content, encoding="utf-8")
    return count


def replace_mdlink_target_in_file(file_path: Path, old_target: str, new_target: str) -> int:
    """Replace a markdown link target in-place; returns substitution count."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(r'(?<!!)\[([^\]]*)\]\(' + re.escape(old_target) + r'(?:#[^)]*)?\)')
    new_content, n = pattern.subn(lambda m: f'[{m.group(1)}]({new_target})', content)
    if n:
        file_path.write_text(new_content, encoding="utf-8")
    return n


def resolve_wikilink(target: str, root: Path, all_md_stems: dict[str, list[Path]]) -> bool:
    """
    Resolve an Obsidian wikilink against the vault root.
    Wikilinks can be:
      - a bare filename stem:    people/rijn-buve  →  <root>/people/rijn-buve.md
      - a full path (no ext):    wiki/concepts/foo →  <root>/wiki/concepts/foo.md
      - a full path with ext:    wiki/concepts/foo.md
    Also checks .png/.jpg/.jpeg/.gif/.svg/.pdf for embedded files.
    If the target has no recognized extension, .md is assumed (Obsidian default).
    Returns True if the target resolves to an existing file.
    """
    candidate = Path(target)
    has_known_ext = candidate.suffix.lower() in KNOWN_EXTENSIONS

    # Try exact path first
    if (root / target).exists():
        return True

    # If no recognized extension, try appending .md and other known types.
    # This handles bare names like "my-note", paths like "wiki/concepts/foo",
    # and names with dots that aren't file extensions (e.g. "2024.05.15").
    if not has_known_ext:
        for ext in (".md", ".png", ".jpg", ".jpeg", ".gif", ".svg", ".pdf", ".webp"):
            if (root / (target + ext)).exists():
                return True

    # Fuzzy match: bare stem against all known markdown files.
    # Use the full name as the lookup key when there is no recognized extension,
    # so that "v1.2" matches "v1.2.md" rather than looking up "v1".
    stem = candidate.stem if has_known_ext else candidate.name
    if stem in all_md_stems:
        return True

    return False


def resolve_wikilink_to_path(target: str, root: Path, stem_index: dict[str, list[Path]]) -> "Path | None":
    """Resolve a wikilink target to an actual Path, or None if unresolvable or ambiguous."""
    candidate = Path(target)
    has_known_ext = candidate.suffix.lower() in KNOWN_EXTENSIONS

    exact = root / target
    if exact.exists():
        return exact

    if not has_known_ext:
        exact_md = root / (target + ".md")
        if exact_md.exists():
            return exact_md

    stem = candidate.stem if has_known_ext else candidate.name
    matches = stem_index.get(stem, [])
    if len(matches) == 1:
        return matches[0]
    return None  # not found or ambiguous


def resolve_mdlink(target: str, source_file: Path, root: Path, all_md_stems: dict[str, list[Path]]) -> bool:
    """Resolve a standard markdown relative link."""
    if is_external(target):
        return True  # handled separately

    # URL-decode basic percent-encoding (e.g. spaces as %20)
    try:
        from urllib.parse import unquote
        target = unquote(target)
    except Exception:
        pass

    p = (source_file.parent / target).resolve()
    if p.exists():
        return True

    # Also try treating as root-relative
    p2 = (root / target).resolve()
    if p2.exists():
        return True

    return False


def check_external(url: str, timeout: int) -> tuple[bool, str]:
    """Return (ok, reason). Performs a HEAD request, falls back to GET."""
    try:
        req = urllib.request.Request(url, method="HEAD")
        req.add_header("User-Agent", "check-broken-links/1.0")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status < 400, f"HTTP {resp.status}"
    except urllib.error.HTTPError as e:
        if e.code == 405:
            # HEAD not allowed — try GET
            try:
                req2 = urllib.request.Request(url, method="GET")
                req2.add_header("User-Agent", "check-broken-links/1.0")
                with urllib.request.urlopen(req2, timeout=timeout) as resp:
                    return resp.status < 400, f"HTTP {resp.status}"
            except Exception as e2:
                return False, str(e2)
        return False, f"HTTP {e.code} {e.reason}"
    except urllib.error.URLError as e:
        return False, str(e.reason)
    except Exception as e:
        return False, str(e)


# ---------------------------------------------------------------------------
# Main logic
# ---------------------------------------------------------------------------

def mark_broken_wikilinks_in_file(file_path: Path, targets: list) -> int:
    """
    For each target in `targets`, rewrite every matching wikilink in file_path:
      [[target]]           -> [[target|(broken link) target]]
      [[target|text]]      -> [[target|(broken link) text]]
      [[target#heading]]   -> [[target#heading|(broken link) target]]
      [[target#heading|text]] -> [[target#heading|(broken link) text]]
    Returns the total number of substitutions made.
    """
    content = file_path.read_text(encoding="utf-8", errors="replace")
    count = 0
    for target in targets:
        pattern = re.compile(
            r'(?<!!)\[\[(' + re.escape(target) + r')(#[^|\\\]]*)?(?:\\?(\|[^\]\n]*))?\]\]'
        )
        def _replacer(m, _t=target):
            heading = m.group(2) or ""
            alias_part = m.group(3)          # includes leading '|', or None
            display = alias_part[1:] if alias_part else _t
            return f'[[{_t}{heading}|(broken link) {display}]]'
        content, n = pattern.subn(_replacer, content)
        count += n
    if count:
        file_path.write_text(content, encoding="utf-8")
    return count


def truncate_path(path: str, max_len: int = 40, prefix_len: int = 20) -> str:
    """Truncate path to max_len chars: first prefix_len chars + '...' + tail."""
    if len(path) <= max_len:
        return path
    tail_len = max_len - prefix_len - 3
    if tail_len <= 0:
        return path[:max_len]
    return path[:prefix_len] + "..." + path[-tail_len:]


def delete_wikilink_in_file(file_path: Path, target: str):
    # Remove [[target...]] from file, collapsing surrounding whitespace.
    # If the resulting line is empty or just a bare list marker, the whole line is dropped.
    # Returns (changed, removed_linenos) where removed_linenos are 1-indexed lines that
    # were fully deleted (so callers can adjust line numbers in sibling entries).
    content = file_path.read_text(encoding='utf-8', errors='replace')
    link_pat = re.compile(
        r'( ?)(?<!!)\[\[' + re.escape(target) + r'(?:#[^|\\\]]*)?(?:\\?\|[^\]]*)?\]\]( ?)'
    )
    # Bare: optional indent + optional list marker + optional empty quote pair + whitespace.
    # Quote pairs: "" '' and their curly variants (via \u escapes)
    bare_pat = re.compile(
        r'^\s*(?:[-*+]|\d+\.)?\s*(?:""|\'\'|\u201c\u201d|\u2018\u2019)?\s*$'
    )

    lines = content.splitlines(keepends=True)
    new_lines = []
    changed = False
    removed_linenos: list[int] = []

    for lineno, line in enumerate(lines, 1):
        if not link_pat.search(line):
            new_lines.append(line)
            continue
        def _repl(m):
            return ' ' if (m.group(1) and m.group(2)) else ''
        new_line = link_pat.sub(_repl, line)
        if bare_pat.match(new_line.rstrip('\r\n')):
            changed = True
            removed_linenos.append(lineno)
        else:
            if new_line != line:
                changed = True
            new_lines.append(new_line)

    if changed:
        file_path.write_text(''.join(new_lines), encoding='utf-8')
    return changed, removed_linenos


def delink_wikilink_in_file(file_path: Path, target: str) -> int:
    """Strip [[ ]] brackets from wikilinks, leaving plain text.
    Path prefix and extension are also removed when no alias is present.
    [[x/y/z]] → z,  [[x/y/z|alias]] → alias
    [[target#heading]] → target,  [[target#heading|alias]] → alias
    Returns substitution count."""
    content = file_path.read_text(encoding='utf-8', errors='replace')
    pattern = re.compile(
        r'(?<!!)\[\[' + re.escape(target) + r'(?:#[^|\\\]]*)?(?:\\?\|([^\]]*))?\]\]'
    )
    stem = Path(target).stem  # strips any path prefix and extension: x/y/z.md → z
    def _repl(m, _stem=stem):
        alias = m.group(1)
        return alias if alias is not None else _stem
    new_content, n = pattern.subn(_repl, content)
    if n:
        file_path.write_text(new_content, encoding='utf-8')
    return n


def delete_mdlink_in_file(file_path: Path, target: str):
    """Remove [text](target) standard markdown links from file.
    If the resulting line is bare, the whole line is dropped.
    Returns (changed, removed_linenos)."""
    content = file_path.read_text(encoding='utf-8', errors='replace')
    link_pat = re.compile(
        r'( ?)(?<!!)\[[^\]]*\]\(' + re.escape(target) + r'(?:#[^)]*)?\)( ?)'
    )
    bare_pat = re.compile(
        r'^\s*(?:[-*+]|\d+\.)?\s*(?:""|\'\'|\u201c\u201d|\u2018\u2019)?\s*$'
    )

    lines = content.splitlines(keepends=True)
    new_lines = []
    changed = False
    removed_linenos: list[int] = []

    for lineno, line in enumerate(lines, 1):
        if not link_pat.search(line):
            new_lines.append(line)
            continue
        def _repl(m):
            return ' ' if (m.group(1) and m.group(2)) else ''
        new_line = link_pat.sub(_repl, line)
        if bare_pat.match(new_line.rstrip('\r\n')):
            changed = True
            removed_linenos.append(lineno)
        else:
            if new_line != line:
                changed = True
            new_lines.append(new_line)

    if changed:
        file_path.write_text(''.join(new_lines), encoding='utf-8')
    return changed, removed_linenos


def mark_as_broken_link_in_file(file_path: Path, target: str) -> bool:
    """Rewrite [[target]] → [[broken-link|target]] in file. Returns True if changed."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    pattern = re.compile(
        r'(?<!!)\[\[(' + re.escape(target) + r')(#[^|\\\]]*)?(?:\\?(\|[^\]\n]*))?\]\]'
    )
    def _replacer(m, _t=target):
        alias_part = m.group(3)
        display = alias_part[1:] if alias_part else _t
        return f'[[broken-link|{display}]]'
    new_content, n = pattern.subn(_replacer, content)
    if n:
        file_path.write_text(new_content, encoding="utf-8")
        return True
    return False


def fix_curly_quotes(root: Path, quiet: bool) -> tuple[int, int, int]:
    """Rename .md files whose stems contain curly quotes and fix curly quotes in all link targets.
    Returns (renamed_files, link_files_changed, links_changed)."""
    # Pass 1: rename files whose stems contain curly quotes
    renamed = 0
    for p in sorted(root.rglob("*.md")):
        if should_skip_md(p, root):
            continue
        if not _CURLY_RE.search(p.stem):
            continue
        new_stem = p.stem.translate(CURLY_TO_STRAIGHT)
        new_path = p.parent / (new_stem + ".md")
        if new_path.exists():
            if not quiet:
                print(f"  Cannot rename {p.name}: {new_path.name} already exists", file=sys.stderr)
            continue
        p.rename(new_path)
        renamed += 1
        if not quiet:
            print(f"  Renamed: {p.name} → {new_path.name}", file=sys.stderr)

    # Pass 2: fix curly quotes inside link targets across all .md files
    link_files = 0
    link_count = 0
    for md_file in sorted(root.rglob("*.md")):
        if should_skip_md(md_file, root):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if not _CURLY_RE.search(content):
            continue  # fast-path: no curly quotes anywhere in this file

        counter = [0]

        def _fix_wiki(m, _c=counter):
            inner = m.group(1)
            fixed = inner.translate(CURLY_TO_STRAIGHT)
            if fixed != inner:
                _c[0] += 1
                return f'[[{fixed}'
            return m.group(0)

        def _fix_img(m, _c=counter):
            inner = m.group(1)
            fixed = inner.translate(CURLY_TO_STRAIGHT)
            if fixed != inner:
                _c[0] += 1
                return f'![[{fixed}'
            return m.group(0)

        def _fix_md(m, _c=counter):
            target = m.group(1)
            if is_external(target) or not _CURLY_RE.search(target):
                return m.group(0)
            fixed = target.translate(CURLY_TO_STRAIGHT)
            _c[0] += 1
            offset = m.start(1) - m.start(0)
            full = m.group(0)
            return full[:offset] + fixed + full[offset + len(target):]

        new_content = RE_WIKILINK.sub(_fix_wiki, content)
        new_content = RE_IMAGE_EMBED.sub(_fix_img, new_content)
        new_content = RE_MDLINK.sub(_fix_md, new_content)
        new_content = RE_MDIMAGE.sub(_fix_md, new_content)

        if counter[0]:
            md_file.write_text(new_content, encoding="utf-8")
            link_files += 1
            link_count += counter[0]

    return renamed, link_files, link_count


def should_skip_md(path: Path, root: Path) -> bool:
    """Return True if this .md file should be excluded from scanning."""
    rel = path.relative_to(root)
    # Skip files inside hidden directories (any parent component starting with '.')
    if any(part.startswith(".") for part in rel.parts[:-1]):
        return True
    # Skip SKILL.md files (superpowers skill definitions)
    if path.name == "SKILL.md":
        return True
    # Skip index and navigation files
    if path.name in ("index.md", "_index.md", "START_HERE.md"):
        return True
    # Skip the log file — it contains ingest headers with individual page links
    if rel == Path("wiki/log.md"):
        return True
    return False


def build_stem_index(root: Path) -> dict[str, list[Path]]:
    """Build a map from filename stem → list of matching paths (for fuzzy wikilink resolution)."""
    index: dict[str, list[Path]] = {}
    for p in root.rglob("*.md"):
        if should_skip_md(p, root):
            continue
        s = p.stem
        index.setdefault(s, []).append(p)
    return index


def has_orphan_false_in_frontmatter(content: str) -> bool:
    """Return True if YAML frontmatter contains 'orphan: false'."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        if re.match(r'\s*orphan\s*:\s*false\s*$', line):
            return True
    return False


def add_orphan_false_to_frontmatter(file_path: Path) -> bool:
    """Add 'orphan: false' to the file's YAML frontmatter. Returns True if changed."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    if has_orphan_false_in_frontmatter(content):
        return False
    lines = content.splitlines(keepends=True)
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() in ("---", "..."):
                lines.insert(i, "orphan: false\n")
                file_path.write_text(''.join(lines), encoding="utf-8")
                return True
        return False  # unclosed frontmatter
    else:
        file_path.write_text("---\norphan: false\n---\n" + content, encoding="utf-8")
        return True


def remove_orphan_false_from_frontmatter(file_path: Path) -> bool:
    """Remove 'orphan: false' from the file's YAML frontmatter. Returns True if changed."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return False
    fm_end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() in ("---", "..."):
            fm_end = i
            break
    if fm_end is None:
        return False
    new_lines = [
        line for i, line in enumerate(lines)
        if not (0 < i < fm_end and re.match(r'\s*orphan\s*:\s*false\s*$', line.rstrip('\r\n')))
    ]
    if len(new_lines) < len(lines):
        file_path.write_text(''.join(new_lines), encoding="utf-8")
        return True
    return False


def has_stub_in_frontmatter(content: str) -> bool:
    """Return True if YAML frontmatter contains 'stub: true'."""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return False
    for line in lines[1:]:
        if line.strip() in ("---", "..."):
            break
        if re.match(r'\s*stub\s*:\s*true\s*$', line):
            return True
    return False


def remove_stub_from_frontmatter(file_path: Path) -> bool:
    """Remove 'stub: true' from the file's YAML frontmatter. Returns True if changed."""
    content = file_path.read_text(encoding="utf-8", errors="replace")
    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        return False
    fm_end = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() in ("---", "..."):
            fm_end = i
            break
    if fm_end is None:
        return False
    new_lines = [
        line for i, line in enumerate(lines)
        if not (0 < i < fm_end and re.match(r'\s*stub\s*:\s*true\s*$', line.rstrip('\r\n')))
    ]
    if len(new_lines) < len(lines):
        file_path.write_text(''.join(new_lines), encoding="utf-8")
        return True
    return False


def check_stubs(root: Path, quiet: bool) -> dict:
    """Find wiki pages (wiki/*/*.md) with 'stub: true' in frontmatter."""
    wiki_dir = root / "wiki"
    if not wiki_dir.is_dir():
        return {"stubs": [], "summary": {"wiki_pages_checked": 0, "stubs_found": 0}}

    stubs = []
    wiki_pages_checked = 0
    for md_file in sorted(wiki_dir.glob("*/*.md")):
        if md_file.name in ("index.md", "_index.md"):
            continue
        wiki_pages_checked += 1
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if has_stub_in_frontmatter(content):
            stubs.append(str(md_file.relative_to(root)))

    return {
        "stubs": sorted(stubs),
        "summary": {"wiki_pages_checked": wiki_pages_checked, "stubs_found": len(stubs)},
    }


_RAW_TEXT_EXTENSIONS = {".md", ".txt", ".vtt", ".eml"}


def has_raw_reference(stem: str, raw_dir: Path) -> bool:
    """Return True if any text file in raw/ contains a plain-text reference to stem."""
    if not raw_dir.is_dir():
        return False
    target_re = re.compile(r'(?<!\w)' + re.escape(stem) + r'(?:\.md)?(?!\w)')
    for raw_file in raw_dir.rglob("*"):
        if not raw_file.is_file() or raw_file.suffix.lower() not in _RAW_TEXT_EXTENSIONS:
            continue
        try:
            content = raw_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if target_re.search(content):
            return True
    return False


def check_orphans(root: Path, quiet: bool) -> dict:
    """Find wiki pages (wiki/*/*.md) with no backlinks except from index files."""
    wiki_dir = root / "wiki"
    if not wiki_dir.is_dir():
        return {"orphans": [], "summary": {"wiki_pages_checked": 0, "orphans_found": 0}}

    # Collect candidate pages: exactly wiki/<subdir>/<file>.md
    # Exclude index files and pages that explicitly declare orphan: false
    wiki_pages: list[Path] = []
    for md_file in sorted(wiki_dir.glob("*/*.md")):
        if md_file.name in ("index.md", "_index.md"):
            continue
        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            content = ""
        if has_orphan_false_in_frontmatter(content):
            continue
        wiki_pages.append(md_file)

    if not quiet:
        print(f"Checking orphans across {len(wiki_pages)} wiki pages ...", file=sys.stderr)

    # Build stem index scoped to wiki pages only (for unambiguous resolution)
    stem_index: dict[str, list[Path]] = {}
    for p in wiki_pages:
        stem_index.setdefault(p.stem, []).append(p)

    # Build backlink map: resolved_path -> set of source_rel (non-index sources only)
    backlinks: dict[str, set[str]] = {}
    scanned = 0
    for md_file in sorted(root.rglob("*.md")):
        rel = md_file.relative_to(root)
        if any(part.startswith(".") for part in rel.parts[:-1]):
            continue
        if md_file.name == "SKILL.md":
            continue
        if md_file.name in ("index.md", "_index.md"):
            continue  # index pages don't count as backlink sources

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue

        scanned += 1
        if not quiet and scanned % 50 == 0:
            print(f"\r  {scanned} files scanned for backlinks ...", end="", flush=True, file=sys.stderr)

        for _, link_type, _, target in extract_links(content, include_images=False):
            if link_type != "wikilink":
                continue
            if not target or target.startswith("#") or is_external(target):
                continue
            resolved = resolve_wikilink_to_path(target, root, stem_index)
            if resolved is not None:
                backlinks.setdefault(str(resolved), set()).add(str(rel))

    if not quiet:
        print(f"\r  {scanned} files scanned for backlinks — done.        ", file=sys.stderr)

    orphans = [
        str(p.relative_to(root))
        for p in wiki_pages
        if not backlinks.get(str(p))
    ]

    return {
        "orphans": sorted(orphans),
        "summary": {
            "wiki_pages_checked": len(wiki_pages),
            "orphans_found": len(orphans),
        },
    }


def replace_plain_references_in_content(content: str, stem: str) -> tuple[str, int]:
    """
    Replace plain-text occurrences of `stem` (and `stem.md`) with `[[stem]]`.
    Skips YAML frontmatter, existing wikilinks, markdown links, and inline code.
    """
    target_re = re.compile(r'(?<!\w)' + re.escape(stem) + r'(?:\.md)?(?!\w)')
    # Regions to skip: [[wikilinks]], [text](url) markdown links, `inline code`
    skip_re = re.compile(r'\[\[[^\]\n]+\]\]|\[(?:[^\]\n]*)\]\([^)\n]*\)|`[^`\n]*`')

    lines = content.splitlines(keepends=True)

    # Find frontmatter end (lines to skip at top)
    fm_end = 0
    if lines and lines[0].strip() == "---":
        for i, line in enumerate(lines[1:], 1):
            if line.strip() in ("---", "..."):
                fm_end = i + 1
                break

    result = []
    count = 0
    for i, line in enumerate(lines):
        if i < fm_end:
            result.append(line)
            continue
        parts = []
        last = 0
        for m in skip_re.finditer(line):
            safe = line[last:m.start()]
            replaced, n = target_re.subn(f'[[{stem}]]', safe)
            parts.append(replaced)
            count += n
            parts.append(m.group(0))
            last = m.end()
        safe = line[last:]
        replaced, n = target_re.subn(f'[[{stem}]]', safe)
        parts.append(replaced)
        count += n
        result.append(''.join(parts))

    return ''.join(result), count


def fix_orphans(orphans: list[str], root: Path, quiet: bool) -> dict:
    """
    For each orphaned wiki page:
    - Find plain-text references in wiki/ and replace with wikilinks (only wiki/ modified).
    - If wiki/ references were linked: remove 'orphan: false' from the page's frontmatter.
    - If no wiki/ references found but raw/ mentions the stem: add 'orphan: false' to the
      page's frontmatter to acknowledge it is known from raw context.
    Raw files are never modified.
    """
    wiki_dir = root / "wiki"
    raw_dir = root / "raw"
    if not wiki_dir.is_dir():
        return {"fixed_references": 0, "files_changed": 0, "orphans_resolved": 0, "details": []}

    wiki_files = sorted(wiki_dir.rglob("*.md"))

    total_refs = 0
    total_files = 0
    details = []

    for orphan_rel in orphans:
        orphan_path = root / orphan_rel
        stem = orphan_path.stem

        if len(stem) < 3:
            continue  # too short — would cause too many false positives

        refs_linked = 0
        files_touched: list[str] = []

        for wiki_file in wiki_files:
            if wiki_file == orphan_path:
                continue  # don't add self-references

            try:
                content = wiki_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue

            if stem not in content:
                continue  # fast path

            new_content, n = replace_plain_references_in_content(content, stem)
            if n:
                wiki_file.write_text(new_content, encoding="utf-8")
                refs_linked += n
                total_files += 1
                files_touched.append((str(wiki_file.relative_to(root)), n))

        # Update frontmatter on the orphan page itself
        fm_action: "str | None" = None
        if refs_linked > 0:
            # Page now has real wiki backlinks — remove orphan: false if present
            if remove_orphan_false_from_frontmatter(orphan_path):
                fm_action = "removed_orphan_false"
        else:
            # No wiki links found — check raw/ for any mention
            if has_raw_reference(stem, raw_dir):
                if add_orphan_false_to_frontmatter(orphan_path):
                    fm_action = "added_orphan_false"

        if not quiet:
            if refs_linked > 0:
                file_list = ", ".join(f"{f} ({n})" for f, n in files_touched)
                print(f"  {orphan_rel}: [[{stem}]] linked in {file_list}", file=sys.stderr)
            if fm_action == "added_orphan_false":
                print(f"  {orphan_rel}: raw reference found → orphan: false added", file=sys.stderr)
            elif fm_action == "removed_orphan_false":
                print(f"  {orphan_rel}: orphan: false removed (now has wiki links)", file=sys.stderr)

        if refs_linked or fm_action:
            total_refs += refs_linked
            details.append({
                "orphan": orphan_rel,
                "stem": stem,
                "references_linked": refs_linked,
                "files_changed": [f for f, _ in files_touched],
                "frontmatter": fm_action,
            })

    return {
        "fixed_references": total_refs,
        "files_changed": total_files,
        "orphans_resolved": len([d for d in details if d["references_linked"] > 0]),
        "orphans_acknowledged": len([d for d in details if d["frontmatter"] == "added_orphan_false"]),
        "details": details,
    }


def check_vault(root: Path, args) -> dict:
    errors = []
    broken = []
    total_files = 0
    total_links = 0
    skipped_external = 0

    if not root.is_dir():
        return {
            "broken_links": [],
            "summary": {"files_checked": 0, "links_checked": 0, "broken": 0, "skipped_external": 0},
            "errors": [f"Root directory not found: {root}"]
        }

    if not args.quiet:
        print(f"Scanning {root} ...", file=sys.stderr)

    stem_index = build_stem_index(root)
    norm_index = build_normalized_index(root)
    md_files = sorted(p for p in root.rglob("*.md") if not should_skip_md(p, root))

    for md_file in md_files:
        total_files += 1
        rel = md_file.relative_to(root)

        try:
            content = md_file.read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            errors.append(f"Cannot read {rel}: {e}")
            continue

        _, fm_end_line = strip_frontmatter(content)

        for lineno, link_type, raw, target in extract_links(content, args.include_images, args.skip_frontmatter):
            total_links += 1

            # External links
            if is_external(target):
                if args.external:
                    ok, reason = check_external(target, args.timeout)
                    if not ok:
                        broken.append({
                            "file": str(rel),
                            "line": lineno,
                            "type": link_type,
                            "raw": raw,
                            "target": target,
                            "reason": reason,
                        })
                else:
                    skipped_external += 1
                continue

            # Skip empty or anchor-only targets
            if not target or target.startswith("#"):
                total_links -= 1
                continue

            # Resolve
            if link_type == "wikilink" or (link_type == "image" and "[[" in raw):
                ok = resolve_wikilink(target, root, stem_index)
                if not ok and Path(target).parent != Path("."):
                    ok = (md_file.parent / target).exists()
            else:
                ok = resolve_mdlink(target, md_file, root, stem_index)

            if not ok:
                entry = {
                    "file": str(rel),
                    "line": lineno,
                    "type": link_type,
                    "raw": raw,
                    "target": target,
                    "reason": "file not found",
                }
                if fm_end_line and lineno <= fm_end_line:
                    entry["in_frontmatter"] = True
                if link_type == "wikilink" or (link_type == "image" and "[[" in raw):
                    fix = find_normalized_match(target, root, norm_index)
                    if fix:
                        entry["suggested_fix"] = fix
                broken.append(entry)

        if not args.quiet and total_files % 50 == 0:
            print(f"\r  {total_files} files scanned ...", end="", flush=True, file=sys.stderr)

    if not args.quiet:
        print(f"\r  {total_files} files scanned — done.        ", file=sys.stderr)

    fixed_links = 0
    fixed_files = 0
    if getattr(args, "fix_simple_errors", False):
        fixes_by_file: dict = {}
        for entry in broken:
            if "suggested_fix" in entry:
                fp = root / entry["file"]
                fixes_by_file.setdefault(fp, []).append(
                    (entry["target"], entry["suggested_fix"])
                )
        for fp, fixes in fixes_by_file.items():
            seen: set = set()
            deduped = [f for f in fixes if not (f in seen or seen.add(f))]  # type: ignore[func-returns-value]
            n = fix_wikilinks_in_file(fp, deduped)
            if n:
                fixed_files += 1
                fixed_links += n
        for entry in broken:
            if "suggested_fix" in entry:
                entry["fixed"] = True
        if not args.quiet and fixed_links:
            print(f"  Fixed {fixed_links} link(s) in {fixed_files} file(s).", file=sys.stderr)

        # Delete bullet lines in YAML frontmatter that contain unfixable broken wikilinks
        fm_targets_by_file: dict = {}
        for entry in broken:
            if entry.get("fixed") or not entry.get("in_frontmatter"):
                continue
            if entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"]):
                fp = root / entry["file"]
                fm_targets_by_file.setdefault(fp, []).append(entry["target"])
        fm_deleted_links = 0
        fm_deleted_files = 0
        for fp, targets in fm_targets_by_file.items():
            seen: set = set()
            deduped = [t for t in targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
            file_changed = False
            for target in deduped:
                changed, _ = delete_wikilink_in_file(fp, target)
                if changed:
                    fm_deleted_links += 1
                    file_changed = True
            if file_changed:
                fm_deleted_files += 1
        for entry in broken:
            if not entry.get("fixed") and entry.get("in_frontmatter"):
                if entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"]):
                    entry["fm_deleted"] = True
        if not args.quiet and fm_deleted_links:
            print(f"  Removed {fm_deleted_links} frontmatter broken link(s) in {fm_deleted_files} file(s).", file=sys.stderr)

        q_renamed, q_link_files, q_links = fix_curly_quotes(root, args.quiet)
        if not args.quiet and (q_renamed or q_links):
            print(f"  Curly quotes: {q_renamed} file(s) renamed, "
                  f"{q_links} link(s) updated in {q_link_files} file(s).", file=sys.stderr)

    removed_links = 0
    removed_files = 0
    if getattr(args, "remove_broken_links", False):
        targets_by_file: dict = {}
        for entry in broken:
            if entry.get("fixed"):
                continue
            if entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"]):
                fp = root / entry["file"]
                targets_by_file.setdefault(fp, []).append(entry["target"])
        for fp, targets in targets_by_file.items():
            seen: set = set()
            deduped = [t for t in targets if not (t in seen or seen.add(t))]  # type: ignore[func-returns-value]
            n = mark_broken_wikilinks_in_file(fp, deduped)
            if n:
                removed_files += 1
                removed_links += n
        for entry in broken:
            if not entry.get("fixed") and (entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"])):
                entry["removed"] = True
        if not args.quiet and removed_links:
            print(f"  Marked {removed_links} broken link(s) in {removed_files} file(s).", file=sys.stderr)

    summary: dict = {
        "files_checked": total_files,
        "links_checked": total_links,
        "broken": len(broken),
        "skipped_external": skipped_external,
    }
    if getattr(args, "fix_simple_errors", False):
        summary["fixed_links"] = fixed_links
        summary["fixed_files"] = fixed_files
        if fm_deleted_links:
            summary["fm_deleted_links"] = fm_deleted_links
            summary["fm_deleted_files"] = fm_deleted_files
        if q_renamed or q_links:
            summary["quote_renamed_files"] = q_renamed
            summary["quote_updated_links"] = q_links
            summary["quote_updated_link_files"] = q_link_files
    if getattr(args, "remove_broken_links", False):
        summary["removed_links"] = removed_links
        summary["removed_files"] = removed_files

    return {
        "broken_links": broken,
        "summary": summary,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_text(result: dict) -> str:
    lines = []
    s = result["summary"]
    lines.append(f"Checked {s['files_checked']} files, {s['links_checked']} links — "
                 f"{s['broken']} broken, {s['skipped_external']} external skipped.")
    if s.get("fixed_links"):
        lines.append(f"Fixed {s['fixed_links']} link(s) in {s['fixed_files']} file(s).")
    if s.get("fm_deleted_links"):
        lines.append(f"Removed {s['fm_deleted_links']} frontmatter broken link(s) in {s['fm_deleted_files']} file(s).")
    if s.get("removed_links"):
        lines.append(f"Marked {s['removed_links']} broken link(s) in {s['removed_files']} file(s).")
    lines.append("")

    if result["errors"]:
        lines.append("ERRORS:")
        for e in result["errors"]:
            lines.append(f"  ! {e}")
        lines.append("")

    if not result["broken_links"]:
        lines.append("No broken links found.")
    else:
        lines.append("BROKEN LINKS:")
        for b in result["broken_links"]:
            lines.append(f"{b['line']}: {b['file']}")
            lines.append(f"    type  : {b['type']}")
            lines.append(f"    reason: {b['reason']}")
            lines.append(f"    raw   : {b['raw']}")
            lines.append(f"    target: {b['target']}")
            if "suggested_fix" in b:
                suffix = " (fixed)" if b.get("fixed") else " (use --fix-simple-errors to apply)"
                lines.append(f"    suggested_fix: {b['suggested_fix']}{suffix}")
            if b.get("removed"):
                lines.append("    action: marked as broken in file")
        lines.append("")

    if "orphans" in result:
        lines.append("")
        os_ = result["orphans"]
        os_s = result.get("orphan_summary", {})
        fix = result.get("orphan_fix")
        if fix:
            parts = [f"{fix['orphans_resolved']} orphan(s) resolved via wiki links"]
            if fix.get("orphans_acknowledged"):
                parts.append(f"{fix['orphans_acknowledged']} acknowledged via raw reference (orphan: false added)")
            lines.append(f"ORPHAN FIX: {', '.join(parts)}; "
                         f"{fix['fixed_references']} reference(s) linked in {fix['files_changed']} file(s).")
        lines.append(f"ORPHAN CHECK: {os_s.get('wiki_pages_checked', '?')} pages checked, "
                     f"{os_s.get('orphans_found', len(os_))} orphan(s) remaining.")
        if os_:
            lines.append("ORPHANS (no incoming links except from index pages):")
            for o in os_:
                lines.append(f"  {o}")
        else:
            lines.append("No orphan pages found.")

    if "stubs" in result:
        lines.append("")
        st_ = result["stubs"]
        st_s = result.get("stub_summary", {})
        lines.append(f"STUB CHECK: {st_s.get('wiki_pages_checked', '?')} pages checked, "
                     f"{st_s.get('stubs_found', len(st_))} stub(s) found.")
        if st_:
            lines.append("STUBS (stub: true in frontmatter):")
            for s in st_:
                lines.append(f"  {s}")
        else:
            lines.append("No stub pages found.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Interactive mode
# ---------------------------------------------------------------------------

def run_interactive(broken_links: list, orphans: list, stubs: list, root: Path) -> None:
    """Curses-based TUI for reviewing broken links, orphan pages, and stub pages."""
    try:
        import curses as _curses
    except ImportError:
        print("Error: curses module not available on this platform.", file=sys.stderr)
        sys.exit(1)

    if not broken_links and not orphans and not stubs:
        print("No broken links, orphan pages, or stub pages found.")
        return

    all_items = [{"_kind": "link", **b} for b in broken_links] + \
                [{"_kind": "orphan", "file": o} for o in orphans] + \
                [{"_kind": "stub", "file": s} for s in stubs]
    n = len(all_items)
    states: list = [None] * n
    messages: list = [""] * n

    def fmt_line(i: int) -> str:
        item = all_items[i]
        if item["_kind"] in ("orphan", "stub"):
            return f"{i + 1:3d}  file:{item['file']}"
        return f"{i + 1:3d}  file:{truncate_path(item['file'])}, line:{item['line']}, link:{item['target']}"

    def do_delete(i: int) -> str:
        entry = broken_links[i]
        try:
            is_wiki = entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"])
            if is_wiki:
                ok, removed = delete_wikilink_in_file(root / entry["file"], entry["target"])
            else:
                ok, removed = delete_mdlink_in_file(root / entry["file"], entry["target"])
            if ok and removed:
                # Adjust line numbers for all entries in the same file that came
                # after any deleted line, so the popup context stays accurate.
                for other in broken_links:
                    if other["file"] != entry["file"]:
                        continue
                    shift = sum(1 for dl in removed if dl < other["line"])
                    if shift:
                        other["line"] -= shift
            return "deleted" if ok else "no match — may already be handled"
        except Exception as e:
            return f"error: {e}"

    def do_broken(i: int) -> str:
        entry = broken_links[i]
        try:
            ok = mark_as_broken_link_in_file(root / entry["file"], entry["target"])
            return "broken" if ok else "no match — may already be handled"
        except Exception as e:
            return f"error: {e}"

    def do_delink(i: int) -> str:
        entry = broken_links[i]
        try:
            n = delink_wikilink_in_file(root / entry["file"], entry["target"])
            return "delinked" if n else "no match — may already be handled"
        except Exception as e:
            return f"error: {e}"

    def do_delete_orphan(i: int) -> str:
        try:
            (root / all_items[i]["file"]).unlink()
            return "deleted"
        except Exception as e:
            return f"error: {e}"

    def do_keep_orphan(i: int) -> str:
        try:
            changed = add_orphan_false_to_frontmatter(root / all_items[i]["file"])
            return "kept" if changed else "already kept"
        except Exception as e:
            return f"error: {e}"

    def do_remove_stub(i: int) -> str:
        try:
            changed = remove_stub_from_frontmatter(root / all_items[i]["file"])
            return "unstubbed" if changed else "already not a stub"
        except Exception as e:
            return f"error: {e}"

    def show_orphan_preview(stdscr, entry: dict, idx: int) -> "str | None":
        """Show scrollable file contents for an orphan. Returns 'd', 'k', or None."""
        try:
            file_lines = (root / entry["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            file_lines = [f"(error reading file: {e})"]

        height, width = stdscr.getmaxyx()
        pop_w = min(max(40, width - 4), width - 2)
        pop_h = min(max(10, height - 4), height - 2)
        pop_y = max(0, (height - pop_h) // 2)
        pop_x = max(0, (width - pop_w) // 2)
        inner_w = pop_w - 4
        list_h = pop_h - 5

        win = _curses.newwin(pop_h, pop_w, pop_y, pop_x)
        win.keypad(True)
        scroll = 0
        sep = "─" * (pop_w - 2)
        hint = "[ d=delete   k=keep as orphan   ↑↓=prev/next   PgUp/PgDn=scroll   h=help   Enter/q=close ]"

        while True:
            win.erase()
            win.box()
            title = f" Orphan preview {idx + 1}/{n} "
            try:
                win.addstr(0, max(1, (pop_w - len(title)) // 2), title)
                win.addstr(1, 2, entry["file"][:pop_w - 3])
                win.addstr(2, 1, sep[:pop_w - 2])
            except _curses.error:
                pass
            for row in range(list_h):
                li = scroll + row
                if li >= len(file_lines):
                    break
                try:
                    win.addstr(3 + row, 2, file_lines[li][:inner_w])
                except _curses.error:
                    pass
            try:
                win.addstr(pop_h - 2, max(1, (pop_w - len(hint)) // 2), hint[:pop_w - 2])
            except _curses.error:
                pass
            win.refresh()

            key = win.getch()
            if key in (10, 13, ord("q"), ord("Q"), 27):
                break
            elif key == _curses.KEY_UP:
                del win; stdscr.touchwin(); stdscr.refresh()
                return "prev"
            elif key == _curses.KEY_DOWN:
                del win; stdscr.touchwin(); stdscr.refresh()
                return "next"
            elif key == _curses.KEY_PPAGE:
                scroll = max(0, scroll - list_h)
            elif key == _curses.KEY_NPAGE:
                scroll = min(max(0, len(file_lines) - list_h), scroll + list_h)
            elif key in (ord("d"), ord("D")):
                del win; stdscr.touchwin(); stdscr.refresh()
                return "d"
            elif key in (ord("k"), ord("K")):
                del win; stdscr.touchwin(); stdscr.refresh()
                return "k"
            elif key in (ord("h"), ord("H")):
                show_help(stdscr)

        del win
        stdscr.touchwin()
        stdscr.refresh()
        return None

    def show_stub_preview(stdscr, entry: dict, idx: int) -> "str | None":
        """Show scrollable file contents for a stub. Returns 'd', 'k', or None."""
        try:
            file_lines = (root / entry["file"]).read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError as e:
            file_lines = [f"(error reading file: {e})"]

        height, width = stdscr.getmaxyx()
        pop_w = min(max(40, width - 4), width - 2)
        pop_h = min(max(10, height - 4), height - 2)
        pop_y = max(0, (height - pop_h) // 2)
        pop_x = max(0, (width - pop_w) // 2)
        inner_w = pop_w - 4
        list_h = pop_h - 5

        win = _curses.newwin(pop_h, pop_w, pop_y, pop_x)
        win.keypad(True)
        scroll = 0
        sep = "─" * (pop_w - 2)
        hint = "[ d=delete   k=keep stub (remove stub: true)   ↑↓=prev/next   PgUp/PgDn=scroll   h=help   Enter/q=close ]"

        while True:
            win.erase()
            win.box()
            title = f" Stub preview {idx + 1}/{n} "
            try:
                win.addstr(0, max(1, (pop_w - len(title)) // 2), title)
                win.addstr(1, 2, entry["file"][:pop_w - 3])
                win.addstr(2, 1, sep[:pop_w - 2])
            except _curses.error:
                pass
            for row in range(list_h):
                li = scroll + row
                if li >= len(file_lines):
                    break
                try:
                    win.addstr(3 + row, 2, file_lines[li][:inner_w])
                except _curses.error:
                    pass
            try:
                win.addstr(pop_h - 2, max(1, (pop_w - len(hint)) // 2), hint[:pop_w - 2])
            except _curses.error:
                pass
            win.refresh()

            key = win.getch()
            if key in (10, 13, ord("q"), ord("Q"), 27):
                break
            elif key == _curses.KEY_UP:
                del win; stdscr.touchwin(); stdscr.refresh()
                return "prev"
            elif key == _curses.KEY_DOWN:
                del win; stdscr.touchwin(); stdscr.refresh()
                return "next"
            elif key == _curses.KEY_PPAGE:
                scroll = max(0, scroll - list_h)
            elif key == _curses.KEY_NPAGE:
                scroll = min(max(0, len(file_lines) - list_h), scroll + list_h)
            elif key in (ord("d"), ord("D")):
                del win; stdscr.touchwin(); stdscr.refresh()
                return "d"
            elif key in (ord("k"), ord("K")):
                del win; stdscr.touchwin(); stdscr.refresh()
                return "k"
            elif key in (ord("h"), ord("H")):
                show_help(stdscr)

        del win
        stdscr.touchwin()
        stdscr.refresh()
        return None

    def read_source_context(entry: dict, context: int = 2) -> list:
        """Return list of (lineno, text, is_current) for the line and `context` lines around it."""
        try:
            fp = root / entry["file"]
            lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
            current = entry["line"] - 1  # 0-indexed
            start = max(0, current - context)
            end = min(len(lines), current + context + 1)
            return [(i + 1, lines[i], i == current) for i in range(start, end)]
        except Exception as e:
            return [(entry["line"], f"(error reading file: {e})", True)]

    def show_file_browser(stdscr, broken_target: str = "") -> "Path | None":
        """Browse the vault tree and pick a replacement .md file.
        Returns path relative to root, or None if cancelled."""
        top_names = ("wiki", "raw")
        top_dirs = [root / name for name in top_names if (root / name).is_dir()]
        if not top_dirs:
            top_dirs = sorted(d for d in root.iterdir() if d.is_dir() and not d.name.startswith("."))

        class _Node:
            __slots__ = ("path", "depth", "expanded", "_children")

            def __init__(self, path: Path, depth: int):
                self.path = path
                self.depth = depth
                self.expanded = False
                self._children: "list | None" = None

            @property
            def is_dir(self) -> bool:
                return self.path.is_dir()

            def load_children(self) -> list:
                if self._children is None:
                    kids: list = []
                    try:
                        for p in sorted(self.path.iterdir(),
                                        key=lambda x: (not x.is_dir(), x.name.lower())):
                            if p.name.startswith("."):
                                continue
                            if p.is_dir() or p.suffix.lower() == ".md":
                                kids.append(_Node(p, self.depth + 1))
                    except PermissionError:
                        pass
                    self._children = kids
                return self._children

        root_nodes = [_Node(d, 0) for d in top_dirs]

        def build_visible() -> list:
            out: list = []

            def _walk(nodes: list) -> None:
                for nd in nodes:
                    out.append(nd)
                    if nd.is_dir and nd.expanded:
                        _walk(nd.load_children())

            _walk(root_nodes)
            return out

        height, width = stdscr.getmaxyx()
        pop_w = min(max(50, width - 6), width - 2)
        pop_h = min(max(10, height - 4), height - 2)
        pop_y = max(0, (height - pop_h) // 2)
        pop_x = max(0, (width - pop_w) // 2)

        win = _curses.newwin(pop_h, pop_w, pop_y, pop_x)
        win.keypad(True)

        selected = 0
        scroll_offset = 0

        while True:
            visible = build_visible()
            if not visible:
                del win
                stdscr.touchwin()
                stdscr.refresh()
                return None
            if selected >= len(visible):
                selected = len(visible) - 1

            win.erase()
            win.box()
            title = " Find replacement link "
            try:
                win.addstr(0, max(1, (pop_w - len(title)) // 2), title)
            except _curses.error:
                pass

            if broken_target:
                try:
                    label = "replacing: "
                    win.addstr(1, 2, label, _curses.A_DIM)
                    win.addstr(1, 2 + len(label),
                               broken_target[:max(1, pop_w - 2 - len(label))],
                               _curses.color_pair(5) | _curses.A_BOLD)
                except _curses.error:
                    pass

            nav = "↑↓ navigate   → expand   ← collapse   a-z=jump to name   Enter=select   Esc=cancel"
            try:
                win.addstr(2, max(1, (pop_w - len(nav)) // 2), nav[:pop_w - 2])
            except _curses.error:
                pass
            sep = "─" * (pop_w - 2)
            try:
                win.addstr(3, 1, sep[:pop_w - 2])
            except _curses.error:
                pass

            list_h = pop_h - 5  # rows 4 .. pop_h-2
            if selected < scroll_offset:
                scroll_offset = selected
            elif selected >= scroll_offset + list_h:
                scroll_offset = selected - list_h + 1

            inner_w = pop_w - 4
            for row in range(list_h):
                idx = scroll_offset + row
                if idx >= len(visible):
                    break
                nd = visible[idx]
                indent = "  " * nd.depth
                if nd.is_dir:
                    icon = "▼ " if nd.expanded else "▶ "
                    label = indent + icon + nd.path.name + "/"
                else:
                    label = indent + "  " + nd.path.name
                attr = _curses.A_REVERSE if idx == selected else _curses.A_NORMAL
                try:
                    win.addstr(4 + row, 2, label[:inner_w], attr)
                except _curses.error:
                    pass

            win.refresh()
            key = win.getch()

            if key == 27:  # Escape
                del win
                stdscr.touchwin()
                stdscr.refresh()
                return None
            elif key == _curses.KEY_UP:
                if selected > 0:
                    selected -= 1
            elif key == _curses.KEY_DOWN:
                if selected < len(visible) - 1:
                    selected += 1
            elif key == _curses.KEY_PPAGE:
                page = max(1, list_h - 1)
                selected = max(0, selected - page)
            elif key == _curses.KEY_NPAGE:
                page = max(1, list_h - 1)
                selected = min(len(visible) - 1, selected + page)
            elif key == _curses.KEY_RIGHT:
                nd = visible[selected]
                if nd.is_dir and not nd.expanded:
                    nd.expanded = True
                    nd.load_children()
            elif key == _curses.KEY_LEFT:
                nd = visible[selected]
                if nd.is_dir and nd.expanded:
                    nd.expanded = False
                else:
                    # Jump to and collapse parent
                    for i in range(selected - 1, -1, -1):
                        if visible[i].depth == nd.depth - 1 and visible[i].is_dir:
                            visible[i].expanded = False
                            selected = i
                            break
            elif key in (10, 13):  # Enter
                nd = visible[selected]
                if nd.is_dir:
                    nd.expanded = not nd.expanded
                    if nd.expanded:
                        nd.load_children()
                else:
                    del win
                    stdscr.touchwin()
                    stdscr.refresh()
                    return nd.path.relative_to(root)
            elif 32 <= key <= 126:  # printable ASCII — jump to next matching name
                ch = chr(key).lower()
                n_vis = len(visible)
                for offset in range(1, n_vis + 1):
                    candidate = (selected + offset) % n_vis
                    if visible[candidate].path.name.lower().startswith(ch):
                        selected = candidate
                        break

    def do_find_replace(i: int, new_rel: Path) -> str:
        """Replace the broken link at index i with a link to new_rel (relative to root)."""
        entry = broken_links[i]
        old_target = entry["target"]
        is_wiki = entry["type"] == "wikilink" or (entry["type"] == "image" and "[[" in entry["raw"])
        try:
            if is_wiki:
                new_target = str(new_rel.with_suffix(""))
                count = fix_wikilinks_in_file(root / entry["file"], [(old_target, new_target)])
            else:
                source_dir = (root / entry["file"]).parent
                new_target = os.path.relpath(root / new_rel, source_dir)
                count = replace_mdlink_target_in_file(root / entry["file"], old_target, new_target)
            if count:
                entry["target"] = new_target
                return "replaced"
            return "no match — may already be changed"
        except Exception as e:
            return f"error: {e}"

    def show_popup(stdscr, entry: dict, idx: int) -> "str | None":
        """Draw a wide popup with source context and missing link; close on Enter."""
        context_lines = read_source_context(entry)
        missing = entry["target"]

        height, width = stdscr.getmaxyx()
        pop_w = min(max(40, width - 4), width - 2)
        inner_w = pop_w - 4

        def _wrap(lineno, text):
            """Yield (display_str, char_start, char_end, text_offset) per wrapped row."""
            prefix = f"{lineno:4d}  "
            prefix_len = len(prefix)
            text_w = max(1, inner_w - prefix_len)
            indent = " " * prefix_len
            if not text:
                yield (prefix, 0, 0, prefix_len)
                return
            for i in range(0, len(text), text_w):
                chunk = text[i:i + text_w]
                yield ((prefix if i == 0 else indent) + chunk, i, i + len(chunk), prefix_len)

        # Find the link's char span in the source line before wrapping.
        # Use raw_link to locate the link, then narrow to just tgt within it
        # so that surrounding syntax (e.g. [[ ]]) is not highlighted.
        raw_link = entry.get("raw", "")
        tgt = entry.get("target", "")
        link_start = link_end = -1
        for _, src_text, is_cur in context_lines:
            if is_cur:
                if raw_link:
                    raw_pos = src_text.find(raw_link)
                    if raw_pos != -1:
                        tgt_off = raw_link.find(tgt) if tgt else -1
                        if tgt_off != -1:
                            link_start = raw_pos + tgt_off
                            link_end = link_start + len(tgt)
                        else:
                            link_start = raw_pos
                            link_end = raw_pos + len(raw_link)
                if link_start == -1 and tgt:
                    pos = src_text.find(tgt)
                    if pos != -1:
                        link_start = pos
                        link_end = pos + len(tgt)
                break

        # Pre-wrap all context lines so pop_h reflects actual row count
        display_rows: list[tuple[str, bool, int, int, int]] = []
        for lineno, text, is_current in context_lines:
            for disp, cstart, cend, toff in _wrap(lineno, text):
                display_rows.append((disp, is_current, cstart, cend, toff))

        # Layout: 4 header rows + content rows + sep + missing + hint + border = +4 fixed footer
        pop_h = min(4 + len(display_rows) + 4, height - 2)
        pop_y = max(0, (height - pop_h) // 2)
        pop_x = max(0, (width - pop_w) // 2)

        sep = "─" * (pop_w - 2)
        hint = "[ ↑/↓ prev/next   d=delete   b=mark broken   p=plain text   f=find link   h=help   Enter/q=close ]"

        win = _curses.newwin(pop_h, pop_w, pop_y, pop_x)
        win.keypad(True)
        win.box()
        title = f" Broken link detail {idx + 1}/{n} "
        win.addstr(0, (pop_w - len(title)) // 2, title)
        win.addstr(1, 2, f"file: {entry['file']}"[:pop_w - 3])
        win.addstr(2, 2, f"line: {entry['line']}"[:pop_w - 3])
        win.addstr(3, 1, sep[:pop_w - 2])
        max_content_rows = max(0, pop_h - 8)
        hl_attr = _curses.color_pair(5) | _curses.A_BOLD
        for i, (display, is_current, cstart, cend, toff) in enumerate(display_rows[:max_content_rows]):
            base_attr = _curses.A_NORMAL if is_current else _curses.A_DIM
            text = display[:pop_w - 3]
            try:
                if is_current and link_start != -1 and cstart < link_end and cend > link_start:
                    # Map source-text char offsets to display positions for this row
                    row_hl_s = max(0, link_start - cstart)
                    row_hl_e = min(cend - cstart, link_end - cstart)
                    d_s = min(toff + row_hl_s, len(text))
                    d_e = min(toff + row_hl_e, len(text))
                    if d_s > 0:
                        win.addstr(4 + i, 2, text[:d_s], base_attr)
                    if d_s < d_e:
                        win.addstr(4 + i, 2 + d_s, text[d_s:d_e], hl_attr)
                    if d_e < len(text):
                        win.addstr(4 + i, 2 + d_e, text[d_e:], base_attr)
                else:
                    win.addstr(4 + i, 2, text, base_attr)
            except _curses.error:
                pass
        sep_row = 4 + min(len(display_rows), max_content_rows)
        try:
            win.addstr(sep_row, 1, sep[:pop_w - 2])
            win.addstr(sep_row + 1, 2, f"Missing link: {missing}"[:pop_w - 3])
        except _curses.error:
            pass
        try:
            win.addstr(pop_h - 2, max(1, (pop_w - len(hint)) // 2), hint[:pop_w - 2])
        except _curses.error:
            pass
        win.refresh()

        action = None
        while True:
            key = win.getch()
            if key in (10, 13, ord("q"), ord("Q"), 27):
                break
            elif key == _curses.KEY_UP:
                action = "prev"
                break
            elif key == _curses.KEY_DOWN:
                action = "next"
                break
            elif key in (ord("d"), ord("D")):
                action = "d"
                break
            elif key in (ord("b"), ord("B")):
                action = "b"
                break
            elif key in (ord("p"), ord("P")):
                action = "r"
                break
            elif key in (ord("f"), ord("F")):
                action = "f"
                break
            elif key in (ord("h"), ord("H")):
                show_help(stdscr)
                win.touchwin()
                win.refresh()

        del win
        stdscr.touchwin()
        stdscr.refresh()
        return action

    def show_help(stdscr) -> None:
        """Show a full-command help dialog. Close with Enter, Esc, or h."""
        help_lines = [
            "NAVIGATION",
            "  ↑ / ↓          Navigate list items",
            "  PgUp / PgDn    Jump a full page",
            "  Enter          Open detail / preview popup",
            "  h              Show this help",
            "  q / Esc        Quit",
            "",
            "BROKEN LINK ACTIONS  (when a broken link is selected)",
            "  d              Delete the broken link from the file",
            "  b              Rewrite as [[broken-link|…]]",
            "  p              Strip [[ ]] brackets — leave plain text",
            "  f              Open file browser to pick a replacement",
            "",
            "ORPHAN PAGE ACTIONS  (when an orphan page is selected)",
            "  d              Delete the orphan page file from disk",
            "  k              Keep orphan, add 'orphan: false' to frontmatter",
            "",
            "STUB PAGE ACTIONS  (when a stub page is selected)",
            "  d              Delete the stub page file from disk",
            "  k              Keep stub, remove 'stub: true' from frontmatter",
            "",
            "DETAIL / PREVIEW POPUP  (opened with Enter)",
            "  ↑ / ↓          Prev / next item (links); scroll (orphans)",
            "  PgUp / PgDn    Scroll content (orphans)",
            "  d  b  p  f  k  Same actions as in the main list",
            "  h              Show this help",
            "  Enter / q      Close popup",
        ]

        height, width = stdscr.getmaxyx()
        pop_w = min(max(54, width - 8), width - 2)
        inner_w = pop_w - 4
        content_h = min(len(help_lines), height - 6)
        pop_h = min(content_h + 4, height - 2)
        pop_y = max(0, (height - pop_h) // 2)
        pop_x = max(0, (width - pop_w) // 2)
        sep = "─" * (pop_w - 2)
        close_hint = "[ ↑↓/PgUp/PgDn scroll   Esc / Enter / h to close ]"

        win = _curses.newwin(pop_h, pop_w, pop_y, pop_x)
        win.keypad(True)
        scroll = 0

        while True:
            win.erase()
            win.box()
            title = " Help "
            try:
                win.addstr(0, max(1, (pop_w - len(title)) // 2), title, _curses.A_BOLD)
                win.addstr(1, 1, sep[:pop_w - 2])
            except _curses.error:
                pass

            rows_avail = pop_h - 4
            for row in range(rows_avail):
                li = scroll + row
                if li >= len(help_lines):
                    break
                line = help_lines[li]
                try:
                    attr = _curses.A_BOLD if (line and not line.startswith(" ")) else _curses.A_NORMAL
                    win.addstr(2 + row, 2, line[:inner_w], attr)
                except _curses.error:
                    pass

            try:
                win.addstr(pop_h - 2, max(1, (pop_w - len(close_hint)) // 2), close_hint[:pop_w - 2])
            except _curses.error:
                pass
            if len(help_lines) > rows_avail:
                pct = int(100 * scroll / max(1, len(help_lines) - rows_avail))
                try:
                    win.addstr(pop_h - 2, pop_w - 5, f"{pct:3d}%")
                except _curses.error:
                    pass

            win.refresh()
            key = win.getch()
            if key in (10, 13, 27, ord("q"), ord("Q"), ord("h"), ord("H")):
                break
            elif key == _curses.KEY_UP:
                scroll = max(0, scroll - 1)
            elif key == _curses.KEY_DOWN:
                scroll = min(max(0, len(help_lines) - rows_avail), scroll + 1)
            elif key == _curses.KEY_PPAGE:
                scroll = max(0, scroll - rows_avail)
            elif key == _curses.KEY_NPAGE:
                scroll = min(max(0, len(help_lines) - rows_avail), scroll + rows_avail)

        del win
        stdscr.touchwin()
        stdscr.refresh()

    def curses_main(stdscr):
        _curses.curs_set(0)
        _curses.start_color()
        _curses.use_default_colors()
        _curses.init_pair(1, _curses.COLOR_BLACK, _curses.COLOR_CYAN)  # selected
        _curses.init_pair(2, _curses.COLOR_GREEN, -1)                  # deleted
        _curses.init_pair(3, _curses.COLOR_YELLOW, -1)                 # marked broken
        _curses.init_pair(4, _curses.COLOR_MAGENTA, -1)               # replaced
        _curses.init_pair(5, _curses.COLOR_YELLOW, -1)                 # broken link in popup
        _curses.init_pair(6, _curses.COLOR_WHITE, -1)                  # filename in list
        _curses.init_pair(7, _curses.COLOR_CYAN, -1)                   # file line number in list
        _curses.init_pair(8, _curses.COLOR_BLUE, -1)                   # delinked (plain text)
        _curses.init_pair(9, _curses.COLOR_GREEN, -1)                  # kept orphan
        _curses.init_pair(10, _curses.COLOR_RED, -1)                   # unhandled orphan
        _curses.init_pair(11, _curses.COLOR_CYAN, -1)                  # unhandled stub

        selected = 0
        offset = 0
        n_links = sum(1 for it in all_items if it["_kind"] == "link")
        n_orps = sum(1 for it in all_items if it["_kind"] == "orphan")
        n_stubs = sum(1 for it in all_items if it["_kind"] == "stub")

        def redraw():
            nonlocal offset
            stdscr.erase()
            height, width = stdscr.getmaxyx()
            list_height = height - 4

            header = f"Broken links: {n_links}   Orphans: {n_orps}   Stubs: {n_stubs}"
            stdscr.addstr(0, 0, header[:width - 1])

            sel_kind = all_items[selected]["_kind"] if n > 0 else "link"
            if sel_kind == "orphan":
                hint = "UP/DOWN/PgUp/PgDn navigate   ENTER=preview   d=delete   k=keep as orphan   h=help   q=quit"
            elif sel_kind == "stub":
                hint = "UP/DOWN/PgUp/PgDn navigate   ENTER=preview   d=delete   k=keep stub   h=help   q=quit"
            else:
                hint = "UP/DOWN/PgUp/PgDn navigate   ENTER=preview   d=delete   b=mark broken   p=plain text   f=find link   h=help   q=quit"
            stdscr.addstr(1, 0, hint[:width - 1])
            stdscr.addstr(2, 0, ("─" * (width - 1))[:width - 1])

            if selected < offset:
                offset = selected
            elif selected >= offset + list_height:
                offset = selected - list_height + 1

            for row in range(list_height):
                idx = offset + row
                if idx >= n:
                    break
                item = all_items[idx]
                state = states[idx]
                is_orphan = item["_kind"] == "orphan"
                is_stub = item["_kind"] == "stub"
                if state == "deleted":
                    prefix = "[DELD] "; state_attr = _curses.color_pair(2)
                elif state == "broken":
                    prefix = "[BRKN] "; state_attr = _curses.color_pair(3)
                elif state == "replaced":
                    prefix = "[FIXD] "; state_attr = _curses.color_pair(4)
                elif state == "delinked":
                    prefix = "[TEXT] "; state_attr = _curses.color_pair(8)
                elif state == "kept":
                    prefix = "[KEEP] "; state_attr = _curses.color_pair(9)
                elif is_orphan:
                    prefix = "[ORPH] "; state_attr = _curses.color_pair(10)
                elif is_stub:
                    prefix = "[STUB] "; state_attr = _curses.color_pair(11)
                else:
                    prefix = "       "; state_attr = _curses.A_NORMAL
                y = 3 + row
                avail = width - 1
                if idx == selected:
                    try:
                        stdscr.addstr(y, 0, (prefix + fmt_line(idx))[:avail], _curses.color_pair(1) | _curses.A_BOLD)
                    except _curses.error:
                        pass
                    continue
                x = 0
                if is_orphan or is_stub:
                    file_w = max(10, avail - len(prefix) - 3 - 7)  # prefix + num(3) + "  file:"(7)
                    segments = [
                        (prefix,                         state_attr),
                        (f"{idx + 1:3d}",                _curses.color_pair(2)),
                        ("  file:",                      _curses.A_DIM),
                        (item['file'][:file_w],          _curses.color_pair(6) | _curses.A_BOLD),
                    ]
                else:
                    segments = [
                        (prefix,                       state_attr),
                        (f"{idx + 1:3d}",              _curses.color_pair(2)),
                        ("  file:",                    _curses.A_DIM),
                        (truncate_path(item['file']),  _curses.color_pair(6) | _curses.A_BOLD),
                        (", line:",                    _curses.A_DIM),
                        (str(item['line']),            _curses.color_pair(7)),
                        (", link:",                    _curses.A_DIM),
                        (item['target'],               _curses.color_pair(5) | _curses.A_BOLD),
                    ]
                for text, attr in segments:
                    if x >= avail or not text:
                        continue
                    try:
                        stdscr.addstr(y, x, text[:avail - x], attr)
                    except _curses.error:
                        pass
                    x += len(text)

            done = sum(1 for s in states if s is not None)
            status = messages[selected] if messages[selected] else ""
            footer = f"  {done}/{n} handled" + (f"  — {status}" if status else "")
            try:
                stdscr.addstr(height - 1, 0, footer[:width - 1])
            except _curses.error:
                pass
            stdscr.refresh()

        while True:
            redraw()
            key = stdscr.getch()

            if key in (ord("q"), ord("Q"), 27):
                break
            elif key in (ord("h"), ord("H")):
                show_help(stdscr)
            elif key == _curses.KEY_UP:
                if selected > 0:
                    selected -= 1
            elif key == _curses.KEY_DOWN:
                if selected < n - 1:
                    selected += 1
            elif key == _curses.KEY_PPAGE:
                height, _ = stdscr.getmaxyx()
                page = max(1, height - 4 - 1)
                selected = max(0, selected - page)
            elif key == _curses.KEY_NPAGE:
                height, _ = stdscr.getmaxyx()
                page = max(1, height - 4 - 1)
                selected = min(n - 1, selected + page)
            elif key in (10, 13):  # Enter — open popup for the selected item
                idx = selected
                while True:
                    redraw()
                    item = all_items[idx]
                    if item["_kind"] == "orphan":
                        action = show_orphan_preview(stdscr, item, idx)
                        if action == "d":
                            res = do_delete_orphan(idx)
                            states[idx] = "deleted" if res == "deleted" else None
                            messages[idx] = "File deleted." if res == "deleted" else res
                        elif action == "k":
                            res = do_keep_orphan(idx)
                            states[idx] = "kept" if res in ("kept", "already kept") else None
                            messages[idx] = res
                        elif action == "next":
                            if idx < n - 1:
                                idx += 1; selected = idx
                            continue
                        elif action == "prev":
                            if idx > 0:
                                idx -= 1; selected = idx
                            continue
                    elif item["_kind"] == "stub":
                        action = show_stub_preview(stdscr, item, idx)
                        if action == "d":
                            res = do_delete_orphan(idx)
                            states[idx] = "deleted" if res == "deleted" else None
                            messages[idx] = "File deleted." if res == "deleted" else res
                        elif action == "k":
                            res = do_remove_stub(idx)
                            states[idx] = "kept" if res == "unstubbed" else None
                            messages[idx] = "stub: true removed." if res == "unstubbed" else res
                        elif action == "next":
                            if idx < n - 1:
                                idx += 1; selected = idx
                            continue
                        elif action == "prev":
                            if idx > 0:
                                idx -= 1; selected = idx
                            continue
                    else:
                        action = show_popup(stdscr, item, idx)
                        if action == "d":
                            res = do_delete(idx)
                            states[idx] = "deleted" if res == "deleted" else None
                            messages[idx] = "Link removed." if res == "deleted" else res
                        elif action == "b":
                            res = do_broken(idx)
                            states[idx] = "broken" if res == "broken" else None
                            messages[idx] = "Marked [[broken-link|…]]." if res == "broken" else res
                        elif action == "r":
                            res = do_delink(idx)
                            states[idx] = "delinked" if res == "delinked" else None
                            messages[idx] = "Brackets removed (plain text)." if res == "delinked" else res
                        elif action == "f":
                            new_rel = show_file_browser(stdscr, item.get("target", ""))
                            if new_rel is not None:
                                res = do_find_replace(idx, new_rel)
                                states[idx] = "replaced" if res == "replaced" else None
                                messages[idx] = f"→ {new_rel.stem}" if res == "replaced" else res
                        elif action == "next":
                            if idx < n - 1:
                                idx += 1; selected = idx
                            continue
                        elif action == "prev":
                            if idx > 0:
                                idx -= 1; selected = idx
                            continue
                    if action in ("d", "b", "r", "k") or (action == "f" and states[idx] is not None):
                        next_idx = next((i for i in range(idx + 1, n) if states[i] is None), None)
                        if next_idx is not None:
                            idx = next_idx; selected = idx
                            continue
                    selected = idx
                    break
            elif key in (ord("d"), ord("D")):
                if states[selected] is None:
                    item = all_items[selected]
                    if item["_kind"] in ("orphan", "stub"):
                        res = do_delete_orphan(selected)
                        states[selected] = "deleted" if res == "deleted" else None
                        messages[selected] = "File deleted." if res == "deleted" else res
                    else:
                        res = do_delete(selected)
                        states[selected] = "deleted" if res == "deleted" else None
                        messages[selected] = "Link removed." if res == "deleted" else res
            elif key in (ord("k"), ord("K")):
                if states[selected] is None:
                    if all_items[selected]["_kind"] == "orphan":
                        res = do_keep_orphan(selected)
                        states[selected] = "kept" if res in ("kept", "already kept") else None
                        messages[selected] = res
                    elif all_items[selected]["_kind"] == "stub":
                        res = do_remove_stub(selected)
                        states[selected] = "kept" if res == "unstubbed" else None
                        messages[selected] = "stub: true removed." if res == "unstubbed" else res
            elif key in (ord("b"), ord("B")):
                if states[selected] is None and all_items[selected]["_kind"] == "link":
                    res = do_broken(selected)
                    states[selected] = "broken" if res == "broken" else None
                    messages[selected] = "Marked [[broken-link|…]]." if res == "broken" else res
            elif key in (ord("p"), ord("P")):
                if states[selected] is None and all_items[selected]["_kind"] == "link":
                    res = do_delink(selected)
                    states[selected] = "delinked" if res == "delinked" else None
                    messages[selected] = "Brackets removed (plain text)." if res == "delinked" else res
            elif key in (ord("f"), ord("F")):
                if states[selected] is None and all_items[selected]["_kind"] == "link":
                    new_rel = show_file_browser(stdscr, all_items[selected].get("target", ""))
                    if new_rel is not None:
                        res = do_find_replace(selected, new_rel)
                        states[selected] = "replaced" if res == "replaced" else None
                        messages[selected] = f"→ {new_rel.stem}" if res == "replaced" else res

    _curses.wrapper(curses_main)

    deleted_links   = sum(1 for i, s in enumerate(states) if s == "deleted" and all_items[i]["_kind"] == "link")
    deleted_orphans = sum(1 for i, s in enumerate(states) if s == "deleted" and all_items[i]["_kind"] == "orphan")
    deleted_stubs   = sum(1 for i, s in enumerate(states) if s == "deleted" and all_items[i]["_kind"] == "stub")
    broken_count    = sum(1 for s in states if s == "broken")
    replaced_count  = sum(1 for s in states if s == "replaced")
    delinked_count  = sum(1 for s in states if s == "delinked")
    kept_orphans    = sum(1 for i, s in enumerate(states) if s == "kept" and all_items[i]["_kind"] == "orphan")
    kept_stubs      = sum(1 for i, s in enumerate(states) if s == "kept" and all_items[i]["_kind"] == "stub")
    skipped = n - sum(1 for s in states if s is not None)
    print(f"\nSession complete: {deleted_links} links deleted, {broken_count} marked broken, "
          f"{delinked_count} plain text, {replaced_count} replaced, "
          f"{deleted_orphans} orphan pages deleted, {kept_orphans} orphans kept, "
          f"{deleted_stubs} stub pages deleted, {kept_stubs} stubs resolved, {skipped} skipped.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        prog="check-broken-links.py",
        description=(
            "Scan Markdown files for broken internal and external links.\n"
            "Output is structured JSON (default) or human-readable text, "
            "designed for AI consumption."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Scan vault rooted at the script's parent directory:
  python3 wiki-lint-check.py

  # Scan a specific vault directory:
  python3 wiki-lint-check.py /path/to/vault

  # Human-readable output:
  python3 wiki-lint-check.py --format text

  # Include external HTTP link checks:
  python3 wiki-lint-check.py --external --timeout 10

  # Include image embeds in checks:
  python3 wiki-lint-check.py --include-images

  # Skip frontmatter links (e.g. author: [[Name]] in raw/clips):
  python3 wiki-lint-check.py --skip-frontmatter

  # Show suggested fixes for broken wikilinks, then apply them:
  python3 wiki-lint-check.py --format text
  python3 wiki-lint-check.py --fix-simple-errors

  # Combine options:
  python3 wiki-lint-check.py --external --include-images --skip-frontmatter --format text /path/to/vault
        """,
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        help="Root directory of the vault (default: parent of this script's directory)",
    )
    parser.add_argument(
        "--external",
        action="store_true",
        help="Also check HTTP/HTTPS links (requires network access; slow)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=5,
        metavar="N",
        help="Timeout in seconds for external HTTP requests (default: 5)",
    )
    parser.add_argument(
        "--include-images",
        action="store_true",
        help="Also check embedded image links (![[...]] and ![alt](...))",
    )
    parser.add_argument(
        "--format",
        choices=["json", "text"],
        default="json",
        help="Output format: 'json' for AI (default), 'text' for humans",
    )
    parser.add_argument(
        "--skip-frontmatter",
        action="store_true",
        help="Do not check links inside YAML frontmatter (useful to ignore author/tag references)",
    )
    parser.add_argument(
        "--remove-broken-links", "--remove",
        action="store_true",
        dest="remove_broken_links",
        help=(
            "Rewrite broken wikilinks in-place to mark them visually. "
            "[[broken]] becomes [[broken|(broken link) broken]] and "
            "[[broken|text]] becomes [[broken|(broken link) text]], "
            "preserving the original target while flagging it in the display text."
        ),
    )
    parser.add_argument(
        "--fix-simple-errors", "--fix",
        action="store_true",
        dest="fix_simple_errors",
        help=(
            "Rewrite broken wikilinks where a unique normalized match is found. "
            "Characters like ':' are often replaced by '_' in filenames or omitted "
            "in link text; this flag repairs such mismatches in-place."
        ),
    )
    parser.add_argument(
        "--fix-orphans",
        action="store_true",
        dest="fix_orphans",
        help=(
            "For each orphaned wiki page, find plain-text references to its name in wiki/ "
            "files and replace them with wikilinks. Only modifies files inside wiki/."
        ),
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress progress messages written to stderr",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help=(
            "After scanning, open an interactive TUI to fix broken links one by one. "
            "Use UP/DOWN to navigate, d to delete the link, b to mark it as [[broken-link|…]]."
        ),
    )
    return parser, parser.parse_args()


def main():
    parser, args = parse_args()

    # Determine root directory
    if args.root:
        root = Path(args.root).resolve()
    else:
        # Default: parent of the 'scripts' directory (i.e., the vault root)
        script_dir = Path(__file__).resolve().parent
        if script_dir.name == "scripts":
            root = script_dir.parent
        else:
            root = script_dir

    if not root.exists():
        msg = f"Error: directory does not exist: {root}"
        if args.format == "json":
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    if not root.is_dir():
        msg = f"Error: not a directory: {root}"
        if args.format == "json":
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    try:
        result = check_vault(root, args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
    except Exception as e:
        msg = f"Unexpected error: {e}"
        if args.format == "json":
            print(json.dumps({"error": msg}, indent=2))
        else:
            print(msg, file=sys.stderr)
        sys.exit(1)

    if getattr(args, "fix_simple_errors", False) and not args.interactive:
        result["broken_links"] = [b for b in result["broken_links"] if "suggested_fix" in b or b.get("fm_deleted")]
        result["summary"]["broken"] = len(result["broken_links"])

    orphan_result = check_orphans(root, args.quiet)
    result["orphans"] = orphan_result["orphans"]
    result["orphan_summary"] = orphan_result["summary"]

    stub_result = check_stubs(root, args.quiet)
    result["stubs"] = stub_result["stubs"]
    result["stub_summary"] = stub_result["summary"]

    if getattr(args, "fix_orphans", False) and orphan_result["orphans"]:
        fix_result = fix_orphans(orphan_result["orphans"], root, args.quiet)
        result["orphan_fix"] = fix_result
        if fix_result["orphans_resolved"] > 0:
            updated = check_orphans(root, quiet=True)
            result["orphans"] = updated["orphans"]
            result["orphan_summary"] = updated["summary"]

    has_issues = (
        result["summary"]["broken"] > 0
        or result.get("orphan_summary", {}).get("orphans_found", 0) > 0
        or result.get("stub_summary", {}).get("stubs_found", 0) > 0
    )

    if args.interactive:
        run_interactive(result["broken_links"], result.get("orphans", []), result.get("stubs", []), root)
    elif args.format == "json":
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        print(format_text(result))

    if not args.interactive and has_issues:
        print("\nTip: run with --interactive to review and fix issues one by one.", file=sys.stderr)

    # Exit code: 0 = clean, 1 = issues found, 2 = errors
    if result.get("errors"):
        sys.exit(2)
    if has_issues:
        sys.exit(1)
    sys.exit(0)


if __name__ == "__main__":
    main()
