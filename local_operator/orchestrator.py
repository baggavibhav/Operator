from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .config import Settings
from .executor import ExecutionCancelled, ExecutionError, execute_plan
from .model_backend import ModelBackend
from .planner import PlannerError, bind_known_context, prepare_plan
from .requirements import (
    bind_user_answer,
    compile_deterministic_plan,
    derive_context,
    infer_field_from_question,
    infer_initial_requirements,
    planner_context,
)
from .security import risk_for
from .task_state import TaskState, TaskStatus, TaskStore
from .tools import TOOL_FUNCTIONS
from .verifier import verify_plan


@dataclass(frozen=True)
class OrchestratorOutcome:
    kind: str
    task: TaskState
    message: str = ""
    results: list[dict[str, Any]] | None = None


@dataclass(frozen=True)
class OrchestratorHooks:
    on_state: Callable[[str], None] = lambda _state: None
    on_plan: Callable[[dict[str, Any]], None] = lambda _plan: None
    on_task: Callable[[dict[str, Any]], None] = lambda _task: None
    on_step: Callable[[int, str, dict[str, Any]], None] = lambda _idx, _tool, _result: None


class OperatorOrchestrator:
    def __init__(self, settings: Settings, backend: ModelBackend, store: TaskStore | None = None,
                 audit: AuditLog | None = None,
                 context_provider: Callable[[], dict[str, Any]] | None = None):
        self.settings = settings
        self.backend = backend
        self.store = store or TaskStore()
        self.audit = audit or AuditLog()
        self.context_provider = context_provider
        self.store.mark_inflight_interrupted()

    def current_task(self) -> TaskState | None:
        active = self.store.latest_active()
        if active is not None:
            return active
        recent = self.store.recent(1)
        if recent and recent[0].status is TaskStatus.INTERRUPTED:
            return recent[0]
        return None

    def cancel_current(self, reason: str = "Cancelled by user.") -> TaskState | None:
        return self.store.cancel_active(reason)

    def close(self) -> None:
        self.store.close()
        self.audit.close()

    @staticmethod
    def _cancelled(task: TaskState, hooks: OrchestratorHooks,
                   message: str = "Task cancelled by user.") -> OrchestratorOutcome:
        task.status = TaskStatus.CANCELLED
        task.pending_field = None
        task.pending_question = None
        task.error = message
        task.context["cancelled_by_user"] = True
        hooks.on_state("done")
        return OrchestratorOutcome("cancelled", task, message, task.results)

    def handle_message(self, message: str, confirmer: Callable[[str, dict[str, Any]], bool],
                       hooks: OrchestratorHooks | None = None,
                       should_cancel: Callable[[], bool] | None = None) -> OrchestratorOutcome:
        hooks = hooks or OrchestratorHooks()
        text = message.strip()
        if not text:
            raise ValueError("Task message cannot be empty.")
        waiting = self.store.latest_waiting()
        if waiting is not None:
            task = waiting
            bind_user_answer(task, text, self.settings)
            task.status = TaskStatus.READY
            task.error = None
            derive_context(task, self.settings)
            self._save(task, hooks)
        else:
            task = self.store.create(text)
            if self.context_provider is not None:
                try:
                    snapshot = self.context_provider()
                    if isinstance(snapshot, dict):
                        task.context["desktop_context"] = snapshot
                except Exception as exc:
                    task.context["desktop_context_error"] = str(exc)
            derive_context(task, self.settings)
            self._emit_task(task, hooks)
        if should_cancel is not None and should_cancel():
            outcome = self._cancelled(task, hooks)
            self._save(task, hooks)
            return outcome
        return self._advance(task, confirmer, hooks, should_cancel=should_cancel)

    def _advance(self, task: TaskState, confirmer: Callable[[str, dict[str, Any]], bool],
                 hooks: OrchestratorHooks,
                 should_cancel: Callable[[], bool] | None = None) -> OrchestratorOutcome:
        missing = infer_initial_requirements(task)
        if missing:
            need = missing[0]
            task.pending_field = need.field
            task.pending_question = need.question
            task.status = TaskStatus.WAITING_FOR_INPUT
            self._save(task, hooks)
            hooks.on_state("waiting_for_input")
            return OrchestratorOutcome("clarification", task, need.question)

        # Explicit public-web research requests use a bounded act→observe→reason
        # loop. This avoids forcing an open-ended research goal into one static
        # plan and prevents needless questions such as "how should I summarize?".
        if self._should_use_web_agent(task) and callable(getattr(self.backend, "agent_turn", None)):
            ok, detail = self.backend.available()
            if not ok:
                task.status = TaskStatus.FAILED
                task.error = f"Local model runtime is unavailable at {self.settings.ollama_url}. {detail}"
                self._save(task, hooks)
                raise RuntimeError(task.error)
            return self._run_web_agent(task, hooks, should_cancel=should_cancel)

        hooks.on_state("thinking")
        task.status = TaskStatus.PLANNING
        self._save(task, hooks)

        grounded_desktop = isinstance(task.context.get("desktop_context"), dict)
        planned = compile_deterministic_plan(task) if grounded_desktop else None
        if planned is not None:
            task.context["plan_source"] = "deterministic_orchestrator"
            planned = prepare_plan(planned, self.settings)
        else:
            ok, detail = self.backend.available()
            if not ok:
                task.status = TaskStatus.FAILED
                task.error = f"Local model runtime is unavailable at {self.settings.ollama_url}. {detail}"
                self._save(task, hooks)
                raise RuntimeError(task.error)
            planned = self._plan_or_clarify(task, hooks)
            if isinstance(planned, OrchestratorOutcome):
                return planned
            task.context["plan_source"] = "model"

        if should_cancel is not None and should_cancel():
            outcome = self._cancelled(task, hooks)
            self._save(task, hooks)
            return outcome

        task.plan = planned
        task.status = TaskStatus.READY
        task.current_step = 0
        task.results = []
        self._save(task, hooks)
        hooks.on_plan(planned)
        return self._execute(task, confirmer, hooks, should_cancel=should_cancel)

    @staticmethod
    def _should_use_web_agent(task: TaskState) -> bool:
        text = " ".join(task.goal.casefold().split())
        if re.search(r"https?://\S+", text):
            return True
        explicit_web = any(token in text for token in ("the web", " web ", "internet", "online"))
        research_verb = any(token in text for token in ("search", "browse", "research", "look up", "lookup", "find"))
        return explicit_web and research_verb

    def _run_web_agent(self, task: TaskState, hooks: OrchestratorHooks,
                       should_cancel: Callable[[], bool] | None = None) -> OrchestratorOutcome:
        if not self.settings.web_enabled:
            raise RuntimeError("Web access is disabled in Operator settings.")

        task.context["plan_source"] = "bounded_web_agent"
        task.plan = {"summary": "Bounded read-only web research agent", "clarification": None, "steps": []}
        task.results = []
        task.current_step = 0
        task.status = TaskStatus.EXECUTING
        self._save(task, hooks)
        hooks.on_plan(task.plan)

        run_id = self.audit.start_run(task.goal, self.settings.model, {"mode": "bounded_web_agent"})
        observations: list[dict[str, Any]] = []
        seen_actions: set[str] = set()
        max_turns = max(2, min(int(self.settings.max_steps), 8))

        try:
            for turn in range(max_turns):
                if should_cancel is not None and should_cancel():
                    self.audit.finish_run(run_id, "cancelled", "Task cancelled by user.")
                    outcome = self._cancelled(task, hooks)
                    self._save(task, hooks)
                    return outcome

                hooks.on_state("thinking")
                task.status = TaskStatus.PLANNING
                self._save(task, hooks)
                decision = self.backend.agent_turn(task.goal, observations)
                if not isinstance(decision, dict):
                    raise RuntimeError("Agent reasoning turn did not return an object.")

                kind = str(decision.get("type", "")).casefold().strip()
                if kind == "final":
                    answer = str(decision.get("answer") or "").strip()
                    if not answer:
                        raise RuntimeError("Agent produced an empty final answer.")
                    raw_suggestions = decision.get("suggestions")
                    suggestions = []
                    if isinstance(raw_suggestions, list):
                        for value in raw_suggestions:
                            if isinstance(value, str) and value.strip():
                                suggestions.append(value.strip())
                            if len(suggestions) >= 3:
                                break
                    final_result = {
                        "answer": answer,
                        "suggestions": suggestions,
                        "agent_turns": turn + 1,
                        "source_count": sum(1 for item in observations if item.get("tool") == "web_open"),
                    }
                    task.results.append(final_result)
                    task.context["web_agent_observations"] = len(observations)
                    task.context["suggestions"] = suggestions
                    task.status = TaskStatus.COMPLETED
                    task.error = None
                    self._save(task, hooks)
                    hooks.on_state("done")
                    self.audit.finish_run(run_id, "ok")
                    return OrchestratorOutcome("completed", task, answer, task.results)

                if kind != "tool":
                    raise RuntimeError("Agent must choose a read-only web tool or finish with an answer.")
                tool = str(decision.get("tool", "")).strip()
                if tool not in {"web_search", "web_open"}:
                    raise RuntimeError(f"Agent attempted unsupported tool: {tool or '(missing)'}")
                args = decision.get("args")
                if not isinstance(args, dict):
                    raise RuntimeError("Agent tool arguments must be an object.")

                # Validate a one-step plan through the same deterministic compiler
                # used by the normal planner. It enforces argument shape and URL
                # safety before a network request is attempted.
                validated = prepare_plan(
                    {
                        "summary": str(decision.get("reason") or "Read public web information"),
                        "clarification": None,
                        "steps": [{"tool": tool, "args": args, "reason": str(decision.get("reason") or "")}],
                    },
                    self.settings,
                )
                clean_args = validated["steps"][0]["args"]

                # For a model-selected open, require that the exact URL was
                # actually observed previously. This prevents hallucinated URLs.
                if tool == "web_open":
                    target = str(clean_args.get("url") or "")
                    observed_urls: set[str] = set()
                    for item in observations:
                        result = item.get("result")
                        if not isinstance(result, dict):
                            continue
                        if isinstance(result.get("url"), str):
                            observed_urls.add(result["url"])
                        for candidate in result.get("results", []) if isinstance(result.get("results"), list) else []:
                            if isinstance(candidate, dict) and isinstance(candidate.get("url"), str):
                                observed_urls.add(candidate["url"])
                        for candidate in result.get("links", []) if isinstance(result.get("links"), list) else []:
                            if isinstance(candidate, dict) and isinstance(candidate.get("url"), str):
                                observed_urls.add(candidate["url"])
                    user_supplied_url = re.search(r"https?://\S+", task.goal)
                    if target not in observed_urls and not (user_supplied_url and target.rstrip(".,)") == user_supplied_url.group(0).rstrip(".,)")):
                        raise RuntimeError("Agent attempted to open a URL that was not present in prior observations.")

                fingerprint = json.dumps({"tool": tool, "args": clean_args}, sort_keys=True, default=str)
                if fingerprint in seen_actions:
                    observations.append({"tool": "orchestrator", "result": {"warning": "Duplicate web action blocked. Use existing observations or choose a different action."}})
                    continue
                seen_actions.add(fingerprint)

                hooks.on_state("working")
                task.status = TaskStatus.EXECUTING
                self._save(task, hooks)
                started = time.perf_counter()
                try:
                    result = TOOL_FUNCTIONS[tool](self.settings, **clean_args)
                except Exception as exc:
                    duration = (time.perf_counter() - started) * 1000
                    self.audit.action(run_id, turn, tool, "read", clean_args, None, None, "failed", duration, str(exc))
                    observations.append({"tool": tool, "result": {"error": str(exc)}})
                    continue

                duration = (time.perf_counter() - started) * 1000
                self.audit.action(run_id, turn, tool, "read", clean_args, None, result, "ok", duration)
                observation = {"tool": tool, "result": result}
                observations.append(observation)
                task.results.append(result)
                task.current_step = len(observations)
                self._save(task, hooks)
                hooks.on_step(turn, tool, result)

            raise RuntimeError("Web agent reached its bounded turn limit before completing the goal.")
        except Exception as exc:
            task.status = TaskStatus.FAILED
            task.error = str(exc)
            self._save(task, hooks)
            self.audit.finish_run(run_id, "failed", str(exc))
            raise

    def _execute(self, task: TaskState, confirmer: Callable[[str, dict[str, Any]], bool],
                 hooks: OrchestratorHooks,
                 should_cancel: Callable[[], bool] | None = None) -> OrchestratorOutcome:
        assert task.plan is not None
        task.status = TaskStatus.EXECUTING
        self._save(task, hooks)
        hooks.on_state("working")
        executed_write = False

        def checkpoint(index: int, tool: str, result: dict[str, Any]) -> None:
            nonlocal executed_write
            if risk_for(tool) == "write":
                executed_write = True
            if len(task.results) == index:
                task.results.append(result)
            elif index < len(task.results):
                task.results[index] = result
            else:
                task.results.extend({} for _ in range(index - len(task.results)))
                task.results.append(result)
            task.current_step = index + 1
            self._save(task, hooks)
            hooks.on_step(index, tool, result)

        try:
            results = execute_plan(
                task.plan,
                settings=self.settings,
                request=task.goal,
                confirmer=confirmer,
                audit=self.audit,
                model_name=self.settings.model,
                on_step=checkpoint,
                should_cancel=should_cancel,
            )
        except ExecutionCancelled:
            outcome = self._cancelled(task, hooks)
            self._save(task, hooks)
            return outcome
        except Exception as exc:
            task.error = str(exc)
            if isinstance(exc, ExecutionError) and str(exc).startswith("Write action denied by user:"):
                task.status = TaskStatus.CANCELLED
                self._save(task, hooks)
                hooks.on_state("done")
                return OrchestratorOutcome("cancelled", task, "Action cancelled by user.", task.results)
            if not executed_write and int(task.context.get("execution_recovery_attempts", 0)) < 1:
                task.context["execution_recovery_attempts"] = 1
                task.context["last_execution_error"] = str(exc)
                task.status = TaskStatus.READY
                self._save(task, hooks)
                return self._recover_read_only_failure(task, confirmer, hooks, should_cancel=should_cancel)
            task.status = TaskStatus.FAILED
            self._save(task, hooks)
            raise

        task.results = results
        task.status = TaskStatus.VERIFYING
        self._save(task, hooks)
        check = verify_plan(task.plan, results)
        if not check.ok:
            task.status = TaskStatus.FAILED
            task.error = check.message
            self._save(task, hooks)
            raise RuntimeError(f"Post-execution verification failed: {check.message}")
        task.context["verification"] = check.message
        task.status = TaskStatus.COMPLETED
        task.error = None
        self._save(task, hooks)
        hooks.on_state("done")
        return OrchestratorOutcome("completed", task, "Task completed and verified.", results)

    def _recover_read_only_failure(self, task: TaskState, confirmer: Callable[[str, dict[str, Any]], bool],
                                   hooks: OrchestratorHooks,
                                   should_cancel: Callable[[], bool] | None = None) -> OrchestratorOutcome:
        if should_cancel is not None and should_cancel():
            outcome = self._cancelled(task, hooks)
            self._save(task, hooks)
            return outcome
        hooks.on_state("thinking")
        task.status = TaskStatus.PLANNING
        task.results = []
        task.current_step = 0
        self._save(task, hooks)
        note = ("A previous attempt failed before any write action occurred. Replan once using the recorded error in "
                "orchestrator context. Do not repeat an invalid path or tool call.")
        planned = self._plan_or_clarify(task, hooks, control_note=note)
        if isinstance(planned, OrchestratorOutcome):
            return planned
        task.plan = planned
        task.status = TaskStatus.READY
        self._save(task, hooks)
        hooks.on_plan(planned)
        return self._execute(task, confirmer, hooks, should_cancel=should_cancel)

    def _backend_propose(self, request: str) -> dict[str, Any]:
        proposer = getattr(self.backend, "propose", None)
        if callable(proposer):
            return proposer(request)
        legacy = getattr(self.backend, "plan", None)
        if callable(legacy):
            return legacy(request)
        raise RuntimeError("Model backend does not provide a propose() method.")

    def _plan_or_clarify(self, task: TaskState, hooks: OrchestratorHooks,
                         control_note: str | None = None) -> dict[str, Any] | OrchestratorOutcome:
        note = control_note
        last_error: str | None = None
        for _attempt in range(2):
            candidate = self._backend_propose(self._planner_request(task, control_note=note))
            if not isinstance(candidate, dict):
                last_error = "Planner candidate must be a JSON object."
                note = last_error
                continue
            candidate = bind_known_context(candidate, planner_context(task))
            clarification = candidate.get("clarification")
            steps = candidate.get("steps")
            has_steps = isinstance(steps, list) and len(steps) > 0
            if isinstance(clarification, str) and clarification.strip():
                question = clarification.strip()
                field = infer_field_from_question(question)
                if field in task.context:
                    task.context["planner_stale_clarification"] = question
                    if has_steps:
                        candidate = dict(candidate)
                        candidate["clarification"] = None
                    else:
                        last_error = f"Planner requested already-known field '{field}'."
                        note = (f"{last_error} The orchestrator already has {field}={task.context.get(field)!r}. "
                                "Return executable steps using durable task state.")
                        continue
                else:
                    task.pending_field = field
                    task.pending_question = question
                    task.plan = None
                    task.status = TaskStatus.WAITING_FOR_INPUT
                    self._save(task, hooks)
                    hooks.on_state("waiting_for_input")
                    return OrchestratorOutcome("clarification", task, question)
            try:
                return prepare_plan(candidate, self.settings)
            except (PlannerError, PermissionError) as exc:
                last_error = str(exc)
                note = ("Deterministic plan compilation rejected the previous candidate: "
                        f"{last_error}. Preserve all known orchestrator context and return one corrected executable plan.")
        task.status = TaskStatus.FAILED
        task.error = last_error or "Planner could not produce a valid executable plan."
        self._save(task, hooks)
        raise RuntimeError(task.error)

    def _planner_request(self, task: TaskState, control_note: str | None = None) -> str:
        context = planner_context(task)
        if control_note:
            context["orchestrator_control_note"] = control_note
        known = task.context
        grounded_lines: list[str] = []
        for field in ("source_folder", "destination_folder"):
            value = known.get(field)
            if isinstance(value, str) and value:
                grounded_lines.append(f"{field.upper()}: {value}")
        grounded = "\n".join(grounded_lines)
        if grounded:
            grounded = f"\nGROUNDED_PATHS_BEGIN\n{grounded}\nGROUNDED_PATHS_END\n"
        return (
            f"USER_GOAL:\n{task.goal}\n"
            f"{grounded}\nORCHESTRATOR_CONTEXT_JSON_BEGIN\n"
            f"{json.dumps(context, indent=2, default=str)}\nORCHESTRATOR_CONTEXT_JSON_END"
        )

    def _save(self, task: TaskState, hooks: OrchestratorHooks) -> None:
        self.store.save(task)
        self._emit_task(task, hooks)

    @staticmethod
    def _emit_task(task: TaskState, hooks: OrchestratorHooks) -> None:
        hooks.on_task(task.snapshot())
