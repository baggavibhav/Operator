from __future__ import annotations

import json
from typing import Any


def format_plan(plan: dict[str, Any]) -> str:
    summary = str(plan.get("summary") or "Plan ready.")
    if plan.get("clarification"):
        return str(plan["clarification"])
    steps = plan.get("steps") or []
    lines = [summary]
    for index, step in enumerate(steps, 1):
        tool = step.get("tool", "unknown")
        reason = step.get("reason", "")
        lines.append(f"{index}. {tool}" + (f" — {reason}" if reason else ""))
    return "\n".join(lines)


def _human_size(value: int | float) -> str:
    size = float(value)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(size) < 1024.0 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024.0
    return f"{size:.1f} TB"


def format_result(result: Any) -> str:
    if not isinstance(result, dict):
        return str(result)
    if "answer" in result:
        answer = str(result.get("answer") or "").strip()
        suggestions = result.get("suggestions")
        lines = [answer or "Done."]
        if isinstance(suggestions, list):
            clean = [str(item).strip() for item in suggestions if str(item).strip()][:3]
            if clean:
                lines.append("")
                lines.append("You can continue with:")
                lines.extend(f"• {item}" for item in clean)
        return "\n".join(lines)
    if "files" in result and isinstance(result["files"], list):
        files = result["files"]
        if not files:
            return "No matching files found."
        lines = []
        for item in files[:12]:
            name = item.get("name") or item.get("path") or "file"
            size = item.get("size_bytes")
            lines.append(f"• {name}" + (f" — {_human_size(size)}" if isinstance(size, (int, float)) else ""))
        if len(files) > 12:
            lines.append(f"…and {len(files) - 12} more")
        return "\n".join(lines)
    if "results" in result and isinstance(result["results"], list):
        items = result["results"]
        if not items:
            return "No web results found."
        lines = []
        for item in items[:8]:
            title = item.get("title") or item.get("url") or "result"
            url = item.get("url") or ""
            lines.append(f"• {title}" + (f"\n  {url}" if url else ""))
        return "\n".join(lines)
    if "content" in result and "url" in result:
        title = result.get("title") or result.get("url")
        content = str(result.get("content") or "").strip()
        return f"{title}\n\n{content[:3500]}" + ("\n…" if len(content) > 3500 else "")
    if "created" in result:
        return f"Created: {result['created']}"
    if "moved" in result:
        count = result.get("count", len(result["moved"]))
        return "No matching files found to move." if count == 0 else f"Moved {count} file(s)."
    if "copied" in result:
        count = result.get("count", len(result["copied"]))
        return "No matching files found to copy." if count == 0 else f"Copied {count} file(s)."
    if "to" in result and "from" in result:
        return f"Renamed/moved:\n{result['from']}\n→ {result['to']}"
    text = json.dumps(result, indent=2, default=str)
    return text[:5000] + ("\n…" if len(text) > 5000 else "")
