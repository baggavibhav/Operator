from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable

from .audit import AuditLog
from .config import Settings
from .executor import execute_plan
from .model_backend import ModelBackend
from .planner import PlannerError, bind_known_context, prepare_plan
from .requirements import (
    bind_user_answer,
    derive_context,
    infer_field_from_question,
    infer_initial_requirements,
    planner_context,
)
from .security import risk_for
from .task_state import TaskState, TaskStatus, TaskStore
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
    """Owns task lifecycle; the LLM is only a planning component.

    The orchestrator persists task state, requests missing information, invokes
    planning, gates execution through approval callbacks, checkpoints each step,
    verifies postconditions, and applies a bounded recovery policy.
    """

    def __init__(
        self,
        settings: Settings,
        backend: ModelBackend,
        store: TaskStore | None = None,
        audit: AuditLog | None = None,
    ):
        self.settings = settings
        self.backend = backend
        self.store = store or TaskStore()
        self.audit = audit or AuditLog()
        self.store.mark_inflight_interrupted()

    def current_task(self) -> TaskState | None:
        active = self.store.latest_active()
        if active is not None:
            return active
        recent = self.store.recent(1)
        if recent and recent[0].status is TaskStatus.INTERRUPTED:
            return recent[0]
        return None

    def close(self) -> None:
        self.store.close()
        self.audit.close()

    def handle_message(
        self,
        message: str,
        confirmer: Callable[[str, dict[str, Any]], bool],
        hooks: OrchestratorHooks | None = None,
    ) -> OrchestratorOutcome:
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
            derive_context(task, self.settings)
            self._emit_task(task, hooks)

        return self._advance(task, confirmer, hooks)

    def _advance(
        self,
        task: TaskState,
        confirmer: Callable[[str, dict[str, Any]], bool],
        hooks: OrchestratorHooks,
    ) -> OrchestratorOutcome:
        missing = infer_initial_requirements(task)
        if missing:
            need = missing[0]
            task.pending_field = need.field
            task.pending_question = need.question
            task.status = TaskStatus.WAITING_FOR_INPUT
            self._save(task, hooks)
            hooks.on_state("waiting_for_input")
            return OrchestratorOutcome("clarification", task, need.question)

        hooks.on_state("thinking")
        task.status = TaskStatus.PLANNING
        self._save(task, hooks)

        ok, detail = self.backend.available()
        if not ok:
            task.status = TaskStatus.FAILED
            task.error = f"Local model runtime is unavailable at {self.settings.ollama_url}. {detail}"
            self._save(task, hooks)
            raise RuntimeError(task.error)

        planned = self._plan_or_clarify(task, hooks)
        if isinstance(planned, OrchestratorOutcome):
            return planned
        plan = planned

        task.plan = plan
        task.status = TaskStatus.READY
        task.current_step = 0
        task.results = []
        self._save(task, hooks)
        hooks.on_plan(plan)

        return self._execute(task, confirmer, hooks)

    def _execute(
        self,
        task: TaskState,
        confirmer: Callable[[str, dict[str, Any]], bool],
        hooks: OrchestratorHooks,
    ) -> OrchestratorOutcome:
        assert task.plan is not None
        task.status = TaskStatus.EXECUTING
        self._save(task, hooks)
        hooks.on_state("working")

        executed_write = False

        def checkpoint(index: int, tool: str, result: dict[str, Any]) -> None:
            nonlocal executed_write
            if risk_for(tool) == "write":
                executed_write = True
            # execute_plan invokes checkpoints in order; store the complete
            # prefix so a crash leaves a meaningful durable checkpoint.
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
            )
        except Exception as exc:
            task.error = str(exc)
            # Never auto-retry a task after a write has happened. That protects
            # against duplicate or partially repeated side effects.
            if not executed_write and int(task.context.get("execution_recovery_attempts", 0)) < 1:
                task.context["execution_recovery_attempts"] = 1
                task.context["last_execution_error"] = str(exc)
                task.status = TaskStatus.READY
                self._save(task, hooks)
                return self._recover_read_only_failure(task, confirmer, hooks)
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

    def _recover_read_only_failure(
        self,
        task: TaskState,
        confirmer: Callable[[str, dict[str, Any]], bool],
        hooks: OrchestratorHooks,
    ) -> OrchestratorOutcome:
        hooks.on_state("thinking")
        task.status = TaskStatus.PLANNING
        task.results = []
        task.current_step = 0
        self._save(task, hooks)
        note = (
            "A previous attempt failed before any write action occurred. Replan once using the recorded error in orchestrator context. "
            "Do not repeat an invalid path or tool call."
        )
        planned = self._plan_or_clarify(task, hooks, control_note=note)
        if isinstance(planned, OrchestratorOutcome):
            return planned
        plan = planned
        task.plan = plan
        task.status = TaskStatus.READY
        self._save(task, hooks)
        hooks.on_plan(plan)
        return self._execute(task, confirmer, hooks)

    def _backend_propose(self, request: str) -> dict[str, Any]:
        """Ask the model for a candidate without giving it lifecycle control.

        V0.3.1 uses ``propose`` for the real backend. The ``plan`` fallback is
        kept only so older test doubles and third-party experimental backends do
        not break during this transition.
        """
        proposer = getattr(self.backend, "propose", None)
        if callable(proposer):
            return proposer(request)
        legacy = getattr(self.backend, "plan", None)
        if callable(legacy):
            return legacy(request)
        raise RuntimeError("Model backend does not provide a propose() method.")

    def _plan_or_clarify(
        self,
        task: TaskState,
        hooks: OrchestratorHooks,
        control_note: str | None = None,
    ) -> dict[str, Any] | OrchestratorOutcome:
        """Turn model candidates into either durable clarification or a safe plan.

        The orchestrator, not the model backend, resolves mixed outputs. If the
        model asks for a field already present in durable task state, that
        clarification is stale. Executable steps may still be compiled after
        known task state is bound into them. If the question is genuinely new,
        executable steps are discarded and the task waits for the user.
        """
        note = control_note
        last_error: str | None = None

        for attempt in range(2):
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
                    # The user's answer already resolved this slot. A small model
                    # may still echo its old question while also proposing valid
                    # work. The control plane owns the slot, so stale metadata is
                    # removed while the executable candidate is preserved.
                    task.context["planner_stale_clarification"] = question
                    if has_steps:
                        candidate = dict(candidate)
                        candidate["clarification"] = None
                    else:
                        last_error = f"Planner requested already-known field '{field}'."
                        note = (
                            f"{last_error} The orchestrator already has {field}={task.context.get(field)!r}. "
                            "Return executable steps using durable task state."
                        )
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
                note = (
                    "Deterministic plan compilation rejected the previous candidate: "
                    f"{last_error}. Preserve all known orchestrator context and return one corrected executable plan."
                )

        task.status = TaskStatus.FAILED
        task.error = last_error or "Planner could not produce a valid executable plan."
        self._save(task, hooks)
        raise RuntimeError(task.error)

    def _planner_request(self, task: TaskState, control_note: str | None = None) -> str:
        context = planner_context(task)
        if control_note:
            context["orchestrator_control_note"] = control_note
        return (
            f"USER_GOAL:\n{task.goal}\n\n"
            "ORCHESTRATOR_CONTEXT_JSON_BEGIN\n"
            f"{json.dumps(context, indent=2, default=str)}\n"
            "ORCHESTRATOR_CONTEXT_JSON_END"
        )

    def _save(self, task: TaskState, hooks: OrchestratorHooks) -> None:
        self.store.save(task)
        self._emit_task(task, hooks)

    @staticmethod
    def _emit_task(task: TaskState, hooks: OrchestratorHooks) -> None:
        hooks.on_task(task.snapshot())
