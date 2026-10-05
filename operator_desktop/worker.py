from __future__ import annotations

import re
import threading
from dataclasses import dataclass
from typing import Any, Callable

from local_operator.config import Settings
from local_operator.model_backend import ModelBackend, OllamaBackend
from local_operator.orchestrator import OperatorOrchestrator, OrchestratorHooks
from local_operator.task_state import TaskStore


@dataclass(frozen=True)
class TaskCallbacks:
    on_plan: Callable[[dict[str, Any]], None]
    on_state: Callable[[str], None]
    on_done: Callable[[list[dict[str, Any]]], None]
    on_clarification: Callable[[str], None]
    on_task: Callable[[dict[str, Any]], None]
    on_error: Callable[[str], None]
    on_cancelled: Callable[[str], None]
    request_approval: Callable[[str, dict[str, Any]], bool]


_CASUAL_REPLIES = {
    "hi": "Hi! What can I do?",
    "hello": "Hello! What can I do?",
    "hey": "Hey! What can I do?",
    "good morning": "Good morning! What can I do?",
    "good afternoon": "Good afternoon! What can I do?",
    "good evening": "Good evening! What can I do?",
    "thanks": "You're welcome.",
    "thank you": "You're welcome.",
    "thankyou": "You're welcome.",
}


def _casual_reply(text: str) -> str | None:
    """Keep obvious social turns out of the action planner and tool layer."""
    normalized = re.sub(r"[^a-z0-9 ]+", "", text.casefold()).strip()
    normalized = " ".join(normalized.split())
    return _CASUAL_REPLIES.get(normalized)


class OperatorWorker:
    """Runs the persistent orchestrator away from the GUI event loop."""

    def __init__(self, settings: Settings, callbacks: TaskCallbacks, backend: ModelBackend | None = None,
                 store: TaskStore | None = None, context_provider: Callable[[], dict[str, Any]] | None = None):
        self.settings = settings
        self.callbacks = callbacks
        self.backend = backend or OllamaBackend(settings)
        self.orchestrator = OperatorOrchestrator(settings, self.backend, store=store, context_provider=context_provider)
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._cancel_event = threading.Event()

    @property
    def busy(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def current_task(self) -> dict[str, Any] | None:
        task = self.orchestrator.current_task()
        return task.snapshot() if task else None

    def recent_tasks(self, limit: int = 10) -> list[dict[str, Any]]:
        return [task.snapshot() for task in self.orchestrator.recent_tasks(limit)]

    def stop_current(self) -> dict[str, Any] | None:
        if self.busy:
            self._cancel_event.set()
            return self.current_task()
        task = self.orchestrator.cancel_current("Task cancelled by user.")
        if task is not None:
            snapshot = task.snapshot()
            self.callbacks.on_task(snapshot)
            self.callbacks.on_cancelled("Task cancelled by user.")
            return snapshot
        return None

    def close(self) -> None:
        self._cancel_event.set()
        self.orchestrator.close()

    def submit(self, request: str) -> bool:
        if not request.strip():
            return False
        if not self._lock.acquire(blocking=False):
            return False
        self._cancel_event.clear()
        self._thread = threading.Thread(target=self._run, args=(request.strip(),), daemon=True,
                                        name="operator-orchestrator")
        self._thread.start()
        return True

    def _run(self, request: str) -> None:
        try:
            casual = _casual_reply(request)
            if casual is not None:
                self.callbacks.on_state("done")
                self.callbacks.on_done([{"answer": casual, "kind": "conversation"}])
                return

            hooks = OrchestratorHooks(on_state=self.callbacks.on_state, on_plan=self.callbacks.on_plan,
                                      on_task=self.callbacks.on_task)
            outcome = self.orchestrator.handle_message(
                request, confirmer=self.callbacks.request_approval, hooks=hooks,
                should_cancel=self._cancel_event.is_set,
            )
            if outcome.kind == "clarification":
                self.callbacks.on_clarification(outcome.message)
                return
            if outcome.kind == "cancelled":
                self.callbacks.on_cancelled(outcome.message or "Task cancelled by user.")
                return
            self.callbacks.on_done(outcome.results or [])
        except Exception as exc:
            self.callbacks.on_error(str(exc))
        finally:
            self._cancel_event.clear()
            self._lock.release()
