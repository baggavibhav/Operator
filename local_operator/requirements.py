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
    return " all " in text or any(token in text for token in (" pdf files ", " pdfs ", " files "))


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


def infer_initial_requirements(task: TaskState) -> list[MissingInput]:
    if not _looks_like_bulk_move_or_copy(task.goal):
        return []

    if "explicit_sources" not in task.context:
        if "source_folder" not in task.context and not _goal_has_explicit_source(task.goal):
            return [MissingInput("source_folder", "Which folder should I move/copy the files from?")]

    if "destination_folder" not in task.context:
        return [MissingInput("destination_folder", "Which folder should I move/copy the files to?")]
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
    for value in (current, source):
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

    deictic = any(token in goal for token in ("these files", "these pdfs", "selected files", "selected pdfs", "these documents"))
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
    if "file_extension" not in task.context and re.search(r"\bpdf(?:s|\s+files?)?\b", task.goal, flags=re.IGNORECASE):
        task.context["file_extension"] = ".pdf"

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
    try:
        destination = assert_allowed(f"{parent}/{child}", settings.allowed_roots)
    except (SecurityError, OSError, ValueError):
        return
    task.context["destination_folder"] = str(destination)
    task.context["destination_derived_from_goal"] = True


def compile_deterministic_plan(task: TaskState) -> dict[str, Any] | None:
    """Compile common grounded file operations without asking the LLM to invent tool steps.

    The model still interprets open-ended tasks. When the orchestrator already owns
    explicit files or a grounded source + extension + destination, execution should
    be deterministic and independent of small-model planner variance.
    """
    action = _move_or_copy_action(task.goal)
    if action is None:
        return None
    destination = task.context.get("destination_folder")
    if not isinstance(destination, str) or not destination:
        return None

    tool = f"{action}_files"
    explicit = task.context.get("explicit_sources")
    if isinstance(explicit, list) and explicit and all(isinstance(item, str) for item in explicit):
        return {
            "summary": f"{action.title()} the selected files to the grounded destination",
            "clarification": None,
            "steps": [{
                "tool": tool,
                "args": {"sources": list(explicit), "destination_dir": destination},
                "reason": "Use the user's selected files as authoritative sources.",
            }],
        }

    source = task.context.get("source_folder")
    extension = task.context.get("file_extension")
    if not isinstance(source, str) or not source or not isinstance(extension, str) or not extension:
        return None
    return {
        "summary": f"Find matching files in the grounded source and {action} them",
        "clarification": None,
        "steps": [
            {
                "tool": "search_files",
                "args": {"path": source, "extension": extension, "recursive": False, "limit": 200},
                "reason": "Discover matching files locally instead of asking the user for exact paths.",
            },
            {
                "tool": tool,
                "args": {"sources": "$steps.0.paths", "destination_dir": destination},
                "reason": f"{action.title()} only the files returned by the grounded search.",
            },
        ],
    }


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
