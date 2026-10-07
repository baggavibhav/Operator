from __future__ import annotations

import copy
import json
import platform
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from .config import Settings
from .security import ALLOWED_TOOLS, assert_allowed

TOOL_DOCS = """
Available tools and arguments:
- list_files(path, recursive=false, extension=null, limit=100)
- search_files(path, name_contains="", extension=null, modified_within_days=null, recursive=true, limit=100)
- search_text(path, query, extensions=null, recursive=true, limit=50)
- largest_files(path, limit=5, recursive=true)
- read_text(path, max_chars=null)
- file_info(path)
- create_folder(path)
- move_files(sources, destination_dir, overwrite=false)
- copy_files(sources, destination_dir, overwrite=false)
- rename_file(path, new_name, overwrite=false)
- web_search(query, limit=5)
- web_open(url, max_chars=null)

A later step may reference an earlier result using strings like:
$steps.0.paths
$steps.1.path

Rules:
1. Only use the listed tools.
2. Never invent a path the task does not imply except a child folder/file name needed to satisfy the goal.
3. Prefer read-only discovery before writes when target files are not explicit.
4. Web access is read-only in V0.4: web_search and web_open may retrieve public http/https content. There is still NO delete, arbitrary shell, authenticated web action, purchasing, email sending, or generic desktop control.
5. Keep plans short and dependency ordered.
6. For moving/copying files found by a search, sources MUST reference $steps.N.paths. Never use wildcards.
7. Search the source folder before creating/using a destination and moving the results.
8. If ORCHESTRATOR_CONTEXT_JSON contains source_folder or destination_folder, treat those values as already resolved task state. Do not ask for them again.
9. Ask for clarification only when genuinely required information is absent from both the user goal and orchestrator context.
10. Return JSON only, with no markdown.
"""

_TOOL_ARG_SPEC: dict[str, dict[str, set[str]]] = {
    "list_files": {"required": {"path"}, "optional": {"recursive", "extension", "limit"}},
    "search_files": {"required": {"path"}, "optional": {"name_contains", "extension", "modified_within_days", "recursive", "limit"}},
    "search_text": {"required": {"path", "query"}, "optional": {"extensions", "recursive", "limit"}},
    "largest_files": {"required": {"path"}, "optional": {"limit", "recursive"}},
    "read_text": {"required": {"path"}, "optional": {"max_chars"}},
    "file_info": {"required": {"path"}, "optional": set()},
    "create_folder": {"required": {"path"}, "optional": set()},
    "move_files": {"required": {"sources", "destination_dir"}, "optional": {"overwrite"}},
    "copy_files": {"required": {"sources", "destination_dir"}, "optional": {"overwrite"}},
    "rename_file": {"required": {"path", "new_name"}, "optional": {"overwrite"}},
    "web_search": {"required": {"query"}, "optional": {"limit"}},
    "web_open": {"required": {"url"}, "optional": {"max_chars"}},
}

_PATH_FIELDS: dict[str, tuple[str, ...]] = {
    "list_files": ("path",),
    "search_files": ("path",),
    "search_text": ("path",),
    "largest_files": ("path",),
    "read_text": ("path",),
    "file_info": ("path",),
    "create_folder": ("path",),
    "move_files": ("destination_dir",),
    "copy_files": ("destination_dir",),
    "rename_file": ("path",),
}

_CONTEXT_BEGIN = "ORCHESTRATOR_CONTEXT_JSON_BEGIN"
_CONTEXT_END = "ORCHESTRATOR_CONTEXT_JSON_END"


class PlannerError(RuntimeError):
    pass


def build_system_prompt(settings: Settings) -> str:
    roots = "\n".join(f"- {root}" for root in settings.allowed_roots)
    return f"""You are the planning component inside UNNAMED Local Operator V0.4.
The orchestrator owns task state, clarifications, execution, approvals, recovery, and completion.
Your job is narrower: propose a safe minimal plan for the current task state.

Runtime environment:
- Operating system: {platform.system()}
- User home: {Path.home()}
- Current working directory: {Path.cwd()}
- Allowed roots:
{roots}

Path rules:
- Use runtime paths above; never invent another operating system's home path.
- Downloads/Desktop/Documents aliases refer to this current user.
- Never use placeholders such as /home/user, /Users/user, C:\\Users\\user, USERNAME, or <user>.
- The orchestrator may append a trusted ORCHESTRATOR_CONTEXT_JSON block. Use its known fields as task state.

Return exactly this shape:
{{
  "summary": "short explanation",
  "clarification": null,
  "steps": [
    {{"tool": "tool_name", "args": {{"arg": "value"}}, "reason": "why"}}
  ]
}}

If genuinely missing information prevents a safe plan, return:
{{
  "summary": "Need one detail before acting",
  "clarification": "short question",
  "steps": []
}}

""" + TOOL_DOCS


def ollama_available(settings: Settings) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(settings.ollama_url.rstrip("/") + "/api/tags", timeout=2.5) as response:
            if response.status == 200:
                return True, "ok"
    except Exception as exc:
        return False, str(exc)
    return False, "Ollama did not return HTTP 200"


def _extract_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise PlannerError("Local model did not return valid JSON.")
        try:
            value = json.loads(text[start:end + 1])
        except json.JSONDecodeError as exc:
            raise PlannerError(f"Local model returned invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise PlannerError("Plan must be a JSON object.")
    return value


def _extract_orchestrator_context(user_request: str) -> dict[str, Any]:
    start = user_request.rfind(_CONTEXT_BEGIN)
    end = user_request.rfind(_CONTEXT_END)
    if start < 0 or end <= start:
        return {}
    raw = user_request[start + len(_CONTEXT_BEGIN):end].strip()
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _ref_index(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\$steps\.(\d+)\.paths", value)
    return int(match.group(1)) if match else None


def _has_wildcard(value: str) -> bool:
    return any(token in value for token in ("*", "?", "[", "]"))


def validate_plan(plan: dict[str, Any], settings: Settings) -> dict[str, Any]:
    summary = plan.get("summary", "")
    clarification = plan.get("clarification")
    steps = plan.get("steps")
    if not isinstance(summary, str):
        raise PlannerError("Plan summary must be a string.")
    if clarification is not None and not isinstance(clarification, str):
        raise PlannerError("Plan clarification must be a string or null.")
    if not isinstance(steps, list):
        raise PlannerError("Plan steps must be an array.")
    if isinstance(clarification, str) and clarification.strip():
        if steps:
            raise PlannerError("A planner response must either ask for clarification or contain executable steps, not both.")
        return {"summary": summary.strip(), "clarification": clarification.strip(), "steps": []}
    if len(steps) > settings.max_steps:
        raise PlannerError(f"Plan has {len(steps)} steps; maximum is {settings.max_steps}.")

    clean_steps: list[dict[str, Any]] = []
    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            raise PlannerError(f"Step {idx} must be an object.")
        tool = step.get("tool")
        args = step.get("args", {})
        reason = step.get("reason", "")
        if tool not in ALLOWED_TOOLS:
            raise PlannerError(f"Step {idx} uses forbidden/unknown tool: {tool}")
        if not isinstance(args, dict):
            raise PlannerError(f"Step {idx} args must be an object.")
        if not isinstance(reason, str):
            reason = str(reason)

        spec = _TOOL_ARG_SPEC[tool]
        missing = sorted(spec["required"] - set(args))
        if missing:
            raise PlannerError(f"Step {idx} {tool} is missing required argument(s): {', '.join(missing)}.")
        unknown = sorted(set(args) - spec["required"] - spec["optional"])
        if unknown:
            raise PlannerError(f"Step {idx} {tool} has unknown argument(s): {', '.join(unknown)}.")

        if tool in {"move_files", "copy_files"}:
            sources = args["sources"]
            ref_idx = _ref_index(sources)
            if isinstance(sources, str):
                if ref_idx is not None:
                    if ref_idx >= idx:
                        raise PlannerError(f"Step {idx} references a future/unavailable step {ref_idx}.")
                elif _has_wildcard(sources):
                    raise PlannerError(f"Step {idx} uses wildcard sources. Search first and pass $steps.N.paths instead.")
            elif isinstance(sources, list):
                if not all(isinstance(item, str) and not _has_wildcard(item) for item in sources):
                    raise PlannerError(f"Step {idx} sources must be explicit paths without wildcards.")
            else:
                raise PlannerError(f"Step {idx} sources must be a path list or $steps.N.paths reference.")

        if tool == "web_search":
            query = args.get("query")
            if not isinstance(query, str) or not query.strip():
                raise PlannerError(f"Step {idx} web_search query must be a non-empty string.")
            if query.startswith("$steps."):
                raise PlannerError("web_search cannot send prior tool output to a search provider.")

        if tool == "web_open":
            url = args.get("url")
            if not isinstance(url, str) or not url.strip():
                raise PlannerError(f"Step {idx} web_open url must be a non-empty string.")
            if url.startswith("$steps."):
                match = re.fullmatch(r"\$steps\.(\d+)\.(?:results|links)\.\d+\.url", url)
                if not match:
                    raise PlannerError("web_open may only follow a URL returned by a prior web_search/web_open step.")
                source_idx = int(match.group(1))
                if source_idx >= idx:
                    raise PlannerError(f"Step {idx} references a future/unavailable web step {source_idx}.")
                source_tool = clean_steps[source_idx]["tool"] if source_idx < len(clean_steps) else None
                if source_tool not in {"web_search", "web_open"}:
                    raise PlannerError("web_open cannot send filesystem/tool data to the network.")
            else:
                parsed = urllib.parse.urlparse(url)
                if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                    raise PlannerError("web_open requires a public http/https URL or a prior web result URL reference.")

        clean_steps.append({"tool": tool, "args": copy.deepcopy(args), "reason": reason})

    return {"summary": summary.strip(), "clarification": None, "steps": clean_steps}


def _ground_static_path(value: Any, settings: Settings) -> Any:
    if isinstance(value, str) and not value.startswith("$steps."):
        return str(assert_allowed(value, settings.allowed_roots))
    return value


def bind_known_context(plan: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(plan, dict):
        return plan
    known = context.get("known") if isinstance(context.get("known"), dict) else {}
    source = known.get("source_folder")
    destination = known.get("destination_folder")
    file_extension = known.get("file_extension")
    explicit_sources = known.get("explicit_sources")
    bound = copy.deepcopy(plan)
    steps = bound.get("steps")
    if not isinstance(steps, list):
        return bound

    referenced: set[int] = set()
    for step in steps:
        if not isinstance(step, dict) or step.get("tool") not in {"move_files", "copy_files"}:
            continue
        args = step.get("args")
        if isinstance(args, dict):
            ref = _ref_index(args.get("sources"))
            if ref is not None:
                referenced.add(ref)

    for idx, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        args = step.get("args")
        if not isinstance(args, dict):
            args = {}
            step["args"] = args
        tool = step.get("tool")
        if source and idx in referenced and tool in {"search_files", "list_files", "largest_files"}:
            args["path"] = source
        if file_extension and idx in referenced and tool == "search_files":
            args["extension"] = file_extension
        if destination and tool == "create_folder":
            args["path"] = destination
        if destination and tool in {"move_files", "copy_files"}:
            args["destination_dir"] = destination
        if explicit_sources and tool in {"move_files", "copy_files"}:
            args["sources"] = list(explicit_sources) if isinstance(explicit_sources, list) else explicit_sources
    return bound


def prepare_plan(plan: dict[str, Any], settings: Settings) -> dict[str, Any]:
    prepared = validate_plan(plan, settings)
    if prepared.get("clarification"):
        return prepared

    for step in prepared["steps"]:
        tool = step["tool"]
        args = step["args"]
        for field in _PATH_FIELDS.get(tool, ()):
            if field in args:
                args[field] = _ground_static_path(args[field], settings)
        if tool in {"move_files", "copy_files"}:
            sources = args.get("sources")
            if isinstance(sources, list):
                args["sources"] = [_ground_static_path(item, settings) for item in sources]
            elif isinstance(sources, str) and not sources.startswith("$steps."):
                args["sources"] = _ground_static_path(sources, settings)

    for idx, step in enumerate(prepared["steps"]):
        if step["tool"] not in {"move_files", "copy_files"}:
            continue
        ref_idx = _ref_index(step["args"].get("sources"))
        if ref_idx is None:
            continue
        source_step = prepared["steps"][ref_idx]
        source_path = source_step["args"].get("path")
        destination = step["args"].get("destination_dir")
        if isinstance(source_path, str) and isinstance(destination, str):
            if Path(source_path).resolve(strict=False) == Path(destination).resolve(strict=False):
                raise PlannerError(
                    f"Step {idx} would search the destination and move those same results back into it. Search the source directory instead."
                )
    return prepared


def _call_ollama(messages: list[dict[str, str]], settings: Settings) -> str:
    payload = {
        "model": settings.model,
        "messages": messages,
        "stream": False,
        "format": "json",
        "options": {"temperature": 0},
    }
    request = urllib.request.Request(
        settings.ollama_url.rstrip("/") + "/api/chat",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise PlannerError(f"Ollama HTTP {exc.code}: {body[:500]}") from exc
    except urllib.error.URLError as exc:
        raise PlannerError(f"Cannot reach Ollama at {settings.ollama_url}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise PlannerError("Timed out waiting for the local model.") from exc

    try:
        return data["message"]["content"]
    except (KeyError, TypeError) as exc:
        raise PlannerError(f"Unexpected Ollama response: {data}") from exc


def propose_with_ollama(user_request: str, settings: Settings) -> dict[str, Any]:
    system = build_system_prompt(settings)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_request},
    ]
    return _extract_json(_call_ollama(messages, settings))


def plan_with_ollama(user_request: str, settings: Settings) -> dict[str, Any]:
    context = _extract_orchestrator_context(user_request)
    system = build_system_prompt(settings)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_request},
    ]
    first_raw = bind_known_context(_extract_json(_call_ollama(messages, settings)), context)
    try:
        return prepare_plan(first_raw, settings)
    except (PlannerError, PermissionError) as exc:
        repair_messages = messages + [
            {"role": "assistant", "content": json.dumps(first_raw)},
            {
                "role": "user",
                "content": (
                    "Deterministic plan validation rejected that candidate:\n"
                    f"{exc}\n"
                    "Return one corrected JSON plan. Keep already-known orchestrator context, preserve dependency order, "
                    "and do not mix clarification with executable steps."
                ),
            },
        ]
        repaired = bind_known_context(_extract_json(_call_ollama(repair_messages, settings)), context)
        try:
            return prepare_plan(repaired, settings)
        except (PlannerError, PermissionError) as repair_exc:
            raise PlannerError(str(repair_exc)) from repair_exc
