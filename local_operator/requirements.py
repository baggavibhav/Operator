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


def _looks_like_bulk_move_or_copy(goal: str) -> bool:
    text = " " + " ".join(goal.casefold().split()) + " "
    if not any(word in text for word in (" move ", " copy ")):
        return False
    bulk = " all " in text or any(token in text for token in (" pdf files ", " pdfs ", " files "))
    return bulk


def _goal_has_explicit_source(goal: str) -> bool:
    text = " ".join(goal.casefold().split())
    return any(re.search(pattern, text) for pattern in _SOURCE_PATTERNS)


def infer_initial_requirements(task: TaskState) -> list[MissingInput]:
    """Return deterministic missing inputs before asking the model to plan.

    The orchestrator, not the LLM, owns obvious task-state requirements. V0.3
    intentionally starts with a conservative filesystem rule: broad move/copy
    operations need an explicit source location before planning can continue.
    """
    if _looks_like_bulk_move_or_copy(task.goal):
        if "source_folder" not in task.context and not _goal_has_explicit_source(task.goal):
            return [MissingInput("source_folder", "Which folder should I move/copy the files from?")]
    return []


def infer_field_from_question(question: str) -> str:
    """Map model clarification text to a durable state slot.

    Known semantic slots get stable names. Unknown questions are still persisted
    using a generic field so the answer remains attached to the same task rather
    than becoming a brand-new user request.
    """
    text = " ".join(question.casefold().split())
    if any(token in text for token in (
        "which folder", "what folder", "where are", "where is", "located",
        "source folder", "source directory", "files from", "move/copy the files from",
    )):
        return "source_folder"
    if any(token in text for token in (
        "destination", "where should", "move them to", "copy them to", "target folder",
    )):
        return "destination_folder"
    return "clarification_answer"


def bind_user_answer(task: TaskState, answer: str, settings: Settings) -> None:
    """Attach a user's follow-up to the exact pending task field.

    Path-like fields are grounded through the sandbox before entering task state.
    Unknown clarification fields retain literal text without inventing semantics.
    """
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



def derive_context(task: TaskState, settings: Settings) -> None:
    """Derive narrow, deterministic facts from the goal once prerequisites exist.

    V0.3 intentionally keeps this conservative. For example, after a source
    folder is known, "a folder called PDFs inside OperatorTest" can be grounded
    through the existing sandbox without asking the model to invent a path.
    """
    # Narrow file-type constraints belong to task state rather than to the
    # planner. This prevents an underspecified model search from broadening a
    # request such as "all PDF files" into "all files".
    if "file_extension" not in task.context and re.search(r"\bpdf(?:s|\s+files?)?\b", task.goal, flags=re.IGNORECASE):
        task.context["file_extension"] = ".pdf"

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

def planner_context(task: TaskState) -> dict[str, Any]:
    """Small, structured context passed to the planning component."""
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
