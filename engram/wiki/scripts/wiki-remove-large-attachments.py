#!/usr/bin/env python3
"""clean-attachments.py — Interactive Obsidian attachment cleaner.

Shows a help popup on startup — press Enter to dismiss and start browsing.
Navigate large attachments with ↑↓, press d/D to move to .trash/ and
update all markdown links. Press q to quit.
"""

import curses
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

VAULT = Path.cwd()
TRASH = VAULT / ".trash"
LOG = VAULT / "wiki" / "log.md"
MIN_SIZE = 50 * 1024  # show files >= 50 KB

SKIP_DIRS = {".obsidian", ".trash", ".git", ".claude", ".junie", ".agents", "scripts", "templates", "config"}

ATTACHMENT_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".tiff", ".tif",
    ".pdf",
    ".mp4", ".mov", ".avi", ".mkv", ".m4a", ".mp3", ".wav", ".ogg", ".flac",
    ".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt",
    ".zip", ".tar", ".gz", ".7z",
    ".eml", ".vtt", ".csv", ".html", ".htm",
}


# ── data ────────────────────────────────────────────────────────────────────

def find_attachments() -> list[tuple[int, Path]]:
    """Return (size, path) pairs for all large attachments, largest first."""
    results: list[tuple[int, Path]] = []
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = sorted(
            d for d in dirs
            if d not in SKIP_DIRS and not d.startswith(".")
        )
        for name in files:
            if Path(name).suffix.lower() not in ATTACHMENT_EXTS:
                continue
            path = Path(root) / name
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size >= MIN_SIZE:
                results.append((size, path))
    results.sort(reverse=True)
    return results


def fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    return f"{n / 1024:.0f} KB"


# ── deletion logic ───────────────────────────────────────────────────────────

def _unique_trash_path(path: Path) -> Path:
    dest = TRASH / path.name
    if not dest.exists():
        return dest
    stem, suffix = path.stem, path.suffix
    i = 1
    while True:
        dest = TRASH / f"{stem}_{i}{suffix}"
        if not dest.exists():
            return dest
        i += 1


def update_links(md_path: Path, fname: str) -> bool:
    """Replace every link/embed of fname in md_path with '(attachment deleted)'.
    Returns True if the file was changed."""
    try:
        text = md_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False

    # Match wikilink embeds:  ![[fname]] or ![[any/path/fname]]
    # Match wikilink refs:    [[fname]] or [[any/path/fname]]
    # Match md images:        ![alt](any/path/fname)
    # Match md links:         [text](any/path/fname)
    esc = re.escape(fname)
    patterns = [
        rf"!\[\[[^\]]*{esc}\]\]",
        rf"\[\[[^\]]*{esc}\]\]",
        rf"!\[[^\]]*\]\([^)]*{esc}\)",
        rf"\[[^\]]*\]\([^)]*{esc}\)",
    ]
    new = text
    for pat in patterns:
        new = re.sub(pat, "(attachment deleted)", new)

    if new == text:
        return False
    md_path.write_text(new, encoding="utf-8")
    return True


def collect_md_files() -> list[Path]:
    results: list[Path] = []
    for root, dirs, files in os.walk(VAULT):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if name.endswith(".md"):
                results.append(Path(root) / name)
    return results


def delete_attachment(path: Path, size: int) -> list[str]:
    """Move to trash, update links, append log. Returns list of updated md paths."""
    TRASH.mkdir(exist_ok=True)

    # Update links before moving so we still have the original path info
    changed: list[str] = []
    for md in collect_md_files():
        if update_links(md, path.name):
            changed.append(str(md.relative_to(VAULT)))

    # Move to trash
    dest = _unique_trash_path(path)
    shutil.move(str(path), str(dest))

    # Append to log
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    rel = str(path.relative_to(VAULT))
    entry = f"\n### [{ts}] attachment deleted | `{rel}`\n"
    entry += f"- Size: {fmt_size(size)}. Moved to `.trash/{dest.name}`.\n"
    if changed:
        files_list = ", ".join(f"`{p}`" for p in changed)
        entry += f"- Updated links in: {files_list}\n"
    else:
        entry += "- No markdown links found.\n"

    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(entry)
    except OSError:
        pass

    return changed


# ── TUI ─────────────────────────────────────────────────────────────────────

HELP = "↑↓ navigate  d/D delete  o/O open  q quit"

HELP_LINES = [
    "Obsidian Attachment Cleaner",
    "",
    "  Select large attachments and move them to .trash/.",
    "  Any notes that reference the deleted attachment are updated automatically.",
    "",
    "  ↑ / ↓      Navigate the list",
    "  d / D      Delete selected attachment",
    "             (asks for confirmation first)",
    "  o / O      Open file with Mac 'open' command",
    "  q / Q      Quit",
    "",
    "Deleted files are moved to .trash/ — not",
    "permanently removed. Markdown links are",
    "updated to '(attachment deleted)'.",
    "",
    f"  Vault: {VAULT}",
    f"  Min size: {50} KB",
    "",
    "Press Enter to start, Esc to exit …",
]


def show_updated_notes_popup(stdscr, changed: list[str]):
    """Show a centered dialog listing which notes were updated. Enter to close."""
    title = "Notes updated:"
    lines = [title, ""] + [f"  {p}" for p in changed] + ["", "Press Enter to close …"]
    curses.curs_set(0)
    while True:
        h, w = stdscr.getmaxyx()
        box_w = min(max(len(title) + 6, max((len(l) for l in lines), default=40) + 4), w - 4)
        box_h = len(lines) + 2
        top = max(0, (h - box_h) // 2)
        left = max(0, (w - box_w) // 2)

        # Dim background by redrawing stdscr (already drawn); just draw popup on top
        for r in range(box_h):
            row = top + r
            if row >= h:
                break
            if r == 0:
                line = "┌" + "─" * (box_w - 2) + "┐"
            elif r == box_h - 1:
                line = "└" + "─" * (box_w - 2) + "┘"
            else:
                content = lines[r - 1] if r - 1 < len(lines) else ""
                line = "│" + content.ljust(box_w - 2)[:box_w - 2] + "│"
            try:
                stdscr.addstr(row, left, line[:w - left - 1])
            except curses.error:
                pass

        stdscr.refresh()
        key = stdscr.getch()
        if key in (10, 13, 27):  # Enter or Esc
            return


def show_help_popup(stdscr) -> bool:
    """Draw a centered help popup. Returns True to continue, False to exit (Esc)."""
    curses.curs_set(0)
    while True:
        h, w = stdscr.getmaxyx()
        box_w = min(120, w - 4)
        box_h = len(HELP_LINES) + 2
        top = max(0, (h - box_h) // 2)
        left = max(0, (w - box_w) // 2)

        stdscr.erase()

        # Draw box border
        for r in range(box_h):
            row = top + r
            if row >= h:
                break
            if r == 0:
                line = "┌" + "─" * (box_w - 2) + "┐"
            elif r == box_h - 1:
                line = "└" + "─" * (box_w - 2) + "┘"
            else:
                content = HELP_LINES[r - 1] if r - 1 < len(HELP_LINES) else ""
                line = "│" + content.ljust(box_w - 2)[:box_w - 2] + "│"
            try:
                stdscr.addstr(row, left, line[:w - left - 1])
            except curses.error:
                pass

        stdscr.refresh()
        key = stdscr.getch()
        if key in (10, 13):   # Enter → continue
            return True
        if key == 27:          # Esc → exit
            return False


def draw(stdscr, attachments, cursor, scroll, status, status_color):
    h, w = stdscr.getmaxyx()
    list_h = h - 3  # rows 2..(h-2) for list; row 0 header, row 1 col titles, row h-1 status

    stdscr.erase()

    # Row 0 — title bar
    title = " Obsidian Attachment Cleaner "
    stdscr.attron(curses.color_pair(1) | curses.A_BOLD)
    stdscr.addstr(0, 0, title.ljust(w - 1)[:w - 1])
    stdscr.attroff(curses.color_pair(1) | curses.A_BOLD)

    # Row 1 — column headers
    col = f"  {'SIZE':>8}   {'FILENAME'}"
    stdscr.attron(curses.A_BOLD)
    stdscr.addstr(1, 0, col[:w - 1])
    stdscr.attroff(curses.A_BOLD)

    # Rows 2..h-2 — file list
    if not attachments:
        stdscr.addstr(3, 2, "No large attachments found.")
    else:
        for i in range(min(list_h, len(attachments) - scroll)):
            idx = i + scroll
            size, path = attachments[idx]
            rel = str(path.relative_to(VAULT))
            line = f"  {fmt_size(size):>8}   {rel}"
            line = line[:w - 1].ljust(w - 1)
            row = i + 2
            if idx == cursor:
                stdscr.attron(curses.color_pair(2))
                stdscr.addstr(row, 0, line)
                stdscr.attroff(curses.color_pair(2))
            else:
                stdscr.addstr(row, 0, line)

        # Scroll indicators
        if scroll > 0:
            stdscr.addstr(2, w - 3, " ▲ ")
        if scroll + list_h < len(attachments):
            stdscr.addstr(h - 2, w - 3, " ▼ ")

    # Bottom status bar
    stdscr.attron(curses.color_pair(status_color))
    stdscr.addstr(h - 1, 0, status[:w - 1].ljust(w - 1))
    stdscr.attroff(curses.color_pair(status_color))

    stdscr.refresh()


def main(stdscr):
    curses.curs_set(0)
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_BLACK, curses.COLOR_CYAN)   # title bar
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_WHITE)  # selected row
    curses.init_pair(3, curses.COLOR_GREEN, -1)                  # success
    curses.init_pair(4, curses.COLOR_RED, -1)                    # error / confirm
    curses.init_pair(5, curses.COLOR_YELLOW, -1)                 # info

    if not show_help_popup(stdscr):
        return

    # Loading indicator
    stdscr.erase()
    stdscr.addstr(0, 0, "Scanning vault for large attachments…")
    stdscr.refresh()

    attachments = find_attachments()
    cursor = 0
    scroll = 0

    def default_status():
        return f"{HELP}  [{len(attachments)} files]", 5

    status, sc = default_status()

    while True:
        h, w = stdscr.getmaxyx()
        list_h = h - 3

        # Keep scroll window sane
        if cursor < scroll:
            scroll = cursor
        elif cursor >= scroll + list_h:
            scroll = cursor - list_h + 1

        draw(stdscr, attachments, cursor, scroll, status, sc)

        key = stdscr.getch()

        if key in (ord("q"), ord("Q"), 27):
            break

        elif key == curses.KEY_UP:
            if cursor > 0:
                cursor -= 1
            status, sc = default_status()

        elif key == curses.KEY_DOWN:
            if cursor < len(attachments) - 1:
                cursor += 1
            status, sc = default_status()

        elif key in (ord("o"), ord("O")):
            if attachments:
                _, path = attachments[cursor]
                subprocess.Popen(["open", str(path)])
                status = f"Opened {path.name}"
                sc = 5

        elif key in (ord("d"), ord("D")):
            if not attachments:
                continue
            size, path = attachments[cursor]
            fname = path.name

            # Confirm prompt
            confirm = f" Delete {fname} ({fmt_size(size)})? Enter=confirm  other=cancel "
            stdscr.attron(curses.color_pair(4) | curses.A_BOLD)
            stdscr.addstr(h - 1, 0, confirm[:w - 1].ljust(w - 1))
            stdscr.attroff(curses.color_pair(4) | curses.A_BOLD)
            stdscr.refresh()

            ck = stdscr.getch()
            if ck in (curses.KEY_ENTER, 10, 13):
                try:
                    changed = delete_attachment(path, size)
                    attachments.pop(cursor)
                    if cursor >= len(attachments) and cursor > 0:
                        cursor -= 1
                    n = len(changed)
                    status = f"Deleted {fname}. Links updated in {n} file(s).  {HELP}  [{len(attachments)} files]"
                    sc = 3
                    if changed:
                        draw(stdscr, attachments, cursor, scroll, status, sc)
                        show_updated_notes_popup(stdscr, changed)
                except Exception as exc:
                    status = f"Error: {exc}"
                    sc = 4
            else:
                status, sc = default_status()


def cli_main():
    if "--help" in sys.argv or "-h" in sys.argv:
        print(__doc__)
        print(f"Vault: {VAULT}")
        print(f"Min size shown: {fmt_size(MIN_SIZE)}")
        sys.exit(0)
    curses.wrapper(main)


if __name__ == "__main__":
    cli_main()
