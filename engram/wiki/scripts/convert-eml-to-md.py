#!/usr/bin/env python3
"""convert-eml-to-md — convert .eml files to Markdown for wiki ingestion.

Each .eml is converted to a .md file (sibling by default, or in --output-dir)
with YAML frontmatter and a plain-text / HTML-to-Markdown body.  The .eml
filename is optionally prefixed with the email date (YYYY-MM-DD) if it does
not already start with one.

EXIT CODES
  0  all files converted successfully (or nothing to do)
  1  one or more files failed or an argument error occurred

OUTPUT FORMAT (for AI tools)
  Each action is prefixed with a tag:
    [OK]    successful step
    [WARN]  non-fatal issue (e.g. missing header, fallback used)
    [ERROR] file skipped due to unrecoverable error
    [INFO]  informational (rename, summary)

EXAMPLES
  # Convert a single file (renames it with date prefix)
  python3 convert-eml-to-md.py "raw/emails/Some email.eml"

  # Convert all new .eml in a directory (skips those already converted)
  python3 convert-eml-to-md.py --input-dir raw/emails

  # Re-convert all .eml including those already converted
  python3 convert-eml-to-md.py --input-dir raw/emails --force

  # Convert all .eml without renaming any files
  python3 convert-eml-to-md.py --input-dir raw/emails --no-rename

  # Write .md files to a different directory
  python3 convert-eml-to-md.py --input-dir raw/emails --output-dir raw/emails/converted

  # Dry run — show what would happen without writing anything
  python3 convert-eml-to-md.py --input-dir raw/emails --dry-run
"""

import argparse
import email
import email.header
import email.utils
import re
import sys
import textwrap
from datetime import datetime, timezone
from email import policy
from pathlib import Path

try:
    import html2text as _html2text_mod
    _HAS_HTML2TEXT = True
except ImportError:
    _HAS_HTML2TEXT = False


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
# Date extraction
# ---------------------------------------------------------------------------

def _parse_rfc2822(value: str) -> datetime | None:
    try:
        return email.utils.parsedate_to_datetime(value.strip())
    except Exception:
        return None


def get_email_date(msg: email.message.Message, eml_path: Path) -> tuple[datetime, str]:
    """Return (datetime, source-description) for the best available date."""
    # 1. Date header
    date_hdr = msg.get("Date", "")
    if date_hdr:
        dt = _parse_rfc2822(date_hdr)
        if dt:
            return dt, "Date header"

    # 2. Received headers — each ends with "; <rfc2822-date>"
    for received in (msg.get_all("Received") or []):
        parts = received.rsplit(";", 1)
        if len(parts) == 2:
            dt = _parse_rfc2822(parts[1])
            if dt:
                return dt, "Received header"

    # 3. File creation time (birthtime on macOS/Windows; mtime fallback on Linux)
    stat = eml_path.stat()
    ts = getattr(stat, "st_birthtime", None) or stat.st_mtime
    return datetime.fromtimestamp(ts, tz=timezone.utc), "file birthtime (fallback)"


# ---------------------------------------------------------------------------
# Header decoding
# ---------------------------------------------------------------------------

def decode_header(value: str | None, field: str) -> str:
    """Decode an RFC 2047-encoded header value; return '' with a warning on failure."""
    if not value:
        return ""
    try:
        parts = []
        for chunk, charset in email.header.decode_header(value):
            if isinstance(chunk, bytes):
                parts.append(chunk.decode(charset or "utf-8", errors="replace"))
            else:
                parts.append(chunk)
        result = "".join(parts)
        # Fold multi-line whitespace into a single space
        return " ".join(result.split())
    except Exception as exc:
        warn(f"could not decode {field!r} header ({exc}); using raw value")
        raw = str(value) if not isinstance(value, str) else value
        return " ".join(raw.split())


# ---------------------------------------------------------------------------
# Body extraction
# ---------------------------------------------------------------------------

def _html_to_text(html: str) -> str:
    if not _HAS_HTML2TEXT:
        # Minimal fallback: strip tags
        text = re.sub(r"<[^>]+>", "", html)
        return re.sub(r"\n{3,}", "\n\n", text).strip()
    h = _html2text_mod.HTML2Text()
    h.ignore_links = False
    h.body_width = 0
    h.protect_links = False
    h.wrap_links = False
    return h.handle(html).strip()


def get_body(msg: email.message.Message) -> tuple[str, str]:
    """Return (body_text, source-description)."""
    plain_parts: list[str] = []
    html_parts: list[str] = []

    for part in msg.walk():
        if (part.get_content_disposition() or "").lower() == "attachment":
            continue
        ct = part.get_content_type()
        charset = part.get_content_charset() or "utf-8"
        payload = part.get_payload(decode=True)
        if not payload:
            continue
        try:
            text = payload.decode(charset, errors="replace")
        except (LookupError, UnicodeDecodeError):
            text = payload.decode("utf-8", errors="replace")

        if ct == "text/plain":
            plain_parts.append(text)
        elif ct == "text/html":
            html_parts.append(text)

    if plain_parts:
        return "\n\n".join(plain_parts).strip(), "text/plain"
    if html_parts:
        if not _HAS_HTML2TEXT:
            warn("html2text not installed; falling back to tag-stripping for HTML body")
        return _html_to_text("\n\n".join(html_parts)), "text/html (converted)"
    return "", "empty"


# ---------------------------------------------------------------------------
# YAML helpers
# ---------------------------------------------------------------------------

def yaml_str(value: str) -> str:
    """Emit a double-quoted YAML scalar, escaping internal double-quotes."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


# ---------------------------------------------------------------------------
# Filename helpers
# ---------------------------------------------------------------------------

# Map of non-ASCII characters to plain-ASCII equivalents for use in filenames.
# Accented Latin letters (é, ü, ñ, …) are intentionally absent — they are allowed.
_FILENAME_CHAR_MAP = str.maketrans({
    # Curly / smart single quotes
    "‘": "'",    # '  left single quotation mark
    "’": "'",    # '  right single quotation mark / apostrophe
    "‚": "'",    # ‚  single low-9 quotation mark
    "‹": "<",    # ‹  single left-pointing angle quotation mark
    "›": ">",    # ›  single right-pointing angle quotation mark
    "′": "'",    # ′  prime
    "‵": "'",    # ‵  reversed prime
    # Curly / smart double quotes
    "“": '"',    # "  left double quotation mark
    "”": '"',    # "  right double quotation mark
    "„": '"',    # „  double low-9 quotation mark
    "‟": '"',    # ‟  double high-reversed-9 quotation mark
    "«": '"',    # «  left-pointing double angle quotation mark
    "»": '"',    # »  right-pointing double angle quotation mark
    "″": '"',    # ″  double prime
    "‶": '"',    # ‶  reversed double prime
    # Dashes
    "–": "-",    # –  en dash
    "—": "-",    # —  em dash
    "―": "-",    # ―  horizontal bar
    "−": "-",    # −  minus sign
    # Arrows
    "←": "<-",   # ←  leftwards arrow
    "→": "->",   # →  rightwards arrow
    "↔": "<->",  # ↔  left right arrow
    "⇐": "<=",   # ⇐  leftwards double arrow
    "⇒": "=>",   # ⇒  rightwards double arrow
    "⇔": "<=>",  # ⇔  left right double arrow
    "↖": "^",    # ↖  north west arrow
    "↗": "^",    # ↗  north east arrow
    "↘": "v",    # ↘  south east arrow
    "↙": "v",    # ↙  south west arrow
    # Ellipsis
    "…": "...",  # …  horizontal ellipsis
    # Bullets / dots
    "•": "-",    # •  bullet
    "·": ".",    # ·  middle dot
    "‣": "-",    # ‣  triangular bullet
    # Spaces
    " ": " ",    # non-breaking space
    " ": " ",    # narrow no-break space
    " ": " ",    # thin space
    "​": "",     # zero-width space
    # Misc
    "×": "x",    # ×  multiplication sign
    "÷": "-",    # ÷  division sign  (/ is path separator — use -)
    "⁄": "-",    # ⁄  fraction slash (/ is path separator — use -)
})


def sanitize_filename(name: str) -> str:
    """Replace known non-ASCII filename-unfriendly chars with ASCII equivalents.

    Accented Latin characters (é, ü, ñ, …) are left unchanged.
    """
    return name.translate(_FILENAME_CHAR_MAP)


_DATE_PREFIX_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def has_date_prefix(name: str) -> bool:
    return bool(_DATE_PREFIX_RE.match(name))


def safe_rename(src: Path, dst: Path, dry_run: bool) -> Path:
    """Rename src → dst, handling collisions; return the final path."""
    if src == dst:
        return src
    if dst.exists():
        warn(f"rename target already exists, keeping original name: {dst.name!r}")
        return src
    if dry_run:
        info(f"[dry-run] would rename {src.name!r} → {dst.name!r}")
        return dst
    src.rename(dst)
    info(f"renamed {src.name!r} → {dst.name!r}")
    return dst


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def convert(eml_path: Path, *, rename: bool, dry_run: bool, output_dir: Path | None = None) -> bool:
    """Convert one .eml → .md.  Returns True on success."""
    # --- validate ---
    if not eml_path.exists():
        error(f"file not found: {eml_path}")
        return False
    if not eml_path.is_file():
        error(f"not a regular file: {eml_path}")
        return False
    if eml_path.suffix.lower() != ".eml":
        error(f"not an .eml file: {eml_path.name!r}")
        return False

    # --- parse ---
    try:
        raw = eml_path.read_bytes()
        msg = email.message_from_bytes(raw, policy=policy.compat32)
    except Exception as exc:
        error(f"could not parse {eml_path.name!r}: {exc}")
        return False

    # --- date ---
    try:
        dt, date_source = get_email_date(msg, eml_path)
    except Exception as exc:
        warn(f"date extraction failed ({exc}); using today")
        dt = datetime.now(tz=timezone.utc)
        date_source = "today (fallback after error)"
    date_str = dt.strftime("%Y-%m-%d")

    if date_source not in ("Date header", "Received header"):
        warn(f"date sourced from {date_source}: {date_str}")

    # --- birthtime for rename prefix ---
    stat = eml_path.stat()
    birthtime_ts = getattr(stat, "st_birthtime", None) or stat.st_mtime
    birthtime_str = datetime.fromtimestamp(birthtime_ts, tz=timezone.utc).strftime("%Y-%m-%d")

    # --- optional rename ---
    if rename and not has_date_prefix(eml_path.name):
        new_name = f"{birthtime_str} {sanitize_filename(eml_path.stem)}.eml"
        eml_path = safe_rename(eml_path, eml_path.parent / new_name, dry_run)

    # Always sanitize the stem so the .md path is free of non-ASCII chars even
    # when the .eml was not renamed (e.g. --no-rename or already date-prefixed).
    md_name = sanitize_filename(eml_path.stem) + ".md"
    md_dir = output_dir if output_dir is not None else eml_path.parent
    md_path = md_dir / md_name

    # --- headers ---
    from_val = decode_header(msg.get("From"), "From") or "(unknown sender)"
    to_val   = decode_header(msg.get("To"),   "To")   or "(unknown recipient)"
    cc_val   = decode_header(msg.get("CC",  msg.get("Cc")),  "CC")
    bcc_val  = decode_header(msg.get("BCC", msg.get("Bcc")), "BCC")
    subject  = decode_header(msg.get("Subject"), "Subject") or "(no subject)"

    if not msg.get("From"):
        warn("From header missing")
    if not msg.get("To"):
        warn("To header missing")
    if not msg.get("Subject"):
        warn("Subject header missing; using '(no subject)'")

    # --- body ---
    try:
        body, body_source = get_body(msg)
    except Exception as exc:
        warn(f"body extraction failed ({exc}); body will be empty")
        body, body_source = "", "error"

    if not body:
        warn(f"empty body (source: {body_source})")

    # --- assemble ---
    lines = [
        "---",
        "type: email",
        f"subject: {yaml_str(subject)}",
        f"date: {date_str}",
        f"source: {yaml_str(eml_path.name)}",
        f"from: {yaml_str(from_val)}",
        f"to: {yaml_str(to_val)}",
    ]
    if cc_val:
        lines.append(f"cc: {yaml_str(cc_val)}")
    if bcc_val:
        lines.append(f"bcc: {yaml_str(bcc_val)}")
    lines += [
        "---",
    ]

    # Human-readable header block in the markdown body
    header_lines = [
        "```",
        f"Date: {date_str}",
        f"From: {from_val}",
        f"To: {to_val}",
    ]
    if cc_val:
        header_lines.append(f"CC: {cc_val}")
    if bcc_val:
        header_lines.append(f"BCC: {bcc_val}")
    header_lines += [
        f"Subject: {subject}",
        "```",
        "",
        "---",
    ]

    content = "\n".join(lines) + "\n\n" + "\n".join(header_lines) + "\n\n" + body + "\n"

    if dry_run:
        info(f"[dry-run] would write {md_path.name!r} ({len(content)} bytes, body via {body_source})")
    else:
        try:
            md_path.write_text(content, encoding="utf-8")
        except Exception as exc:
            error(f"could not write {md_path}: {exc}")
            return False
        ok(f"wrote {md_path.name!r} ({len(content)} bytes, body via {body_source})")

    return True


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="convert-eml-to-md.py",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description=textwrap.dedent("""\
            Convert .eml email files to Markdown (.md) for wiki ingestion.

            For each .eml the script:
              1. Extracts the date from the Date header, then Received headers,
                 then file mtime (warns if falling back).
              2. Renames the .eml to "YYYY-MM-DD <original-name>.eml" unless it
                 already starts with a date or --no-rename is given.
              3. Writes a sibling .md file with YAML frontmatter and a plain-text
                 body (HTML is converted via html2text when available).

            Output lines are prefixed with [OK], [WARN], [ERROR], or [INFO] so
            AI tools can parse results without ambiguity.
        """),
        epilog=textwrap.dedent("""\
            FRONTMATTER FIELDS WRITTEN
              type     always "email"
              source   original .eml filename (after rename)
              from     From header (RFC 2047 decoded)
              to       To header
              cc       CC header (omitted when empty)
              bcc      BCC header (omitted when empty)
              subject  Subject header
              date     YYYY-MM-DD HH:mm:ss from Date / Received / mtime

            The markdown body also contains a human-readable header block
            (Date / From / To / CC / BCC / Subject) before the email body.

            EXAMPLES
              # Convert a single file, rename with date prefix
              python3 convert-eml-to-md.py "raw/emails/Meeting notes.eml"

              # Convert all new .eml in a directory (skip those already converted)
              python3 convert-eml-to-md.py --input-dir raw/emails

              # Re-convert all, including those already converted
              python3 convert-eml-to-md.py --input-dir raw/emails --force

              # Batch convert without renaming
              python3 convert-eml-to-md.py --input-dir raw/emails --no-rename

              # Write .md files to a different directory
              python3 convert-eml-to-md.py --input-dir raw/emails --output-dir raw/emails/converted

              # Preview what would happen
              python3 convert-eml-to-md.py --input-dir raw/emails --dry-run

            EXIT CODES
              0  all conversions succeeded (or nothing to do)
              1  one or more files failed or bad arguments
        """),
    )
    parser.add_argument(
        "files",
        nargs="*",
        metavar="FILE.eml",
        help="one or more .eml files to convert",
    )
    parser.add_argument(
        "--input-dir",
        metavar="DIR",
        help="convert all *.eml files in this directory",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-convert .eml files that already have a .md counterpart",
    )
    parser.add_argument(
        "--no-rename",
        action="store_true",
        help="do not prefix .eml filenames with the date",
    )
    parser.add_argument(
        "--output-dir",
        metavar="DIR",
        help="write .md files to this directory instead of alongside the .eml files",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be done without writing or renaming any files",
    )
    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # --- resolve output directory ---
    output_dir: Path | None = None
    if args.output_dir:
        output_dir = Path(args.output_dir)
        if not args.dry_run:
            output_dir.mkdir(parents=True, exist_ok=True)
        if not args.dry_run and not output_dir.is_dir():
            print(f"[ERROR] output-dir is not a directory: {args.output_dir}", file=sys.stderr)
            sys.exit(1)

    # --- collect paths ---
    paths: list[Path] = []

    if args.input_dir and args.files:
        print("[WARN] both --input-dir and explicit files given; using --input-dir and ignoring explicit files",
              file=sys.stderr)

    if args.input_dir:
        d = Path(args.input_dir)
        if not d.exists():
            print(f"[ERROR] directory not found: {args.input_dir}", file=sys.stderr)
            sys.exit(1)
        if not d.is_dir():
            print(f"[ERROR] not a directory: {args.input_dir}", file=sys.stderr)
            sys.exit(1)
        paths = sorted(d.glob("*.eml"))
        if not paths:
            print(f"[INFO] no .eml files found in {args.input_dir}")
            sys.exit(0)
    elif args.files:
        paths = [Path(f) for f in args.files]
    else:
        parser.print_help()
        sys.exit(1)

    # --- skip already-converted files unless --force ---
    if not args.force:
        before = len(paths)
        def _md_exists(p: Path) -> bool:
            md_name = sanitize_filename(p.stem) + ".md"
            check_dir = output_dir if output_dir is not None else p.parent
            return (check_dir / md_name).exists()
        paths = [p for p in paths if not _md_exists(p)]
        skipped = before - len(paths)
        if skipped:
            print(f"[INFO] skipping {skipped} file(s) that already have a .md (use --force to re-convert)")

    if not paths:
        print("[INFO] nothing to convert")
        sys.exit(0)

    # --- convert ---
    n_ok = n_fail = 0
    for p in paths:
        print(f"converting {p.name!r} …")
        success = convert(p, rename=not args.no_rename, dry_run=args.dry_run, output_dir=output_dir)
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
