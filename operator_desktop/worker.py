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


# Tool access is opt-in by intent. Unknown text is conversation, never an action.
_ACTION_VERBS = {
    "find", "list", "show", "search", "locate", "read", "open", "summarize", "inspect",
    "create", "make", "move", "copy", "rename", "organize", "sort", "put", "transfer",
}
_LOCAL_NOUNS = {
    "file", "files", "folder", "folders", "desktop", "downloads", "documents", "pdf", "pdfs",
    "png", "pngs", "image", "images", "selected", "selection", "directory", "directories",
}
_WEB_MARKERS = {
    "web", "internet", "online", "website", "url", "latest", "current", "news", "announcement",
    "announcements", "google", "search online", "search the web", "look up online", "browse",
}
_WRITE_VERBS = {"create", "make", "move", "copy", "rename", "organize", "sort", "put", "transfer"}


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.casefold()))


def _route(request: str) -> str:
    """Deterministic capability gate. The LLM never grants itself tools."""
    lower = " ".join(request.casefold().split())
    words = _tokens(lower)

    # Explicit web intent gets only the existing read-only web path.
    if any(marker in lower for marker in _WEB_MARKERS):
        if words & {"search", "find", "look", "browse", "summarize", "latest", "current", "news", "announcement", "announcements", "open", "read"}:
            return "web"

    # Local computer access requires BOTH an action verb and a local-resource signal.
    # This prevents social/general questions from silently touching private files.
    if words & _ACTION_VERBS and words & _LOCAL_NOUNS:
        return "computer"

    # Path-like requests are actionable only with an explicit supported action verb.
    if words & _ACTION_VERBS and ("~/" in request or "/Users/" in request or "\\Users\\" in request):
        return "computer"

    return "conversation"


def _friendly_result(result: Any) -> Any:
    """Keep internal tool envelopes out of the normal conversation surface."""
    if not isinstance(result, dict):
        return result
    if isinstance(result.get("answer"), str):
        return {"answer": result["answer"], "kind": result.get("kind", "answer")}
    if isinstance(result.get("summary"), str):
        return {"answer": result["summary"], "kind": "summary"}
    return result


class OperatorWorker:
    def __init__(self, settings: Settings, callbacks: TaskCallbacks, backend: ModelBackend | None = None,
                 store: TaskStore | None = None, context_provider: Callable[[], dict[str, Any]] | None = None):
        self.settings=settings; self.callbacks=callbacks; self.backend=backend or OllamaBackend(settings)
        self.orchestrator=OperatorOrchestrator(settings,self.backend,store=store,context_provider=context_provider)
        self._lock=threading.Lock(); self._thread=None; self._cancel_event=threading.Event()

    @property
    def busy(self): return self._thread is not None and self._thread.is_alive()
    def current_task(self):
        task=self.orchestrator.current_task(); return task.snapshot() if task else None
    def recent_tasks(self,limit=10): return [t.snapshot() for t in self.orchestrator.recent_tasks(limit)]
    def stop_current(self):
        if self.busy: self._cancel_event.set(); return self.current_task()
        task=self.orchestrator.cancel_current("Task cancelled by user.")
        if task is not None:
            snap=task.snapshot(); self.callbacks.on_task(snap); self.callbacks.on_cancelled("Task cancelled by user."); return snap
        return None
    def close(self): self._cancel_event.set(); self.orchestrator.close()
    def submit(self,request):
        if not request.strip() or not self._lock.acquire(blocking=False): return False
        self._cancel_event.clear(); self._thread=threading.Thread(target=self._run,args=(request.strip(),),daemon=True,name="operator-orchestrator"); self._thread.start(); return True

    def _run(self,request):
        try:
            route=_route(request)
            if route=="conversation":
                self.callbacks.on_state("thinking")
                answer=self.backend.chat(request)
                self.callbacks.on_state("done")
                self.callbacks.on_done([{"answer":answer,"kind":"conversation"}])
                return

            # Web and computer requests enter the orchestrator only after the deterministic gate.
            # Existing security policy still constrains roots, tools, writes, approvals and network flow.
            hooks=OrchestratorHooks(on_state=self.callbacks.on_state,on_plan=self.callbacks.on_plan,on_task=self.callbacks.on_task)
            outcome=self.orchestrator.handle_message(request,confirmer=self.callbacks.request_approval,hooks=hooks,should_cancel=self._cancel_event.is_set)
            if outcome.kind=="clarification": self.callbacks.on_clarification(outcome.message); return
            if outcome.kind=="cancelled": self.callbacks.on_cancelled(outcome.message or "Task cancelled by user."); return
            self.callbacks.on_done([_friendly_result(r) for r in (outcome.results or [])])
        except Exception as exc: self.callbacks.on_error(str(exc))
        finally:
            self._cancel_event.clear(); self._lock.release()
