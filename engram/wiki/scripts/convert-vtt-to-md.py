#!/usr/bin/env python3
"""convert-vtt-to-md — convert .vtt transcript files to Markdown for wiki ingestion.

Each .vtt is converted to a .md file (sibling by default, or in --output-dir)
with YAML frontmatter and a readable transcript body.  Consecutive cues from
the same speaker are merged into a single paragraph.

Speaker detection supports two VTT conventions:
  • Voice tags  — <v Speaker Name>text</v>
  • Colon prefix — Speaker Name: text  (first word(s) before the first colon)

EXIT CODES
  0  all files converted successfully (or nothing to do)
  1  one or more files failed or an argument error occurred

OUTPUT FORMAT (for AI tools)
  Each action is prefixed with a tag:
    [OK]    successful step
    [WARN]  non-fatal issue (e.g. no speakers found, fallback used)
    [ERROR] file skipped due to unrecoverable error
    [INFO]  informational (rename, summary)

EXAMPLES
  # Convert a single file
  python3 convert-vtt-to-md.py "raw/transcripts/Meeting.vtt"

  # Provide a human-readable title (used as the H1 heading and frontmatter title)
  python3 convert-vtt-to-md.py "raw/transcripts/Meeting.vtt" --title "Q2 Planning Meeting"

  # Override the date (useful when the filename has no date)
  python3 convert-vtt-to-md.py "raw/transcripts/Meeting.vtt" --date 2026-04-15

  # Convert all .vtt in a directory (already-converted files skipped by default)
  python3 convert-vtt-to-md.py --input-dir raw/transcripts

  # Force reconversion even if .md already exists
  python3 convert-vtt-to-md.py --input-dir raw/transcripts --force

  # Write .md files to a different directory
  python3 convert-vtt-to-md.py --input-dir raw/transcripts --output-dir raw/transcripts/converted

  # Dry run — show what would happen without writing anything
  python3 convert-vtt-to-md.py --input-dir raw/transcripts --dry-run

  # Omit inline timestamps from the transcript body (timestamps are on by default)
  python3 convert-vtt-to-md.py "raw/transcripts/Meeting.vtt" --no-timestamps
"""

import argparse
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

_WARNINGS: list[str] = []
_ERRORS: list[str] = []


def _log(tag: str, msg: str, file=None) -> None:
    print(f"  {tag} {msg}", file=file or sys.stdout)


def warn(msg: str) -> None:
    _log("[WARN]", msg)
    _WARNINGS.append(msg)


def error(msg: str) -> None:
    _log("[ERROR]", msg, file=sys.stderr)
    _ERRORS.append(msg)


def ok(msg: str) -> None:
    _log("[OK]", msg)


def info(msg: str) -> None:
    _log("[INFO]", msg)


# ---------------------------------------------------------------------------
# VTT parsing
# ---------------------------------------------------------------------------

_TIMESTAMP_RE = re.compile(
    r"(\d{1,2}):(\d{2}):(\d{2})[.,](\d{3})"  # HH:MM:SS.mmm
    r"|(\d{1,2}):(\d{2})[.,](\d{3})"          # MM:SS.mmm (short form)
)
_CUE_ARROW_RE = re.compile(r"-->")
_VOICE_TAG_RE = re.compile(r"<v\s+([^>]+)>")
_HTML_TAG_RE  = re.compile(r"<[^>]+>")

# Colon-speaker heuristic: up to 4 words before the first colon (enforced by regex)
_COLON_SPEAKER_RE = re.compile(r"^((?:\S+\s+){0,3}\S+):\s+(.+)$", re.DOTALL)


def _ts_to_seconds(m: re.Match) -> float:
    """Convert a timestamp regex match to total seconds."""
    if m.group(1) is not None:
        # HH:MM:SS.mmm
        h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
        return h * 3600 + mn * 60 + s + ms / 1000
    else:
        # MM:SS.mmm
        mn, s, ms = int(m.group(5)), int(m.group(6)), int(m.group(7))
        return mn * 60 + s + ms / 1000


def _seconds_to_hms(total: float) -> str:
    """Format seconds as H:MM:SS (no milliseconds)."""
    total = int(total)
    h = total // 3600
    mn = (total % 3600) // 60
    s = total % 60
    return f"{h}:{mn:02d}:{s:02d}"


def _clean_text(raw: str) -> str:
    """Strip HTML/VTT tags and normalize whitespace."""
    text = _VOICE_TAG_RE.sub("", raw)
    text = _HTML_TAG_RE.sub("", text)
    return " ".join(text.split())


@dataclass
class Cue:
    start: float        # seconds
    end: float          # seconds
    speaker: str        # "" if unknown
    text: str


def _parse_vtt(content: str) -> list[Cue]:
    """Parse a VTT file and return a list of Cue objects."""
    lines = content.splitlines()
    cues: list[Cue] = []
    i = 0

    # Skip WEBVTT header and any leading metadata/NOTE blocks
    while i < len(lines) and not _CUE_ARROW_RE.search(lines[i]):
        i += 1

    while i < len(lines):
        line = lines[i].strip()

        # Skip blank lines and NOTE blocks
        if not line:
            i += 1
            continue
        if line.startswith("NOTE"):
            i += 1
            while i < len(lines) and lines[i].strip():
                i += 1
            continue
        # Skip cue identifier lines (no "-->" but not blank)
        if not _CUE_ARROW_RE.search(line):
            i += 1
            continue

        # Parse timing line: "HH:MM:SS.mmm --> HH:MM:SS.mmm [settings]"
        parts = line.split("-->")
        if len(parts) < 2:
            i += 1
            continue
        start_m = _TIMESTAMP_RE.search(parts[0])
        end_m   = _TIMESTAMP_RE.search(parts[1])
        if not start_m or not end_m:
            i += 1
            continue
        start = _ts_to_seconds(start_m)
        end   = _ts_to_seconds(end_m)
        i += 1

        # Collect payload lines until next blank line
        payload_lines: list[str] = []
        while i < len(lines) and lines[i].strip():
            payload_lines.append(lines[i])
            i += 1

        if not payload_lines:
            continue

        raw_text = " ".join(payload_lines)

        # Detect speaker from voice tag first (most reliable)
        voice_m = _VOICE_TAG_RE.search(raw_text)
        if voice_m:
            speaker = voice_m.group(1).strip()
            text = _clean_text(raw_text)
        else:
            # Try colon-prefix heuristic on the first payload line
            colon_m = _COLON_SPEAKER_RE.match(_clean_text(raw_text))
            if colon_m:  # regex already limits to ≤4 words
                speaker = colon_m.group(1).strip()
                text = colon_m.group(2).strip()
            else:
                speaker = ""
                text = _clean_text(raw_text)

        if text:
            cues.append(Cue(start=start, end=end, speaker=speaker, text=text))

    return cues


# ---------------------------------------------------------------------------
# Cue merging
# ---------------------------------------------------------------------------

@dataclass
class Block:
    start: float
    end: float
    speaker: str
    cues: list["Cue"] = field(default_factory=list)


def _merge_cues(cues: list[Cue], gap_seconds: float = 120.0) -> list[Block]:
    """Merge consecutive cues from the same speaker within gap_seconds into blocks."""
    if not cues:
        return []
    blocks: list[Block] = []
    cur = Block(start=cues[0].start, end=cues[0].end,
                speaker=cues[0].speaker, cues=[cues[0]])
    for cue in cues[1:]:
        same_speaker = cue.speaker == cur.speaker
        small_gap = (cue.start - cur.end) <= gap_seconds
        if same_speaker and small_gap:
            cur.cues.append(cue)
            cur.end = cue.end
        else:
            blocks.append(cur)
            cur = Block(start=cue.start, end=cue.end,
                        speaker=cue.speaker, cues=[cue])
    blocks.append(cur)
    return blocks


def _cues_to_paragraphs(cues: list[Cue], max_merge_len: int = 500) -> list[str]:
    """Merge cues into paragraphs using sentence-continuity and proximity rules.

    max_merge_len=0 disables merging (each cue becomes its own paragraph).
    """
    if not cues:
        return []
    if max_merge_len == 0:
        return [cue.text for cue in cues]
    paragraphs: list[str] = []
    current = cues[0].text
    current_end = cues[0].end
    for cue in cues[1:]:
        gap = cue.start - current_end
        merged = current + " " + cue.text
        if not current.endswith("."):
            current = merged
        elif gap < 3.0 and len(merged) <= max_merge_len:
            current = merged
        else:
            paragraphs.append(current)
            current = cue.text
        current_end = cue.end
    paragraphs.append(current)
    return paragraphs


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def _blocks_to_markdown(
    blocks: list[Block],
    has_speakers: bool,
    include_timestamps: bool,
    gap_threshold: float = 120.0,
    max_merge_len: int = 500,
) -> str:
    parts: list[str] = []
    prev_end: float | None = None
    for block in blocks:
        body = "\n\n".join(_cues_to_paragraphs(block.cues, max_merge_len))
        # Show timestamp only when a gap >= threshold has elapsed since the previous block
        show_ts = include_timestamps and (
            prev_end is not None and (block.start - prev_end) >= gap_threshold
        )
        ts_line = f"_{_seconds_to_hms(block.start)}_\n" if show_ts else ""
        if has_speakers and block.speaker:
            parts.append(f"{ts_line}**{block.speaker}**\n{body}")
        else:
            parts.append(f"{ts_line}{body}")
        prev_end = block.end
    # Two blank lines before a timestamped section; one blank line otherwise
    result: list[str] = []
    for i, part in enumerate(parts):
        if i == 0:
            result.append(part)
        else:
            sep = "\n\n\n" if part.startswith("_") else "\n\n"
            result.append(sep + part)
    return "".join(result)


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def yaml_str(value: str) -> str:
    """Emit a double-quoted YAML scalar, escaping internal double-quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def yaml_list(items: list[str]) -> str:
    """Emit a YAML block sequence."""
    if not items:
        return "[]"
    return "\n" + "\n".join(f"  - {yaml_str(item)}" for item in items)


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

_FILENAME_CHAR_MAP = str.maketrans({
    "‘": "'", "’": "'", "‚": "'",
    "“": '"', "”": '"', "„": '"',
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...",
    "•": "-", "·": ".", "‣": "-",
    " ": " ", " ": " ", " ": " ", "​": "",
    "×": "x", "÷": "-", "⁄": "-",
})

_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def sanitize_filename(name: str) -> str:
    return name.translate(_FILENAME_CHAR_MAP)


def has_date_prefix(name: str) -> bool:
    return bool(_DATE_PREFIX_RE.match(name))


def safe_rename(src: Path, dst: Path, dry_run: bool) -> Path:
    if src == dst:
        return src
    if dst.exists():
        warn(f"rename target already exists, keeping original name: {dst.name!r}")
        return src
    if dry_run:
        info(f"would rename {src.name!r} → {dst.name!r}")
        return dst
    src.rename(dst)
    info(f"renamed {src.name!r} → {dst.name!r}")
    return dst


# ---------------------------------------------------------------------------
# Date extraction
# ---------------------------------------------------------------------------

def get_file_date(path: Path) -> datetime:
    """Return the file creation time (birthtime on macOS/Windows; mtime fallback on Linux)."""
    stat = path.stat()
    ts = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc)


def parse_date_arg(value: str) -> datetime | None:
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def convert(
    vtt_path: Path,
    *,
    rename: bool,
    dry_run: bool,
    output_dir: Path | None = None,
    title_override: str | None = None,
    date_override: datetime | None = None,
    merge_gap: float = 120.0,
    include_timestamps: bool = True,
    max_merge_len: int = 500,
) -> bool:
    """Convert one .vtt → .md.  Returns True on success."""
    if not vtt_path.exists():
        error(f"file not found: {vtt_path}")
        return False
    if not vtt_path.is_file():
        error(f"not a regular file: {vtt_path}")
        return False
    if vtt_path.suffix.lower() != ".vtt":
        error(f"not a .vtt file: {vtt_path.name!r}")
        return False

    # --- read ---
    try:
        content = vtt_path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        error(f"could not read {vtt_path.name!r}: {exc}")
        return False

    if not content.strip().startswith("WEBVTT"):
        warn(f"{vtt_path.name!r} does not start with 'WEBVTT'; attempting to parse anyway")

    # --- date ---
    if date_override:
        dt = date_override
        date_source = "command-line --date"
    else:
        # Try to extract YYYY-MM-DD from the filename itself
        stem_date_m = _DATE_PREFIX_RE.match(vtt_path.stem)
        if stem_date_m:
            try:
                dt = datetime.strptime(stem_date_m.group(0), "%Y-%m-%d").replace(tzinfo=timezone.utc)
                date_source = "filename date prefix"
            except ValueError:
                dt = get_file_date(vtt_path)
                date_source = "file birthtime (fallback)"
        else:
            dt = get_file_date(vtt_path)
            date_source = "file birthtime (fallback)"

    date_str = dt.strftime("%Y-%m-%d")
    if "fallback" in date_source:
        info(f"date sourced from {date_source}: {date_str}; use --date to override")

    # --- optional rename ---
    if rename and not has_date_prefix(vtt_path.name):
        new_name = f"{date_str} {sanitize_filename(vtt_path.stem)}.vtt"
        vtt_path = safe_rename(vtt_path, vtt_path.parent / new_name, dry_run)

    md_name = sanitize_filename(vtt_path.stem) + ".md"
    md_dir = output_dir if output_dir is not None else vtt_path.parent
    md_path = md_dir / md_name

    # --- parse VTT ---
    try:
        cues = _parse_vtt(content)
    except Exception as exc:
        error(f"could not parse VTT {vtt_path.name!r}: {exc}")
        return False

    if not cues:
        warn(f"no cues found in {vtt_path.name!r}; the .md will have no transcript body")

    # --- merge cues ---
    blocks = _merge_cues(cues, gap_seconds=merge_gap)

    # --- metadata ---
    duration_s = cues[-1].end if cues else 0.0
    duration_str = _seconds_to_hms(duration_s) if cues else "0:00:00"

    all_speakers = []
    seen: set[str] = set()
    for b in blocks:
        if b.speaker and b.speaker not in seen:
            seen.add(b.speaker)
            all_speakers.append(b.speaker)

    has_speakers = bool(all_speakers)
    if not has_speakers:
        warn("no speaker labels detected; transcript will have plain text only" if not include_timestamps
             else "no speaker labels detected; transcript will have timestamps only")

    title = title_override or sanitize_filename(vtt_path.stem)

    # --- assemble frontmatter ---
    fm_lines = [
        "---",
        "type: transcript",
        f"title: {yaml_str(title)}",
        f"date: {date_str}",
        f"source: {yaml_str(vtt_path.name)}",
        f"duration: {yaml_str(duration_str)}",
    ]
    fm_lines.append(f"speakers:{yaml_list(all_speakers)}")
    fm_lines.append("---")

    # --- assemble body ---
    body = _blocks_to_markdown(blocks, has_speakers, include_timestamps, gap_threshold=merge_gap, max_merge_len=max_merge_len)
    content_out = "\n".join(fm_lines) + f"\n\n# {title}\n\n" + body + "\n"

    # --- write ---
    if dry_run:
        info(
            f"[dry-run] would write {md_path.name!r} "
            f"({len(content_out)} bytes, {len(blocks)} block(s), "
            f"{len(all_speakers)} speaker(s), duration {duration_str})"
        )
    else:
        try:
            md_path.write_text(content_out, encoding="utf-8")
        except Exception as exc:
            error(f"could not write {md_path}: {exc}")
            return False
        ok(
            f"wrote {md_path.name!r} "
            f"({len(content_out)} bytes, {len(blocks)} block(s), "
            f"{len(all_speakers)} speaker(s), duration {duration_str})"
        )

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert-vtt-to-md.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Convert .vtt transcript files to Markdown (.md) for wiki ingestion.

            For each .vtt the script:
              1. Parses WebVTT cues, extracting speaker names from voice tags
                 (<v Speaker>) or colon-prefix convention (Speaker: text).
              2. Merges consecutive same-speaker cues into readable paragraphs.
              3. Optionally renames the .vtt to "YYYY-MM-DD <original>.vtt"
                 (date from filename prefix, --date flag, or file mtime).
              4. Writes a .md with YAML frontmatter (type, title, date, source,
                 duration, speakers) and a bold-speaker transcript body.

            Output lines are prefixed with [OK], [WARN], [ERROR], or [INFO].
        """),
        epilog=textwrap.dedent("""\
            FRONTMATTER FIELDS WRITTEN
              type      always "transcript"
              title     --title value, or the .vtt filename stem
              date      YYYY-MM-DD HH:mm:ss (from filename prefix, --date, or file birthtime)
              source    original .vtt filename (after optional rename)
              duration  H:MM:SS total duration
              speakers  list of unique speakers (empty list when none detected)

            SPEAKER DETECTION
              • <v Speaker Name>...</v>  WebVTT voice tags (most reliable)
              • "Speaker Name: text"     Colon-prefix convention (first ≤4 words)

            EXAMPLES
              # Convert single file with a human-readable title (skipped if .md exists)
              python3 convert-vtt-to-md.py "Meeting.vtt" --title "Q2 Planning"

              # Batch convert, output to wiki (already-converted files skipped by default)
              python3 convert-vtt-to-md.py --input-dir raw/transcripts \\
                      --output-dir raw/transcripts/converted

              # Force reconversion of all files even if .md already exists
              python3 convert-vtt-to-md.py --input-dir raw/transcripts --force

              # Preview without writing
              python3 convert-vtt-to-md.py --input-dir raw/transcripts --dry-run

            EXIT CODES
              0  all conversions succeeded (or nothing to do)
              1  one or more files failed or bad arguments
        """),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE.vtt",
        help="one or more .vtt files to convert",
    )
    parser.add_argument(
        "--input-dir",
        metavar="DIR",
        help="convert all *.vtt files in this directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="reconvert even if a .md counterpart already exists (default: skip already-converted files)",
    )

    parser.add_argument(
        "--no-rename",
        action="store_true",
        help="do not prefix .vtt filenames with the date",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="write .md files here instead of alongside the .vtt files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be done without writing or renaming anything",
    )
    parser.add_argument(
        "--title",
        metavar="TITLE",
        help="human-readable title for the transcript (H1 heading and frontmatter)",
    )
    parser.add_argument(
        "--date",
        metavar="YYYY-MM-DD",
        help="override the transcript date (default: filename prefix or file mtime)",
    )
    parser.add_argument(
        "--merge-gap",
        metavar="SECONDS",
        type=float,
        default=120.0,
        help="max gap in seconds to merge consecutive same-speaker cues (default: 120)",
    )
    parser.add_argument(
        "--max-merge-len",
        metavar="CHARS",
        type=int,
        default=500,
        help="max paragraph length when merging sentences (0 = no merging, default: 500)",
    )
    parser.add_argument(
        "--no-timestamps",
        dest="timestamps",
        action="store_false",
        default=True,
        help="omit inline timestamps (H:MM:SS) from the transcript body (default: included)",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # --- parse --date ---
    date_override: datetime | None = None
    if args.date:
        date_override = parse_date_arg(args.date)
        if not date_override:
            print(f"[ERROR] invalid --date value: {args.date!r} (expected YYYY-MM-DD)", file=sys.stderr)
            sys.exit(1)

    # --- validate --merge-gap ---
    if args.merge_gap < 0:
        print(f"[ERROR] --merge-gap must be >= 0, got {args.merge_gap}", file=sys.stderr)
        sys.exit(1)
    if args.max_merge_len < 0:
        print(f"[ERROR] --max-merge-len must be >= 0, got {args.max_merge_len}", file=sys.stderr)
        sys.exit(1)

    # --- resolve output directory ---
    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        if not args.dry_run:
            try:
                output_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                print(f"[ERROR] cannot create output-dir {args.output_dir!r}: {exc}", file=sys.stderr)
                sys.exit(1)

    # --- collect paths ---
    paths: list[Path] = []

    if args.input_dir and args.files:
        warn("both --dir and explicit files given; using --dir and ignoring explicit files")

    if args.input_dir:
        d = Path(args.input_dir)
        if not d.exists():
            print(f"[ERROR] directory not found: {args.input_dir}", file=sys.stderr)
            sys.exit(1)
        if not d.is_dir():
            print(f"[ERROR] not a directory: {args.input_dir}", file=sys.stderr)
            sys.exit(1)
        paths = sorted(d.glob("*.vtt"))
        if not paths:
            print(f"[INFO] no .vtt files found in {args.input_dir}")
            sys.exit(0)
    elif args.files:
        paths = [Path(f) for f in args.files]
    else:
        parser.print_help()
        sys.exit(1)

    # --- skip already-converted files (unless --force) ---
    if not args.force:
        before = len(paths)

        def _md_exists(p: Path) -> bool:
            check_dir = output_dir if output_dir is not None else p.parent
            stem = sanitize_filename(p.stem)
            if (check_dir / (stem + ".md")).exists():
                return True
            # Also check the date-prefixed name that would result from rename
            if not args.no_rename and not has_date_prefix(p.name):
                dt = get_file_date(p)
                date_str = dt.strftime("%Y-%m-%d")
                prefixed_stem = f"{date_str} {stem}"
                if (check_dir / (prefixed_stem + ".md")).exists():
                    return True
            return False

        paths = [p for p in paths if not _md_exists(p)]
        skipped = before - len(paths)
        if skipped:
            print(f"[INFO] skipping {skipped} already-converted file(s) (use --force to reconvert)")

    if not paths:
        print("[INFO] nothing to convert")
        sys.exit(0)

    # --- convert ---
    n_ok = n_fail = 0
    for p in paths:
        print(f"converting {p.name!r} …")
        success = convert(
            p,
            rename=not args.no_rename,
            dry_run=args.dry_run,
            output_dir=output_dir,
            title_override=args.title,
            date_override=date_override,
            merge_gap=args.merge_gap,
            include_timestamps=args.timestamps,
            max_merge_len=args.max_merge_len,
        )
        if success:
            n_ok += 1
        else:
            n_fail += 1

    # --- summary ---
    label = "[dry-run] " if args.dry_run else ""
    total = n_ok + n_fail
    print(
        f"\n[INFO] {label}done: {n_ok}/{total} converted"
        + (f", {n_fail} failed" if n_fail else "")
        + (f", {len(_WARNINGS)} warning(s)" if _WARNINGS else "")
    )

    if n_fail:
        sys.exit(1)


if __name__ == "__main__":
    main()
