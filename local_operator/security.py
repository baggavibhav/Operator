from __future__ import annotations

import re
from pathlib import Path


class SecurityError(PermissionError):
    pass


READ_TOOLS = {"list_files", "search_files", "search_text", "largest_files", "read_text", "file_info", "web_search", "web_open"}
WRITE_TOOLS = {"create_folder", "move_files", "copy_files", "rename_file"}
ALLOWED_TOOLS = READ_TOOLS | WRITE_TOOLS

_SPECIAL_FOLDERS = {
    "downloads": "Downloads",
    "desktop": "Desktop",
    "documents": "Documents",
}


def _resolve_user_folder_alias(value: str) -> Path | None:
    """Resolve common aliases without trusting invented absolute paths."""
    raw = value.strip().strip('"').strip("'")
    if not raw:
        return None

    slashy = raw.replace("\\", "/")
    folded = slashy.casefold().rstrip("/")
    home = Path.home()

    if folded in {"~", "$home", "home", "my home", "home folder"}:
        return home

    for key, folder_name in _SPECIAL_FOLDERS.items():
        prefixes = (key, f"~/{key}", f"$home/{key}")
        for prefix in prefixes:
            if folded == prefix:
                return home / folder_name
            if folded.startswith(prefix + "/"):
                suffix = slashy[len(prefix):].lstrip("/")
                return home / folder_name / Path(suffix)

    match = re.match(
        r"^/(?:home|users)/[^/]+/(downloads|desktop|documents)(?:/(.*))?$",
        slashy,
        flags=re.IGNORECASE,
    )
    if match:
        folder_name = _SPECIAL_FOLDERS[match.group(1).casefold()]
        suffix = match.group(2)
        return home / folder_name / Path(suffix) if suffix else home / folder_name

    return None


def _resolve_alias_against_roots(value: str, roots: tuple[Path, ...]) -> Path | None:
    raw = value.strip().strip('"').strip("'").replace("\\", "/")
    folded = raw.casefold().rstrip("/")
    for key, folder_name in _SPECIAL_FOLDERS.items():
        prefixes = (key, f"~/{key}", f"$home/{key}")
        for prefix in prefixes:
            if folded == prefix or folded.startswith(prefix + "/"):
                matching = [root for root in roots if root.name.casefold() == folder_name.casefold()]
                if len(matching) == 1:
                    suffix = raw[len(prefix):].lstrip("/")
                    return (matching[0] / Path(suffix)).resolve(strict=False) if suffix else matching[0]
    return None


def normalize_path(value: str | Path) -> Path:
    if isinstance(value, str):
        aliased = _resolve_user_folder_alias(value)
        if aliased is not None:
            return aliased.expanduser().resolve(strict=False)
    return Path(value).expanduser().resolve(strict=False)


def is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return path == root


def _existing_prefix_depth(candidate: Path, root: Path) -> int:
    try:
        rel = candidate.relative_to(root)
    except ValueError:
        return -1
    parts = rel.parts
    for depth in range(len(parts), 0, -1):
        prefix = root.joinpath(*parts[:depth])
        if prefix.exists():
            return depth
    return 0


def resolve_allowed_path(path_value: str | Path, allowed_roots: tuple[Path, ...]) -> Path:
    roots = tuple(Path(root).expanduser().resolve(strict=False) for root in allowed_roots)
    if not roots:
        raise SecurityError("No allowed roots are configured.")

    if isinstance(path_value, str):
        configured_alias = _resolve_alias_against_roots(path_value, roots)
        if configured_alias is not None:
            return configured_alias
        aliased = _resolve_user_folder_alias(path_value)
        if aliased is not None:
            candidate = aliased.expanduser().resolve(strict=False)
            if any(is_within(candidate, root) for root in roots):
                return candidate
            printable = ", ".join(str(root) for root in roots)
            raise SecurityError(f"Path is outside allowed roots: {candidate}. Allowed roots: {printable}")

    raw = Path(path_value).expanduser()
    if raw.is_absolute():
        candidate = raw.resolve(strict=False)
        if any(is_within(candidate, root) for root in roots):
            return candidate
        printable = ", ".join(str(root) for root in roots)
        raise SecurityError(f"Path is outside allowed roots: {candidate}. Allowed roots: {printable}")

    scored: list[tuple[int, Path]] = []
    for root in roots:
        candidate = (root / raw).resolve(strict=False)
        scored.append((_existing_prefix_depth(candidate, root), candidate))

    best_depth = max(depth for depth, _ in scored)
    best = [candidate for depth, candidate in scored if depth == best_depth]
    if best_depth > 0 and len(best) == 1:
        return best[0]
    if len(roots) == 1:
        return best[0]

    cwd = Path.cwd().resolve(strict=False)
    cwd_matches = [root for root in roots if root == cwd]
    if best_depth == 0 and len(cwd_matches) == 1:
        return (cwd / raw).resolve(strict=False)

    matches = ", ".join(str(candidate) for candidate in best)
    raise SecurityError(
        f"Ambiguous relative path '{path_value}'. Ground it to a specific allowed root. "
        f"Candidate paths: {matches}"
    )


def assert_allowed(path_value: str | Path, allowed_roots: tuple[Path, ...]) -> Path:
    return resolve_allowed_path(path_value, allowed_roots)


def risk_for(tool_name: str) -> str:
    if tool_name in READ_TOOLS:
        return "read"
    if tool_name in WRITE_TOOLS:
        return "write"
    raise SecurityError(f"Unknown or forbidden tool: {tool_name}")
