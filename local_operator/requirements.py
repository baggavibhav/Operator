from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Settings
from .security import SecurityError, assert_allowed
from .task_state import TaskState


@dataclass(frozen=True)
class MissingInput:
    field: str
    question: str


_SOURCE_PATTERNS = (
    r"\bfrom\s+(?:my\s+)?[^,.]+",
    r"\b(?:on|in)\s+my\s+(?:desktop|downloads|documents)\b",
    r"\b(?:on|in)\s+(?:the\s+)?(?:desktop|downloads|documents)\b",
)
_DESTINATION_PATTERN = re.compile(
    r"\b(?:into|to)\s+(?:the\s+)?([A-Za-z0-9_. -]+?)(?:\s+folder)?(?:[.!?]|$)",
    flags=re.IGNORECASE,
)
_CREATE_FOLDER_PATTERN = re.compile(r"\bcreate\s+(?:a\s+)?(?:folder|directory)\b", re.IGNORECASE)
_SPECIAL_SOURCE_PATTERN = re.compile(
    r"\b(?:from|on|in)\s+(?:my\s+|the\s+)?(desktop|downloads|documents)\b",
    flags=re.IGNORECASE,
)
_EXTENSION_WORDS = {
    "pdf": ".pdf",
    "png": ".png",
    "jpg": ".jpg",
    "jpeg": ".jpeg",
    "gif": ".gif",
    "webp": ".webp",
    "csv": ".csv",
    "json": ".json",
    "markdown": ".md",
    "md": ".md",
    "python": ".py",
    "py": ".py",
    "text": ".txt",
    "txt": ".txt",
}


def _move_or_copy_action(goal: str) -> str | None:
    text = " " + " ".join(goal.casefold().split()) + " "
    if " move " in text:
        return "move"
    if " copy " in text:
        return "copy"
    return None


def _looks_like_bulk_move_or_copy(goal: str) -> bool:
    text = " " + " ".join(goal.casefold().split()) + " "
    if _move_or_copy_action(goal) is None:
        return False
    return " all " in text or any(token in text for token in (" files ", " pdfs ", " images ", " documents "))


def _goal_has_explicit_source(goal: str) -> bool:
    text = " ".join(goal.casefold().split())
    return any(re.search(pattern, text) for pattern in _SOURCE_PATTERNS)


def _destination_name(goal: str) -> str | None:
    match = _DESTINATION_PATTERN.search(goal)
    if not match:
        return None
    value = match.group(1).strip()
    if not value or value.casefold() in {"it", "them", "there"}:
        return None
    return value


def _extension_from_goal(goal: str) -> str | None:
    explicit = re.search(r"(?<!\w)\.([A-Za-z0-9]{1,10})\b", goal)
    if explicit:
        return "." + explicit.group(1).casefold()
    text = " ".join(goal.casefold().split())
    for word, extension in _EXTENSION_WORDS.items():
        if re.search(rf"\b{re.escape(word)}(?:s|\s+files?)?\b", text):
            return extension
    return None


def _read_only_largest_request(goal: str) -> tuple[int, bool] | None:
    text = " ".join(goal.casefold().split())
    if "largest" not in text or "file" not in text:
        return None
    match = re.search(r"\b(?:the\s+)?(\d{1,2})\s+largest\s+files?\b", text)
    limit = int(match.group(1)) if match else 5
    recursive = any(token in text for token in ("including subfolders", "recursively", "all subfolders"))
    return max(1, min(limit, 50)), recursive


def _selected_file_info_request(goal: str) -> bool:
    text = " ".join(goal.casefold().split())
    return "selected" in text and "file" in text and any(token in text for token in ("tell me", "what file", "which file", "file info", "information"))


def goal_allows_write(goal: str) -> bool:
    """Conservative side-effect contract used to reject model-invented writes."""
    text = " " + " ".join(goal.casefold().split()) + " "
    return any(token in text for token in (
        " move ", " copy ", " create ", " make a folder ", " make folder ",
        " rename ", " organize ", " organise ",
    ))


def infer_initial_requirements(task: TaskState) -> list[MissingInput]:
    if not _looks_like_bulk_move_or_copy(task.goal):
        return []
    if "explicit_sources" in task.context:
        return []
    if "source_folder" not in task.context and not _goal_has_explicit_source(task.goal):
        return [MissingInput("source_folder", "Which folder should I move/copy the files from?")]
    return []


def infer_field_from_question(question: str) -> str:
    text = " ".join(question.casefold().split())
    if any(token in text for token in (
        "which folder", "what folder", "where are", "where is", "located",
        "source folder", "source directory", "files from", "move/copy the files from",
    )) and not any(token in text for token in ("where should", "files to", "target", "destination")):
        return "source_folder"
    if any(token in text for token in (
        "destination", "where should", "move them to", "copy them to", "target folder", "files to",
    )):
        return "destination_folder"
    return "clarification_answer"


def bind_user_answer(task: TaskState, answer: str, settings: Settings) -> None:
    field = task.pending_field
    if not field:
        raise ValueError("Task has no pending input field.")
    value = answer.strip()
    if not value:
        raise ValueError("Clarification answer cannot be empty.")
    if field in {"source_folder", "destination_folder", "path"}:
        try:
            resolved = assert_allowed(value, settings.allowed_roots)
        except (SecurityError, OSError, ValueError) as exc:
            raise ValueError(
                f"I couldn't safely resolve '{value}' to an allowed folder. Please give a folder such as Desktop, Downloads, Documents, or an allowed full path."
            ) from exc
        task.context[field] = str(resolved)
        task.context[f"{field}_user_answer"] = value
    else:
        clarifications = task.context.setdefault("clarifications", {})
        if not isinstance(clarifications, dict):
            clarifications = {}
            task.context["clarifications"] = clarifications
        clarifications[field] = value
    task.pending_field = None
    task.pending_question = None


def _derive_source_from_goal(task: TaskState, settings: Settings) -> None:
    if "source_folder" in task.context:
        return
    match = _SPECIAL_SOURCE_PATTERN.search(task.goal)
    if not match:
        return
    try:
        task.context["source_folder"] = str(assert_allowed(match.group(1), settings.allowed_roots))
        task.context["source_derived_from_goal"] = True
    except (SecurityError, OSError, ValueError):
        pass


def _derive_destination(task: TaskState, settings: Settings) -> None:
    if "destination_folder" in task.context:
        return
    name = _destination_name(task.goal)
    if not name:
        return

    desktop = task.context.get("desktop_context")
    current = desktop.get("current_folder") if isinstance(desktop, dict) else None
    source = task.context.get("source_folder")
    anchors: list[str] = []
    # When a source is explicit, a bare destination such as OperatorTest is most
    # naturally resolved beside that source before unrelated foreground context.
    for value in (source, current):
        if isinstance(value, str) and value and value not in anchors:
            anchors.append(value)

    for anchor in anchors:
        candidate = Path(anchor) / name
        try:
            resolved = assert_allowed(candidate, settings.allowed_roots)
        except (SecurityError, OSError, ValueError):
            continue
        if resolved.exists() and resolved.is_dir():
            task.context["destination_folder"] = str(resolved)
            task.context["destination_derived_from_context"] = True
            return


def _derive_desktop_context(task: TaskState, settings: Settings) -> None:
    desktop = task.context.get("desktop_context")
    if not isinstance(desktop, dict):
        return
    current = desktop.get("current_folder")
    selected = desktop.get("selected_files")
    goal = " ".join(task.goal.casefold().split())
    if isinstance(current, str) and current and any(token in goal for token in ("this folder", "this directory", "from here")):
        try:
            task.context.setdefault("source_folder", str(assert_allowed(current, settings.allowed_roots)))
        except (SecurityError, OSError, ValueError):
            pass

    deictic = any(token in goal for token in (
        "these files", "these pdfs", "selected files", "selected pdfs", "these documents",
        "file i have selected", "file i've selected", "currently selected file", "selected file",
    ))
    if deictic and isinstance(selected, list) and selected:
        extension = task.context.get("file_extension")
        safe: list[str] = []
        for value in selected:
            if not isinstance(value, str):
                continue
            try:
                path = assert_allowed(value, settings.allowed_roots)
            except (SecurityError, OSError, ValueError):
                continue
            if not path.is_file():
                continue
            if isinstance(extension, str) and extension and path.suffix.casefold() != extension.casefold():
                continue
            safe.append(str(path))
        if safe:
            task.context["explicit_sources"] = safe
            task.context.setdefault("source_folder", str(Path(safe[0]).parent))


def derive_context(task: TaskState, settings: Settings) -> None:
    if "file_extension" not in task.context:
        extension = _extension_from_goal(task.goal)
        if extension:
            task.context["file_extension"] = extension

    _derive_source_from_goal(task, settings)
    _derive_desktop_context(task, settings)
    _derive_destination(task, settings)
    if "destination_folder" in task.context:
        return

    match = re.search(
        r"\b(?:folder|directory)\s+(?:called|named)\s+([^\s,.;]+)\s+inside\s+([^\s,.;]+)",
        task.goal,
        flags=re.IGNORECASE,
    )
    if not match:
        return
    child, parent = match.group(1), match.group(2)
    source = task.context.get("source_folder")
    candidates = [f"{parent}/{child}"]
    if isinstance(source, str) and source:
        candidates.insert(0, str(Path(source) / parent / child))
    for candidate in candidates:
        try:
            destination = assert_allowed(candidate, settings.allowed_roots)
        except (SecurityError, OSError, ValueError):
            continue
        task.context["destination_folder"] = str(destination)
        task.context["destination_derived_from_goal"] = True
        return


def compile_deterministic_plan(task: TaskState) -> dict[str, Any] | None:
    """Compile common grounded file operations without asking a small model to invent tool plumbing."""
    largest = _read_only_largest_request(task.goal)
    source = task.context.get("source_folder")
    if largest and isinstance(source, str) and source:
        limit, recursive = largest
        return {
            "summary": f"Find the {limit} largest files and report their names and sizes",
            "clarification": None,
            "steps": [{
                "tool": "largest_files",
                "args": {"path": source, "limit": limit, "recursive": recursive},
                "reason": "Rank files deterministically by size; no write action is needed.",
            }],
        }

    explicit = task.context.get("explicit_sources")
    if _selected_file_info_request(task.goal) and isinstance(explicit, list) and len(explicit) == 1 and isinstance(explicit[0], str):
        return {
            "summary": "Report information about the selected file",
            "clarification": None,
            "steps": [{
                "tool": "file_info",
                "args": {"path": explicit[0]},
                "reason": "Use the captured desktop selection as authoritative context.",
            }],
        }

    action = _move_or_copy_action(task.goal)
    if action is None:
        return None
    destination = task.context.get("destination_folder")
    if not isinstance(destination, str) or not destination:
        return None

    tool = f"{action}_files"
    destination_path = Path(destination)
    create_requested = bool(_CREATE_FOLDER_PATTERN.search(task.goal)) and not destination_path.exists()

    if isinstance(explicit, list) and explicit and all(isinstance(item, str) for item in explicit):
        steps: list[dict[str, Any]] = []
        if create_requested:
            steps.append({"tool": "create_folder", "args": {"path": destination}, "reason": "Create the requested destination."})
        steps.append({
            "tool": tool,
            "args": {"sources": list(explicit), "destination_dir": destination},
            "reason": "Use the user's selected files as authoritative sources.",
        })
        return {"summary": f"{action.title()} the selected files to the grounded destination", "clarification": None, "steps": steps}

    extension = task.context.get("file_extension")
    if not isinstance(source, str) or not source or not isinstance(extension, str) or not extension:
        return None

    steps: list[dict[str, Any]] = []
    if create_requested:
        steps.append({"tool": "create_folder", "args": {"path": destination}, "reason": "Create the requested destination."})
    search_index = len(steps)
    steps.append({
        "tool": "search_files",
        "args": {"path": source, "extension": extension, "recursive": False, "limit": 200},
        "reason": "Discover matching files in the grounded source.",
    })
    steps.append({
        "tool": tool,
        "args": {"sources": f"$steps.{search_index}.paths", "destination_dir": destination},
        "reason": f"{action.title()} only files returned by the grounded search.",
    })
    return {"summary": f"Find matching files in the grounded source and {action} them", "clarification": None, "steps": steps}


def planner_context(task: TaskState) -> dict[str, Any]:
    context = {
        "task_id": task.id,
        "goal": task.goal,
        "known": task.context,
        "current_step": task.current_step,
        "recovery_attempts": int(task.context.get("recovery_attempts", 0)),
    }
    if task.error:
        context["last_error"] = task.error
    return context


def path_for_context_field(task: TaskState, field: str) -> Path | None:
    value = task.context.get(field)
    return Path(value) if isinstance(value, str) and value else None
