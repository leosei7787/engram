#!/usr/bin/env python3
"""
wiki-create-index-pages.py — Rebuild wiki index pages.

Creates:
  wiki/index.md          — root index linking to all topic sections
  wiki/<topic>/_index.md — per-topic index listing all pages in that section

Run from any directory; the wiki path is resolved relative to this script's
parent by default, or override with --wiki-dir.
"""
import argparse
import datetime
import os
import re
import sys

TOPIC_DIRS: dict[str, tuple[str, str]] = {
    "competition":   ("Competitors",   "Competing companies, products, and approaches."),
    "concepts":      ("Concepts",      "Technologies, standards, mental models, and domain vocabulary."),
    "conversations": ("Conversations", "Valuable results of earlier queries and conversations."),
    "decisions":     ("Decisions",     "Why decisions were taken, on what basis, by whom, and when."),
    "people":        ("People",        "Colleagues, contacts, external stakeholders, and teams."),
    "problems":      ("Problems",      "Active and past problems."),
    "projects":      ("Projects",      "Active and past initiatives."),
    "systems":       ("Systems",       "Our products, platforms, and services."),
}


def get_title_and_summary(filepath: str) -> tuple[str, str]:
    """Return (h1 title, first-sentence summary) from a markdown file."""
    stem = os.path.splitext(os.path.basename(filepath))[0]
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as exc:
        print(f"WARNING: cannot read {filepath}: {exc}", file=sys.stderr)
        return stem, "No summary available."

    # Skip YAML front matter
    start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                start = i + 1
                break

    title: str | None = None
    summary: str | None = None
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if title is None and stripped.startswith("#"):
            title = re.sub(r"^#+\s*", "", stripped)
        elif title is not None and summary is None and not stripped.startswith("#"):
            summary = stripped
            break

    if title is None:
        title = stem
    if summary is None:
        summary = "No summary available."

    # Truncate to first sentence
    m = re.match(r"^(.+?[.!?])\s", summary + " ")
    if m:
        summary = m.group(1)

    return title, summary


def write_file(path: str, content: str, dry_run: bool) -> None:
    if not dry_run:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)


def build_topic_index(
    wiki_dir: str,
    topic_key: str,
    type_name: str,
    description: str,
    today: str,
    dry_run: bool,
    verbose: bool,
) -> int:
    """Build _index.md for one topic directory. Returns number of entries written."""
    dirpath = os.path.join(wiki_dir, topic_key)
    if not os.path.isdir(dirpath):
        print(f"WARNING: directory not found, skipping: {topic_key}/", file=sys.stderr)
        return 0

    files = sorted(
        f for f in os.listdir(dirpath)
        if f.endswith(".md") and f != "_index.md"
    )

    entries = []
    for fname in files:
        fpath = os.path.join(dirpath, fname)
        title, summary = get_title_and_summary(fpath)
        stem = os.path.splitext(fname)[0]
        # Use filename stem for the link path; show title as display text
        link = f"[[wiki/{topic_key}/{stem}|{title}]]"
        entries.append(f"- {link} — {summary}")

    entry_list = "\n".join(entries) if entries else "_No pages yet._"
    content = (
        f"---\ntype: index\ndate: {today}\n---\n"
        f"# {type_name} - index\n"
        f"[[wiki/index|← Index]]\n\n"
        f"{description}\n\n"
        f"{entry_list}\n"
    )

    index_path = os.path.join(dirpath, "_index.md")
    write_file(index_path, content, dry_run)
    verb = "would write" if dry_run else "wrote"
    print(f"{verb} {os.path.relpath(index_path)} ({len(entries)} entries)")
    return len(entries)


def build_root_index(wiki_dir: str, today: str, dry_run: bool, verbose: bool) -> None:
    """Build wiki/index.md linking to all topic _index.md pages."""
    rows = [
        f"---\ntype: index\ndate: {today}\n---\n",
        "# Wiki Index\n\n",
        "| Section | Description |\n",
        "|---------|-------------|\n",
    ]
    for topic_key, (type_name, description) in TOPIC_DIRS.items():
        link = f"[[wiki/{topic_key}/_index\\|{type_name}]]"
        rows.append(f"| {link} | {description} |\n")

    content = "".join(rows)
    index_path = os.path.join(wiki_dir, "index.md")
    write_file(index_path, content, dry_run)
    verb = "would write" if dry_run else "wrote"
    print(f"{verb} {os.path.relpath(index_path)}")


def resolve_wiki_dir(cli_arg: str | None) -> str:
    if cli_arg:
        return os.path.abspath(cli_arg)
    # Default: wiki/ is a sibling of the scripts/ directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(os.path.dirname(script_dir), "wiki")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--wiki-dir",
        metavar="PATH",
        help=(
            "Path to the wiki directory. "
            "Defaults to 'wiki/' relative to this script's parent directory."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be written without creating any files.",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Verbose output (implied by --dry-run).",
    )
    args = parser.parse_args()

    wiki_dir = resolve_wiki_dir(args.wiki_dir)
    if not os.path.isdir(wiki_dir):
        print(f"ERROR: wiki directory not found: {wiki_dir}", file=sys.stderr)
        sys.exit(1)

    today = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    print(f"Rebuilding wiki index pages in {os.path.relpath(wiki_dir)}/", flush=True)
    if args.verbose or args.dry_run:
        print()

    try:
        total = 0
        for topic_key, (type_name, description) in TOPIC_DIRS.items():
            total += build_topic_index(
                wiki_dir, topic_key, type_name, description,
                today, args.dry_run, args.verbose,
            )

        build_root_index(wiki_dir, today, args.dry_run, args.verbose)
    except OSError as exc:
        print(f"\nERROR: {exc}", file=sys.stderr)
        sys.exit(1)

    n_files = len(TOPIC_DIRS) + 1
    suffix = f"{total} entries" if args.verbose or args.dry_run else ""
    print(f"\nDone — {n_files} index files written." + (f" {suffix}" if suffix else ""))


if __name__ == "__main__":
    main()
