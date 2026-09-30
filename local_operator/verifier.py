from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class VerificationResult:
    ok: bool
    message: str


def verify_step(tool: str, result: dict[str, Any]) -> VerificationResult:
    """Verify observable postconditions for deterministic filesystem writes."""
    try:
        if tool == "create_folder":
            path = Path(str(result.get("created", "")))
            return VerificationResult(path.is_dir(), f"Folder {'exists' if path.is_dir() else 'missing'}: {path}")

        if tool in {"move_files", "copy_files"}:
            paths = result.get("paths", [])
            if not isinstance(paths, list):
                return VerificationResult(False, "Write result did not contain a path list.")
            missing = [str(path) for path in paths if not Path(str(path)).is_file()]
            if missing:
                return VerificationResult(False, f"Expected output files are missing: {missing[:5]}")
            return VerificationResult(True, f"Verified {len(paths)} output file(s).")

        if tool == "rename_file":
            path = Path(str(result.get("path", result.get("to", ""))))
            return VerificationResult(path.is_file(), f"Renamed file {'exists' if path.is_file() else 'missing'}: {path}")

        # Read-only tools already fail loudly if their operation is invalid; no
        # additional filesystem mutation postcondition is required.
        return VerificationResult(True, "Read-only step completed.")
    except (OSError, TypeError, ValueError) as exc:
        return VerificationResult(False, f"Verification failed: {exc}")


def verify_plan(plan: dict[str, Any], results: list[dict[str, Any]]) -> VerificationResult:
    steps = plan.get("steps", [])
    if len(steps) != len(results):
        return VerificationResult(False, "Execution result count does not match the plan step count.")
    messages: list[str] = []
    for step, result in zip(steps, results):
        check = verify_step(str(step.get("tool")), result)
        messages.append(check.message)
        if not check.ok:
            return check
    return VerificationResult(True, "; ".join(messages) if messages else "No execution steps required.")
