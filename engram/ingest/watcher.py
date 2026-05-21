"""
engram.ingest.watcher — Continuous folder watcher
==================================================

Polls an inbox directory for new documents and triggers per-file ingestion
via the Claude CLI. Runs as a long-lived daemon or single shot.

Supported file types: .md, .txt, .eml, .vtt, .pdf (read as text)

The watcher is the open-source replacement for the AcmeCorp-specific
sync_and_ingest.sh launchd script. It works on any OS with Python 3.11+,
requires no launchd / cron / systemd setup, and can be embedded as a
background thread in the dashboard server.

Usage (standalone):

    python3 -m engram.ingest.watcher \\
        --inbox  /path/to/inbox \\
        --memory /path/to/memory-store \\
        --wiki   /path/to/knowledge-base \\
        --interval 60

Configuration via engram_config.yaml:

    ingest:
      enabled:          true
      inbox_path:       /path/to/inbox        # overrides paths.inbox_src
      interval_seconds: 60
      extensions:       [.md, .txt, .eml, .vtt, .pdf]
      model:            claude-haiku-4-5
      on_new_file:      compile               # compile | wiki | both
      redaction:
        enabled: false
        ...  (see engram/ingest/redactor.py)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional


# ─── Seen-file registry ───────────────────────────────────────────────────────

class SeenRegistry:
    """
    Persists a set of already-processed file paths/hashes across restarts.
    Stored in {memory_path}/.watcher_seen.json
    """

    def __init__(self, registry_path: Path):
        self._path = registry_path
        self._seen: dict[str, str] = {}   # {relative_path: content_hash}
        self._load()

    def _load(self):
        try:
            if self._path.exists():
                self._seen = json.loads(self._path.read_text())
        except Exception:
            self._seen = {}

    def _save(self):
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._seen, indent=2))
        except Exception:
            pass

    def is_new(self, path: Path, content: str) -> bool:
        """True if this path+content has not been seen before."""
        key  = str(path)
        hash_ = hashlib.sha1(content.encode(errors="ignore")).hexdigest()[:16]
        return self._seen.get(key) != hash_

    def mark_done(self, path: Path, content: str):
        key   = str(path)
        hash_ = hashlib.sha1(content.encode(errors="ignore")).hexdigest()[:16]
        self._seen[key] = hash_
        self._save()


# ─── Watcher ──────────────────────────────────────────────────────────────────

class InboxWatcher:
    """
    Polls `inbox_path` for new/changed files and triggers ingestion.

    For each new file:
      1. Read and optionally redact content
      2. Call `on_file(path, content)` — defaults to Claude CLI ingestion

    Thread-safe for single-watcher use. Not designed for concurrent watchers
    on the same inbox directory.
    """

    SUPPORTED_EXTENSIONS = {".md", ".txt", ".eml", ".vtt", ".pdf"}

    def __init__(
        self,
        inbox_path: Path,
        memory_path: Path,
        *,
        claude_bin:       Optional[str] = None,
        model:            str            = "claude-haiku-4-5",
        interval_seconds: int            = 60,
        extensions:       Optional[set]  = None,
        on_new_file=None,
        redactor=None,
        log_path:         Optional[Path] = None,
    ):
        self.inbox_path       = Path(inbox_path)
        self.memory_path      = Path(memory_path)
        self.claude_bin       = claude_bin or shutil.which("claude") or "claude"
        self.model            = model
        self.interval_seconds = interval_seconds
        self.extensions       = extensions or self.SUPPORTED_EXTENSIONS
        self.redactor         = redactor
        self.log_path         = log_path

        self._registry = SeenRegistry(self.memory_path / ".watcher_seen.json")
        self._running  = False

        # Custom handler: fn(path: Path, content: str) -> bool (True = success)
        self._on_new_file = on_new_file or self._default_ingest

    # ── Logging ───────────────────────────────────────────────────────────────

    def _log(self, msg: str):
        ts  = time.strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        if self.log_path:
            try:
                with open(self.log_path, "a") as f:
                    f.write(line + "\n")
            except Exception:
                pass

    # ── Default ingestion: Claude CLI ─────────────────────────────────────────

    def _default_ingest(self, path: Path, content: str) -> bool:
        """
        Ingest a single file via the Claude CLI.
        Calls: claude -p "Compile this document into memory: {path}" --model {model}
        """
        prompt = (
            f"You are an engram memory compiler. "
            f"Read the following document and extract key facts, decisions, "
            f"people, dates, and open questions into the memory store.\n\n"
            f"Document path: {path}\n\n"
            f"Content:\n{content[:8000]}"
        )
        try:
            result = subprocess.run(
                [self.claude_bin, "-p", prompt, "--model", self.model],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                self._log(f"  ✓ ingested: {path.name}")
                return True
            else:
                self._log(f"  ✗ CLI error ({result.returncode}): {result.stderr[:200]}")
                return False
        except subprocess.TimeoutExpired:
            self._log(f"  ✗ timeout ingesting: {path.name}")
            return False
        except Exception as e:
            self._log(f"  ✗ error ingesting {path.name}: {e}")
            return False

    # ── File reading ──────────────────────────────────────────────────────────

    def _read_file(self, path: Path) -> str:
        """Return the text content of a file. Dispatches by extension:

          - text-shaped (.md, .txt, .eml, .vtt, .html, .htm) → raw read
          - .pdf  → pypdf
          - .docx, .pptx, .xlsx → engram.ingest.extractors
          - images (.png, .jpg, .jpeg, .tiff, .gif, .bmp, .webp) → Anthropic
            vision via the SDK (requires ANTHROPIC_API_KEY; otherwise returns
            "" and the watcher silently treats the file as a placeholder).

        Returns "" for unrecognised extensions or extractor failures — the
        caller's <200 char check skips placeholders on the writeback side.
        """
        ext = path.suffix.lower()
        # Plain text — original fast path
        if ext in (".md", ".txt", ".eml", ".vtt", ".html", ".htm", ""):
            try:
                return path.read_text(errors="ignore")
            except Exception as e:
                self._log(f"  ⚠ read error for {path.name}: {e}")
                return ""
        if ext == ".pdf":
            try:
                import pypdf
                reader = pypdf.PdfReader(str(path))
                pages = [page.extract_text() or "" for page in reader.pages]
                text = "\n\n".join(pages).strip()
                if not text:
                    self._log(f"  ⚠ no text extracted from PDF (scanned image?): {path.name}")
                return text
            except Exception as e:
                self._log(f"  ⚠ PDF read error for {path.name}: {e}")
                return ""
        # Office docs + images — single dispatch through the extractors module.
        try:
            from engram.ingest.extractors import extract as _extract
            text = _extract(path)
            if not text:
                self._log(f"  ⚠ no text extracted from {ext} file: {path.name}")
            return text
        except Exception as e:
            self._log(f"  ⚠ extractor error for {path.name}: {e}")
            return ""

    # ── Scan ─────────────────────────────────────────────────────────────────

    def _scan_once(self) -> int:
        """Scan inbox, process new files. Returns count of new files found.

        Files that ingest successfully are moved to ``inbox/_processed/`` so
        the live folder stays clean and only contains pending work. Files
        already under ``_processed/`` are skipped on subsequent scans.
        """
        if not self.inbox_path.exists():
            return 0

        new_count = 0
        for p in sorted(self.inbox_path.rglob("*")):
            if not p.is_file():
                continue
            if p.suffix.lower() not in self.extensions:
                continue
            if p.name.startswith("."):
                continue
            # Don't re-process archived files
            try:
                if "_processed" in p.relative_to(self.inbox_path).parts:
                    continue
            except ValueError:
                pass

            try:
                content = self._read_file(p)
            except Exception:
                continue

            if not self._registry.is_new(p, content):
                # Already-processed file is still sitting in the live inbox —
                # likely processed before the auto-archive feature landed.
                # Catch it up by moving to _processed/ now. Idempotent: the
                # archive step's own check skips files already under there.
                self._archive_processed(p)
                continue

            # Apply redaction if configured
            if self.redactor and self.redactor.is_active:
                content, n_redacted = self.redactor.redact(content, source=p.name)
                if n_redacted:
                    # Write redacted version back to file so downstream tools see clean text
                    try:
                        p.write_text(content)
                    except Exception:
                        pass

            self._log(f"New file: {p.relative_to(self.inbox_path)}")
            new_count += 1

            try:
                success = self._on_new_file(p, content)
            except Exception as e:
                self._log(f"  ✗ handler error: {e}")
                success = False

            if success:
                self._registry.mark_done(p, content)
                self._archive_processed(p)

        return new_count

    def _archive_processed(self, src: Path) -> None:
        """Move a successfully-ingested file to ``inbox/_processed/`` so the
        live folder only shows pending work. Preserves the relative directory
        structure under _processed/ and disambiguates name collisions with a
        timestamp suffix.
        """
        try:
            rel = src.relative_to(self.inbox_path)
        except ValueError:
            return  # not under inbox — nothing to archive
        if rel.parts and rel.parts[0] == "_processed":
            return  # already there
        dest = self.inbox_path / "_processed" / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}_{int(time.time())}{dest.suffix}")
            src.rename(dest)
            self._log(f"  → archived to _processed/{rel}")
        except Exception as e:
            self._log(f"  ⚠ archive failed for {src.name}: {e}")

    # ── Run ───────────────────────────────────────────────────────────────────

    def run_once(self) -> int:
        """Scan inbox once. Returns count of new files processed."""
        return self._scan_once()

    def run(self):
        """
        Run the watcher loop until interrupted (Ctrl-C or self.stop()).

        Scans every `interval_seconds`. Logs each cycle.
        """
        self._running = True
        self._log(f"Watcher started — inbox: {self.inbox_path} (every {self.interval_seconds}s)")

        try:
            while self._running:
                n = self._scan_once()
                if n:
                    self._log(f"Cycle done — {n} new file(s) ingested")
                time.sleep(self.interval_seconds)
        except KeyboardInterrupt:
            self._log("Watcher stopped (KeyboardInterrupt)")
        finally:
            self._running = False

    def stop(self):
        self._running = False


# ─── Factory from config ──────────────────────────────────────────────────────

def watcher_from_config(cfg) -> Optional[InboxWatcher]:
    """
    Build an InboxWatcher from an EngramConfig object.
    Returns None if ingest is disabled or inbox path is not set.
    """
    ingest_cfg = getattr(cfg, "ingest", None)
    if not ingest_cfg:
        return None
    if not getattr(ingest_cfg, "enabled", False):
        return None

    inbox_path = (
        getattr(ingest_cfg, "inbox_path", None)
        or getattr(cfg.paths if hasattr(cfg, "paths") else cfg, "inbox_src", None)
    )
    if not inbox_path:
        return None

    # Redactor
    redactor = None
    redact_cfg = getattr(ingest_cfg, "redaction", None)
    if redact_cfg:
        from .redactor import Redactor, redactor_config_from_dict
        r_cfg = redact_cfg if isinstance(redact_cfg, dict) else {}
        redactor = Redactor(redactor_config_from_dict(r_cfg))

    return InboxWatcher(
        inbox_path       = Path(inbox_path),
        memory_path      = cfg.memory_path,
        claude_bin       = getattr(cfg.paths if hasattr(cfg, "paths") else cfg, "claude_bin", None),
        model            = getattr(ingest_cfg, "model", "claude-haiku-4-5"),
        interval_seconds = getattr(ingest_cfg, "interval_seconds", 60),
        extensions       = set(getattr(ingest_cfg, "extensions", [".md", ".txt", ".eml", ".vtt", ".pdf"])),
        redactor         = redactor,
        log_path         = cfg.memory_path / "logs" / "watcher.log" if hasattr(cfg, "memory_path") else None,
    )


# ─── CLI entry point ──────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="engram inbox watcher")
    parser.add_argument("--inbox",    required=True, help="Inbox directory to watch")
    parser.add_argument("--memory",   required=True, help="Memory store root path")
    parser.add_argument("--interval", type=int, default=60, help="Poll interval in seconds")
    parser.add_argument("--model",    default="claude-haiku-4-5", help="Claude model for ingestion")
    parser.add_argument("--once",     action="store_true", help="Scan once then exit")
    args = parser.parse_args()

    watcher = InboxWatcher(
        inbox_path       = Path(args.inbox),
        memory_path      = Path(args.memory),
        model            = args.model,
        interval_seconds = args.interval,
    )

    if args.once:
        n = watcher.run_once()
        print(f"Done — {n} new file(s) processed")
    else:
        watcher.run()


if __name__ == "__main__":
    main()
