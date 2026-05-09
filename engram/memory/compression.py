"""
Compression-Based Forgetting.

Old memory objects are not archived as-is — they are compressed,
retaining the core semantics while losing peripheral detail.

Tier schedule:
  fresh    (0-30 days):  full memory, no compression
  aging    (30-90 days): compress to 60% of original
  mature   (90-365 days): compress to 25%
  ancient  (>365 days):   key-facts-only (~10%)
"""
import time
from pathlib import Path
from typing import Callable


COMPRESSION_TIERS = {
    "fresh":   (0,    30,  1.00),
    "aging":   (30,   90,  0.60),
    "mature":  (90,   365, 0.25),
    "ancient": (365,  10**6, 0.10),
}

# Files/night, prioritised highest-savings first
COMPRESSION_BUDGET_PER_NIGHT = {
    "ancient": 10,
    "mature":  20,
    "aging":   30,
    "fresh":   0,
}

COMPRESSION_PROMPT = """You are compressing a memory object that is {age} days old.

The original is {original_chars} chars. Target: ~{target_chars} chars.

Compression rules by age tier:
- aging (30-90d):  remove specifics, keep named entities + key decisions + open items
- mature (90-365d): keep only decisions made, people involved, outcomes, unresolved items
- ancient (>365d): key-facts-only (who, what, when, final outcome)

PRESERVE ABSOLUTELY:
- All named decisions with rationale
- All people with their roles
- All unresolved questions
- Numerical targets, deadlines, contract terms
- Anything explicitly flagged [crystallised]

Memory object:
{content}

Output: compressed memory object, same markdown format, no preamble.
Append at the end: `[Compressed {date}: {original_chars}c → ~{target_chars}c]`"""


def classify_age_tier(age_days: float) -> str:
    for tier, (low, high, _ratio) in COMPRESSION_TIERS.items():
        if low <= age_days < high:
            return tier
    return "ancient"


def find_compression_candidates(
    memory_dir: Path,
    *,
    skip_dirs: tuple = ("crystallised", "working", "context"),
) -> dict:
    """
    Walk MEMORY/, classify each .md by age, return candidates per tier.
    Returns {tier: [(path, age_days, size)]}
    """
    out = {"aging": [], "mature": [], "ancient": [], "fresh": []}
    if not memory_dir.exists():
        return out
    now_t = time.time()
    for f in memory_dir.rglob("*.md"):
        rel = f.relative_to(memory_dir).parts
        if rel and rel[0] in skip_dirs:
            continue
        # Skip already-compressed
        try:
            head = f.read_text(errors="ignore")[-300:]
            if "[Compressed " in head:
                continue
        except Exception:
            continue
        try:
            age_days = (now_t - f.stat().st_mtime) / 86400
        except Exception:
            continue
        tier = classify_age_tier(age_days)
        out[tier].append((f, age_days, f.stat().st_size))
    return out


def select_for_tonight(candidates: dict) -> list:
    """
    Prioritize: ancient first (largest savings), then mature, aging.
    Within tier, sort by (savings = original - target) descending.
    """
    selected = []
    for tier in ("ancient", "mature", "aging"):
        budget = COMPRESSION_BUDGET_PER_NIGHT[tier]
        ratio = COMPRESSION_TIERS[tier][2]
        items = candidates.get(tier, [])
        items_with_savings = [
            (path, age, size, size * (1 - ratio))
            for (path, age, size) in items
        ]
        items_with_savings.sort(key=lambda x: -x[3])
        for path, age, size, savings in items_with_savings[:budget]:
            selected.append({
                "path":   str(path),
                "tier":   tier,
                "age_days": round(age, 1),
                "original_chars": size,
                "target_chars": int(size * ratio),
                "estimated_savings": int(savings),
            })
    return selected


def compress_file(
    candidate: dict,
    *,
    claude_complete: Callable,
    dry_run: bool = False,
) -> dict:
    """
    Compress one file using Claude Haiku (cloud).
    Returns result dict with status, original_chars, new_chars, error.
    """
    path = Path(candidate["path"])
    if not path.exists():
        return {**candidate, "status": "skipped", "error": "file_not_found"}

    try:
        content = path.read_text()
    except Exception as e:
        return {**candidate, "status": "error", "error": str(e)}

    prompt = COMPRESSION_PROMPT.format(
        age=int(candidate["age_days"]),
        original_chars=candidate["original_chars"],
        target_chars=candidate["target_chars"],
        date=time.strftime("%Y-%m-%d"),
        content=content,
    )

    if dry_run:
        return {**candidate, "status": "dry_run"}

    try:
        compressed = claude_complete(prompt)
    except Exception as e:
        return {**candidate, "status": "error", "error": str(e)}

    if not compressed or len(compressed) < 50:
        return {**candidate, "status": "error", "error": "empty_output"}

    # Backup original
    backup_dir = path.parent / "_pre_compression_backups"
    backup_dir.mkdir(exist_ok=True)
    backup_path = backup_dir / f"{path.stem}_{int(time.time())}.md"
    try:
        backup_path.write_text(content)
    except Exception:
        pass

    path.write_text(compressed)

    return {
        **candidate,
        "status": "ok",
        "new_chars": len(compressed),
        "actual_savings": len(content) - len(compressed),
        "backup": str(backup_path.relative_to(path.parent.parent.parent)),
    }
