"""clean_junk — find and trash junk files safely.

Spec highlights
---------------
* ``directory=None`` -> scan OS-appropriate junk paths (no user folder touched).
* Junk = ``*.tmp``, ``*.log`` older than 7 days, ``Thumbs.db``, ``.DS_Store``,
  ``desktop.ini``, and empty folders.
* ``dry_run=True``  -> compute total size, return a preview, touch nothing.
* ``dry_run=False`` -> use ``send2trash`` only. Never ``os.remove`` or
  ``shutil.rmtree``.
* Always log each item via the shared logger.
"""
from __future__ import annotations

import json
import os
import platform
import time
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field
from send2trash import send2trash

from config import PROJECT_ROOT
from core.logger import log_action

LOG_AGE_DAYS = 7
LOG_AGE_SECONDS = LOG_AGE_DAYS * 86400

# OS junk locations. Only these paths are scanned when no directory is given.
def _system_junk_paths() -> list[Path]:
    """Return junk-root paths appropriate for the host OS. Detected at runtime."""
    sysname = platform.system()
    home = Path.home()
    paths: list[Path] = []

    if sysname == "Windows":
        temp = os.environ.get("TEMP") or os.environ.get("TMP")
        local_temp = os.environ.get("LOCALAPPDATA")
        if temp:
            paths.append(Path(temp))
        if local_temp:
            paths.append(Path(local_temp) / "Temp")
        # Common browser cache folders.
        for browser in ("Google/Chrome/User/Data/Default/Cache",
                         "Mozilla/Firefox/Profiles",
                         "Microsoft/Edge/User/Data/Default/Cache"):
            candidate = local_temp and (Path(local_temp).parent / browser)
            if candidate and candidate.exists():
                paths.append(candidate)
    elif sysname == "Darwin":  # macOS
        paths.append(home / "Library" / "Caches")
        # /private/var/folders is symlinked from /var/folders on most Macs.
        paths.append(Path("/private/var/folders"))
    else:  # Linux / other Unix
        paths.append(Path("/tmp"))
        paths.append(home / ".cache")

    # Deduplicate and only return existing paths.
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        try:
            rp = p.expanduser().resolve()
        except OSError:
            continue
        if rp in seen or not rp.exists():
            continue
        seen.add(rp)
        out.append(rp)
    return out


# Junk file matchers.
KNOWN_JUNK_NAMES = {"Thumbs.db", ".DS_Store", "desktop.ini"}
JUNK_EXTENSIONS = (".tmp",)


def _is_junk_file(path: Path) -> bool:
    """True if `path` matches the junk file rules."""
    if path.name in KNOWN_JUNK_NAMES:
        return True
    if path.suffix.lower() in JUNK_EXTENSIONS:
        return True
    if path.suffix.lower() == ".log":
        try:
            return (time.time() - path.stat().st_mtime) > LOG_AGE_SECONDS
        except OSError:
            return False
    return False


def _is_empty_dir(path: Path) -> bool:
    """True if `path` is a directory with no children (recursively empty)."""
    if not path.is_dir():
        return False
    try:
        next(path.iterdir())
        return False
    except StopIteration:
        return True
    except OSError:
        return False


def _safe_size(path: Path) -> int:
    """Best-effort size in bytes; returns 0 for files we can't stat."""
    try:
        if path.is_file():
            return path.stat().st_size
        # Sum the size of everything inside a directory.
        return sum((p.stat().st_size for p in path.rglob("*") if p.is_file()), 0)
    except OSError:
        return 0


def _scan_roots(roots: list[Path]) -> tuple[list[Path], list[Path]]:
    """Walk each root, return (junk_files, empty_dirs) found anywhere inside."""
    files: list[Path] = []
    dirs: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            for dirpath, dirnames, filenames in os.walk(root):
                d = Path(dirpath)
                for fname in filenames:
                    p = d / fname
                    if _is_junk_file(p):
                        files.append(p)
                for sub in dirnames:
                    sub_p = d / sub
                    if _is_empty_dir(sub_p):
                        dirs.append(sub_p)
        except (PermissionError, OSError):
            continue
    return files, dirs


def clean_junk(directory: Optional[str] = None, dry_run: bool = True) -> dict:
    """Public entry point. Returns a dict per the spec."""
    if directory is None:
        roots = _system_junk_paths()
        scope = f"system junk paths ({platform.system()})"
    else:
        root = Path(directory).expanduser().resolve()
        if not root.exists() or not root.is_dir():
            return {"error": f"Folder not found: {root}",
                    "would_free_mb": 0.0, "items": [], "removed": False}
        roots = [root]
        scope = str(root)

    junk_files, empty_dirs = _scan_roots(roots)
    items: list[dict] = []
    total_bytes = 0

    for p in junk_files:
        size = _safe_size(p)
        total_bytes += size
        items.append({"type": "file", "path": str(p), "size_bytes": size})
    for p in empty_dirs:
        items.append({"type": "empty_dir", "path": str(p), "size_bytes": 0})

    if dry_run:
        # Dry run: report and return without touching anything.
        for it in items:
            log_action("clean_junk[dry_run]", {"path": it["path"], "type": it["type"]},
                       f"would free {_human(it['size_bytes'])}")
        return {
            "would_free_mb": round(total_bytes / (1024 * 1024), 2),
            "items": items,
            "removed": False,
            "scope": scope,
            "dry_run": True,
        }

    # Real run: send2trash every item. Never os.remove, never rmtree.
    removed = 0
    for it in items:
        try:
            send2trash(it["path"])
            removed += 1
            log_action("clean_junk", {"path": it["path"], "type": it["type"]},
                       f"trashed ({_human(it['size_bytes'])})")
        except OSError as e:
            log_action("clean_junk", {"path": it["path"], "type": it["type"]},
                       f"FAILED: {e}")
    return {
        "would_free_mb": round(total_bytes / (1024 * 1024), 2),
        "items": items,
        "removed": removed > 0,
        "scope": scope,
        "dry_run": False,
    }


def _human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


# ---------- OpenAI tool descriptor ----------

class CleanJunkArgs(BaseModel):
    directory: Optional[str] = Field(
        None,
        description=(
            "Folder to scan for junk. Omit (leave as null) to scan the OS's "
            "standard junk locations (TEMP dirs, browser caches, etc.) only. "
            "Never pass a user folder you don't intend to scan."
        ),
    )
    dry_run: bool = Field(
        True,
        description=(
            "If true, only return what would be removed and the size in MB. "
            "ALWAYS call with dry_run=true first. Show the user the result, "
            "and only call again with dry_run=false after they confirm in chat."
        ),
    )


TOOL_SPEC = {
    "name": "clean_junk",
    "description": (
        "Find and (optionally) trash junk files. Junk = *.tmp, *.log older than "
        "7 days, Thumbs.db, .DS_Store, desktop.ini, and empty folders. With "
        "directory=null, scans the OS's standard junk paths (TEMP dirs, "
        "browser caches) only. With dry_run=true (default), returns a preview "
        "with total size in MB and the file list. With dry_run=false, moves "
        "items to the OS trash (always recoverable; never permanent delete). "
        "Always call once with dry_run=true first; show the user; only re-call "
        "with dry_run=false after they explicitly confirm in chat."
    ),
    "parameters": CleanJunkArgs.model_json_schema(),
    "function": clean_junk,
}


# CLI smoke test: `python -m tools.junk_cleaner [path] [--apply]`
if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    target = None
    apply = False
    for a in args:
        if a == "--apply":
            apply = True
        else:
            target = a
    print(json.dumps(clean_junk(target, dry_run=not apply), indent=2, default=str))
