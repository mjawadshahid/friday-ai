"""organize_files — sort files in a folder into subfolders by type or date.

This is the LLM-callable tool exposed as ``organize_files``.

Behavior summary
----------------
* Resolves ``directory`` to an absolute path. If the folder is missing,
  returns an error dict instead of crashing.
* ``mode="type"``  -> bucket by extension into fixed categories.
* ``mode="date"``  -> bucket by year-month of the file's last-modified time.
* Does NOT recurse. Only files sitting directly inside ``directory`` are
  touched. Subfolders are left alone.
* Skips files already sitting inside a subfolder whose name matches a
  category — a previous organize run is therefore idempotent.
* On name collisions, appends ``" (1)"``, ``" (2)"`` … until unique.
* Logs every move via the shared logger so the trail in
  ``logs/actions.log`` is complete.
"""
from __future__ import annotations

import json
import shutil
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from config import PROJECT_ROOT
from core.logger import log_action

# --- Category map for mode="type". Extension is lowercased before lookup. ---
CATEGORIES: dict[str, tuple[str, ...]] = {
    "Documents":  (".pdf", ".docx", ".doc", ".txt", ".xlsx", ".pptx", ".csv"),
    "Images":     (".jpg", ".jpeg", ".png", ".gif", ".svg", ".webp"),
    "Videos":     (".mp4", ".mov", ".avi", ".mkv"),
    "Audio":      (".mp3", ".wav", ".m4a"),
    "Archives":   (".zip", ".rar", ".7z", ".tar", ".gz"),
    "Installers": (".exe", ".msi", ".dmg", ".pkg", ".deb"),
    "Code":       (".py", ".js", ".ts", ".html", ".css", ".json"),
}
OTHER = "Other"  # fallback bucket name


class OrganizeFilesArgs(BaseModel):
    directory: str = Field(..., description="Absolute path to the folder to organize.")
    mode: Literal["type", "date"] = Field(
        "type",
        description="Either 'type' (bucket by extension) or 'date' (bucket by YYYY-MM).",
    )


def _category_for(ext: str) -> str:
    """Return the category name for an extension like '.pdf'."""
    e = ext.lower()
    for name, exts in CATEGORIES.items():
        if e in exts:
            return name
    return OTHER


def _is_in_category_folder(entry: Path, valid_names: set[str], root: Path) -> bool:
    """A file is 'already organized' if it sits inside a category *subfolder* of
    root. The root folder itself is never a "category folder" — otherwise
    organizing a folder literally named e.g. "Documents" would skip everything."""
    return (
        bool(valid_names)
        and entry.parent != root
        and entry.parent.name in valid_names
    )


def _unique_target(dest_dir: Path, filename: str) -> Path:
    """Return ``dest_dir/filename`` unless taken, then try ``name (1).ext`` etc."""
    p = Path(filename)
    candidate = dest_dir / p.name
    if not candidate.exists():
        return candidate
    n = 1
    while True:
        candidate = dest_dir / f"{p.stem} ({n}){p.suffix}"
        if not candidate.exists():
            return candidate
        n += 1


def organize_files(directory: str, mode: str = "type") -> dict:
    """Public entry point. Returns a plain dict (per the spec)."""
    args = OrganizeFilesArgs(directory=directory, mode=mode)
    root = Path(args.directory).expanduser()

    if not root.exists():
        return {"error": f"Folder not found: {root}", "moved": [], "skipped": [], "category_counts": {}}
    if not root.is_dir():
        return {"error": f"Not a directory: {root}", "moved": [], "skipped": [], "category_counts": {}}

    root = root.resolve()  # canonical absolute path for clean logging

    # In date mode, bucket names change monthly — don't skip anything.
    valid_names = set() if args.mode == "date" else (set(CATEGORIES.keys()) | {OTHER})

    moved: list[tuple[str, str]] = []
    skipped: list[str] = []
    category_counts: dict[str, int] = {}

    # Snapshot the listing before we start moving entries around.
    for entry in list(root.iterdir()):
        if not entry.is_file():
            continue  # never recurse; subfolders are off-limits
        if _is_in_category_folder(entry, valid_names, root):
            skipped.append(str(entry))
            continue

        # Decide destination bucket.
        if args.mode == "type":
            bucket = _category_for(entry.suffix)
        else:  # "date"
            bucket = datetime.fromtimestamp(entry.stat().st_mtime).strftime("%Y-%m")

        dest_dir = root / bucket
        dest_dir.mkdir(exist_ok=True)
        target = _unique_target(dest_dir, entry.name)

        try:
            shutil.move(str(entry), str(target))
        except OSError as e:
            skipped.append(f"{entry} (move failed: {e})")
            continue

        moved.append((str(entry), str(target)))
        category_counts[bucket] = category_counts.get(bucket, 0) + 1
        log_action(
            "organize_files",
            {"from": str(entry), "to": str(target), "mode": args.mode},
            f"moved to {bucket}",
        )

    return {"moved": moved, "skipped": skipped, "category_counts": category_counts}


# ---------- OpenAI tool descriptor ----------

TOOL_SPEC = {
    "name": "organize_files",
    "description": (
        "Sort files sitting directly inside `directory` into subfolders. "
        "Use mode='type' to bucket by file type (Documents, Images, Videos, "
        "Audio, Archives, Installers, Code, Other) or mode='date' to bucket "
        "by YYYY-MM of last modification. Does not recurse into subfolders. "
        "Returns a dict with the list of moves, anything skipped, and "
        "per-category counts. Call this when the user asks to organize, tidy, "
        "or sort files in a specific folder."
    ),
    "parameters": OrganizeFilesArgs.model_json_schema(),
    "function": organize_files,
}


# Quick CLI smoke test: `python -m tools.file_organizer <path> [type|date]`
if __name__ == "__main__":
    import sys
    target = sys.argv[1] if len(sys.argv) > 1 else str(PROJECT_ROOT / "data")
    mode = sys.argv[2] if len(sys.argv) > 2 else "type"
    print(json.dumps(organize_files(target, mode=mode), indent=2))
