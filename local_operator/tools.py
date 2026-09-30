from __future__ import annotations

import os
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import Settings
from .security import assert_allowed
from .web import web_open, web_search

TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".csv", ".json", ".jsonl", ".py", ".js", ".ts",
    ".tsx", ".jsx", ".html", ".css", ".xml", ".yaml", ".yml", ".toml", ".ini",
    ".cfg", ".log", ".sql", ".r", ".swift", ".java", ".c", ".cpp", ".h",
}


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def _record(path: Path) -> dict[str, Any]:
    stat = path.stat()
    return {
        "path": str(path),
        "name": path.name,
        "size_bytes": stat.st_size,
        "modified_at": _iso(stat.st_mtime),
        "is_dir": path.is_dir(),
    }


def list_files(settings: Settings, path: str, recursive: bool = False, extension: str | None = None,
               limit: int = 100) -> dict[str, Any]:
    root = assert_allowed(path, settings.allowed_roots)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")
    extension = (extension or "").strip()
    if extension and not extension.startswith("."):
        extension = "." + extension
    iterator = root.rglob("*") if recursive else root.iterdir()
    entries: list[dict[str, Any]] = []
    for item in iterator:
        if extension and (item.is_dir() or item.suffix.lower() != extension.lower()):
            continue
        entries.append(_record(item))
        if len(entries) >= max(1, min(int(limit), 500)):
            break
    return {"root": str(root), "count": len(entries), "entries": entries,
            "paths": [entry["path"] for entry in entries]}


def search_files(settings: Settings, path: str, name_contains: str = "", extension: str | None = None,
                 modified_within_days: float | None = None, recursive: bool = True,
                 limit: int = 100) -> dict[str, Any]:
    root = assert_allowed(path, settings.allowed_roots)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")
    ext = (extension or "").strip().lower()
    if ext and not ext.startswith("."):
        ext = "." + ext
    needle = name_contains.casefold().strip()
    cutoff = None
    if modified_within_days is not None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=float(modified_within_days))
    iterator = root.rglob("*") if recursive else root.iterdir()
    matches: list[dict[str, Any]] = []
    for item in iterator:
        if not item.is_file():
            continue
        if needle and needle not in item.name.casefold():
            continue
        if ext and item.suffix.lower() != ext:
            continue
        if cutoff:
            modified = datetime.fromtimestamp(item.stat().st_mtime, tz=timezone.utc)
            if modified < cutoff:
                continue
        matches.append(_record(item))
        if len(matches) >= max(1, min(int(limit), 500)):
            break
    return {"root": str(root), "count": len(matches), "files": matches,
            "paths": [entry["path"] for entry in matches]}


def search_text(settings: Settings, path: str, query: str, extensions: list[str] | None = None,
                recursive: bool = True, limit: int = 50) -> dict[str, Any]:
    root = assert_allowed(path, settings.allowed_roots)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")
    needle = query.casefold()
    if not needle:
        raise ValueError("query cannot be empty")
    allowed_exts = None
    if extensions:
        allowed_exts = {ext.lower() if ext.startswith(".") else "." + ext.lower() for ext in extensions}
    iterator = root.rglob("*") if recursive else root.iterdir()
    matches: list[dict[str, Any]] = []
    for item in iterator:
        if not item.is_file():
            continue
        if item.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        if allowed_exts and item.suffix.lower() not in allowed_exts:
            continue
        try:
            text = item.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        folded = text.casefold()
        pos = folded.find(needle)
        if pos < 0:
            continue
        line_no = text.count("\n", 0, pos) + 1
        start = max(0, pos - 120)
        end = min(len(text), pos + len(query) + 120)
        snippet = " ".join(text[start:end].split())
        matches.append({"path": str(item), "line": line_no, "snippet": snippet})
        if len(matches) >= max(1, min(int(limit), 200)):
            break
    return {"root": str(root), "query": query, "count": len(matches), "matches": matches,
            "paths": list(dict.fromkeys(match["path"] for match in matches))}


def largest_files(settings: Settings, path: str, limit: int = 5, recursive: bool = True) -> dict[str, Any]:
    root = assert_allowed(path, settings.allowed_roots)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Directory not found: {root}")
    iterator = root.rglob("*") if recursive else root.iterdir()
    files = [item for item in iterator if item.is_file()]
    files.sort(key=lambda p: p.stat().st_size, reverse=True)
    selected = [_record(item) for item in files[:max(1, min(int(limit), 50))]]
    return {"root": str(root), "count": len(selected), "files": selected,
            "paths": [entry["path"] for entry in selected]}


def read_text(settings: Settings, path: str, max_chars: int | None = None) -> dict[str, Any]:
    target = assert_allowed(path, settings.allowed_roots)
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(f"File not found: {target}")
    if target.suffix.lower() not in TEXT_EXTENSIONS:
        raise ValueError(f"V0.1 only reads text-like files. Unsupported extension: {target.suffix or '(none)'}")
    cap = max(256, min(int(max_chars or settings.max_read_chars), 100_000))
    text = target.read_text(encoding="utf-8", errors="replace")
    truncated = len(text) > cap
    return {"path": str(target), "content": text[:cap], "truncated": truncated, "chars": min(len(text), cap)}


def file_info(settings: Settings, path: str) -> dict[str, Any]:
    target = assert_allowed(path, settings.allowed_roots)
    if not target.exists():
        raise FileNotFoundError(f"Path not found: {target}")
    return _record(target)


def create_folder(settings: Settings, path: str) -> dict[str, Any]:
    target = assert_allowed(path, settings.allowed_roots)
    target.mkdir(parents=True, exist_ok=True)
    return {"created": str(target), "exists": target.exists()}


def _coerce_sources(sources: list[str] | str) -> list[str]:
    if isinstance(sources, str):
        return [sources]
    return [str(value) for value in sources]


def move_files(settings: Settings, sources: list[str] | str, destination_dir: str,
               overwrite: bool = False) -> dict[str, Any]:
    destination = assert_allowed(destination_dir, settings.allowed_roots)
    destination.mkdir(parents=True, exist_ok=True)
    moved: list[dict[str, str]] = []
    for raw in _coerce_sources(sources):
        source = assert_allowed(raw, settings.allowed_roots)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Source file not found: {source}")
        target = assert_allowed(destination / source.name, settings.allowed_roots)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Destination exists: {target}")
        if target.exists() and overwrite:
            target.unlink()
        shutil.move(str(source), str(target))
        moved.append({"from": str(source), "to": str(target)})
    return {"count": len(moved), "moved": moved, "paths": [item["to"] for item in moved]}


def copy_files(settings: Settings, sources: list[str] | str, destination_dir: str,
               overwrite: bool = False) -> dict[str, Any]:
    destination = assert_allowed(destination_dir, settings.allowed_roots)
    destination.mkdir(parents=True, exist_ok=True)
    copied: list[dict[str, str]] = []
    for raw in _coerce_sources(sources):
        source = assert_allowed(raw, settings.allowed_roots)
        if not source.exists() or not source.is_file():
            raise FileNotFoundError(f"Source file not found: {source}")
        target = assert_allowed(destination / source.name, settings.allowed_roots)
        if target.exists() and not overwrite:
            raise FileExistsError(f"Destination exists: {target}")
        shutil.copy2(source, target)
        copied.append({"from": str(source), "to": str(target)})
    return {"count": len(copied), "copied": copied, "paths": [item["to"] for item in copied]}


def rename_file(settings: Settings, path: str, new_name: str, overwrite: bool = False) -> dict[str, Any]:
    source = assert_allowed(path, settings.allowed_roots)
    if not source.exists() or not source.is_file():
        raise FileNotFoundError(f"Source file not found: {source}")
    if Path(new_name).name != new_name or new_name in {"", ".", ".."}:
        raise ValueError("new_name must be a filename only, not a path")
    target = assert_allowed(source.with_name(new_name), settings.allowed_roots)
    if target.exists() and not overwrite:
        raise FileExistsError(f"Destination exists: {target}")
    if target.exists() and overwrite:
        target.unlink()
    source.rename(target)
    return {"from": str(source), "to": str(target), "path": str(target)}


TOOL_FUNCTIONS = {
    "list_files": list_files,
    "search_files": search_files,
    "search_text": search_text,
    "largest_files": largest_files,
    "read_text": read_text,
    "file_info": file_info,
    "create_folder": create_folder,
    "move_files": move_files,
    "copy_files": copy_files,
    "rename_file": rename_file,
    "web_search": web_search,
    "web_open": web_open,
}
