from __future__ import annotations

import json
import re
import time
from typing import Any, Callable

from .audit import AuditLog
from .config import Settings
from .planner import prepare_plan
from .security import risk_for
from .tools import TOOL_FUNCTIONS

_REF = re.compile(r"^\$steps\.(\d+)(?:\.([A-Za-z0-9_\.]+))?$")


class ExecutionError(RuntimeError):
    pass


class ExecutionCancelled(ExecutionError):
    """Raised only at a safe boundary before another tool action begins."""


def _pluck(value: Any, path: str | None) -> Any:
    if not path:
        return value
    current = value
    for part in path.split("."):
        if isinstance(current, dict):
            if part not in current:
                raise ExecutionError(f"Reference key not found: {part}")
            current = current[part]
        elif isinstance(current, list) and part.isdigit():
            current = current[int(part)]
        else:
            raise ExecutionError(f"Cannot traverse reference segment: {part}")
    return current


def resolve_refs(value: Any, results: list[dict[str, Any]]) -> Any:
    if isinstance(value, str):
        match = _REF.match(value)
        if not match:
            return value
        index = int(match.group(1))
        if index >= len(results):
            raise ExecutionError(f"Reference points to unavailable step {index}")
        return _pluck(results[index], match.group(2))
    if isinstance(value, list):
        return [resolve_refs(item, results) for item in value]
    if isinstance(value, dict):
        return {key: resolve_refs(item, results) for key, item in value.items()}
    return value


def _preview(tool: str, args: dict[str, Any]) -> str:
    compact = json.dumps(args, indent=2, default=str)
    if len(compact) > 1600:
        compact = compact[:1600] + "\n... (preview truncated)"
    return f"{tool}\n{compact}"


def _raise_if_cancelled(should_cancel: Callable[[], bool] | None) -> None:
    if should_cancel is not None and should_cancel():
        raise ExecutionCancelled("Task cancelled by user.")


def execute_plan(plan: dict[str, Any], settings: Settings, request: str,
                 confirmer: Callable[[str, dict[str, Any]], bool], audit: AuditLog | None = None,
                 model_name: str | None = None,
                 on_step: Callable[[int, str, dict[str, Any]], None] | None = None,
                 should_cancel: Callable[[], bool] | None = None) -> list[dict[str, Any]]:
    plan = prepare_plan(plan, settings)
    if plan.get("clarification"):
        raise ExecutionError(f"Clarification required: {plan['clarification']}")
    audit = audit or AuditLog()
    run_id = audit.start_run(request=request, model=model_name or settings.model, plan=plan)
    results: list[dict[str, Any]] = []
    try:
        for idx, step in enumerate(plan["steps"]):
            # Cancellation is cooperative: never interrupt a tool mid-write. The
            # stop request is honored before the next action begins.
            _raise_if_cancelled(should_cancel)
            tool = step["tool"]
            args = resolve_refs(step["args"], results)
            risk = risk_for(tool)
            approved: bool | None = None
            if risk == "write":
                _raise_if_cancelled(should_cancel)
                approved = confirmer(tool, args)
                if not approved:
                    audit.action(run_id, idx, tool, risk, args, False, None, "denied", 0.0)
                    raise ExecutionError(f"Write action denied by user: {tool}")
                # A stop requested while the approval prompt was open prevents
                # the write even if approval was granted a moment earlier.
                _raise_if_cancelled(should_cancel)
            started = time.perf_counter()
            try:
                result = TOOL_FUNCTIONS[tool](settings, **args)
            except Exception as exc:
                duration = (time.perf_counter() - started) * 1000
                audit.action(run_id, idx, tool, risk, args, approved, None, "failed", duration, str(exc))
                raise
            duration = (time.perf_counter() - started) * 1000
            audit.action(run_id, idx, tool, risk, args, approved, result, "ok", duration)
            results.append(result)
            if on_step is not None:
                on_step(idx, tool, result)
        audit.finish_run(run_id, "ok")
        return results
    except ExecutionCancelled as exc:
        audit.finish_run(run_id, "cancelled", str(exc))
        raise
    except Exception as exc:
        audit.finish_run(run_id, "failed", str(exc))
        raise


def terminal_confirmer(tool: str, args: dict[str, Any]) -> bool:
    print("\nWRITE ACTION REQUIRES APPROVAL")
    print("-" * 40)
    print(_preview(tool, args))
    answer = input("Approve this action? [y/N]: ").strip().lower()
    return answer in {"y", "yes"}
