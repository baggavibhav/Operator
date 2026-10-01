from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_operator.audit import AuditLog
from local_operator.config import Settings
from local_operator.executor import ExecutionCancelled, execute_plan
from local_operator.orchestrator import OperatorOrchestrator
from local_operator.planner import PlannerError, prepare_plan
from local_operator.requirements import compile_deterministic_plan, derive_context, infer_initial_requirements
from local_operator.security import risk_for
from local_operator.task_state import TaskState, TaskStatus, TaskStore
from local_operator.web import WebAccessError, web_open, web_search
from operator_desktop.context import capture_desktop_context
from operator_desktop.formatting import format_result


class FakeBackend:
    name = "fake"

    def __init__(self, plans):
        self.plans = list(plans)

    def available(self):
        return True, "ok"

    def plan(self, request: str):
        if not self.plans:
            raise AssertionError("unexpected planning call")
        value = self.plans.pop(0)
        return value(request) if callable(value) else value


class V04Tests(unittest.TestCase):
    def settings(self, root: Path) -> Settings:
        return Settings("test", "http://localhost", (root,), 1000, 10)

    def test_desktop_context_selected_files_remove_source_clarification(self):
        with tempfile.TemporaryDirectory() as td:
            desktop = Path(td).resolve() / "Desktop"
            desktop.mkdir()
            target = desktop / "OperatorTest"
            target.mkdir()
            selected = desktop / "one.pdf"
            selected.write_text("1")
            task = TaskState.create("Move these PDFs into OperatorTest")
            task.context["desktop_context"] = {
                "platform": "Windows",
                "active_app": "explorer.exe",
                "current_folder": str(desktop),
                "selected_files": [str(selected)],
            }
            derive_context(task, self.settings(desktop))
            self.assertEqual(infer_initial_requirements(task), [])
            self.assertEqual(task.context["explicit_sources"], [str(selected)])
            self.assertEqual(task.context["destination_folder"], str(target))

    def test_selected_files_are_authoritative_sources(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop = base / "Desktop"
            desktop.mkdir()
            target = desktop / "OperatorTest"
            target.mkdir()
            selected = desktop / "one.pdf"
            other = desktop / "two.pdf"
            selected.write_text("1")
            other.write_text("2")
            context = lambda: {
                "platform": "Windows",
                "active_app": "explorer.exe",
                "current_folder": str(desktop),
                "selected_files": [str(selected)],
            }
            # Empty backend proves the grounded task does not need a planner call.
            orchestrator = OperatorOrchestrator(
                self.settings(desktop),
                FakeBackend([]),
                store=TaskStore(base / "tasks.db"),
                audit=AuditLog(base / "audit.db"),
                context_provider=context,
            )
            outcome = orchestrator.handle_message("Move these PDFs into OperatorTest", confirmer=lambda _t, _a: True)
            self.assertEqual(outcome.kind, "completed")
            self.assertTrue((target / "one.pdf").exists())
            self.assertTrue(other.exists())
            self.assertEqual(outcome.task.context["plan_source"], "deterministic_orchestrator")
            self.assertEqual(outcome.task.plan["steps"][0]["args"]["sources"], [str(selected)])

    def test_grounded_folder_search_is_compiled_without_model(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            source = base / "Desktop"
            source.mkdir()
            target = source / "OperatorTest"
            target.mkdir()
            (source / "one.pdf").write_text("1")
            (source / "ignore.txt").write_text("x")
            task = TaskState.create("Move all PDFs into OperatorTest")
            task.context["source_folder"] = str(source)
            task.context["destination_folder"] = str(target)
            derive_context(task, self.settings(base))
            plan = compile_deterministic_plan(task)
            self.assertIsNotNone(plan)
            self.assertEqual(plan["steps"][0]["tool"], "search_files")
            self.assertEqual(plan["steps"][1]["tool"], "move_files")
            self.assertEqual(plan["steps"][1]["args"]["sources"], "$steps.0.paths")

    def test_web_tools_are_read_only(self):
        self.assertEqual(risk_for("web_search"), "read")
        self.assertEqual(risk_for("web_open"), "read")

    def test_web_open_blocks_private_network(self):
        with self.assertRaises(WebAccessError):
            web_open(self.settings(Path.cwd().resolve()), "http://127.0.0.1/private")

    def test_web_search_parses_results_without_live_network(self):
        page = b'<html><body><div class="result"><a class="result__a" href="https://example.com/a">Example result</a><div class="result__snippet">A useful snippet.</div></div></body></html>'
        with patch("local_operator.web._open_public", return_value=("https://html.duckduckgo.com/html/", page, "utf-8")):
            result = web_search(self.settings(Path.cwd().resolve()), "example", limit=3)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["url"], "https://example.com/a")
        self.assertIn("useful", result["results"][0]["snippet"])

    def test_web_open_extracts_readable_page_without_live_network(self):
        page = b"<html><head><title>Docs</title></head><body><h1>Hello</h1><p>World</p></body></html>"
        with patch("local_operator.web._open_public", return_value=("https://example.com/docs", page, "utf-8")):
            result = web_open(self.settings(Path.cwd().resolve()), "https://example.com/docs")
        self.assertEqual(result["title"], "Docs")
        self.assertIn("Hello", result["content"])
        self.assertIn("World", result["content"])

    def test_planner_rejects_filesystem_output_exfiltration_to_web(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {
                "summary": "unsafe",
                "clarification": None,
                "steps": [
                    {"tool": "read_text", "args": {"path": str(root / "notes.txt")}, "reason": "read"},
                    {"tool": "web_open", "args": {"url": "$steps.0.content"}, "reason": "send"},
                ],
            }
            with self.assertRaisesRegex(PlannerError, "prior web_search/web_open"):
                prepare_plan(plan, self.settings(root))

    def test_planner_allows_web_search_then_open_result(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {
                "summary": "research",
                "clarification": None,
                "steps": [
                    {"tool": "web_search", "args": {"query": "python docs", "limit": 3}, "reason": "find"},
                    {"tool": "web_open", "args": {"url": "$steps.0.results.0.url"}, "reason": "read"},
                ],
            }
            prepared = prepare_plan(plan, self.settings(root))
            self.assertEqual(prepared["steps"][1]["args"]["url"], "$steps.0.results.0.url")

    def test_denied_write_becomes_cancelled_task(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {
                "summary": "create",
                "clarification": None,
                "steps": [{"tool": "create_folder", "args": {"path": str(root / "nope")}, "reason": "test"}],
            }
            orchestrator = OperatorOrchestrator(
                self.settings(root),
                FakeBackend([plan]),
                store=TaskStore(root / "tasks.db"),
                audit=AuditLog(root / "audit.db"),
            )
            outcome = orchestrator.handle_message("Create a test folder", confirmer=lambda _t, _a: False)
            self.assertEqual(outcome.kind, "cancelled")
            self.assertEqual(outcome.task.status, TaskStatus.CANCELLED)
            self.assertFalse((root / "nope").exists())

    def test_task_store_can_cancel_waiting_task(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks.db")
            task = store.create("Move PDFs")
            task.status = TaskStatus.WAITING_FOR_INPUT
            task.pending_field = "source_folder"
            task.pending_question = "Where from?"
            store.save(task)
            cancelled = store.cancel_active()
            self.assertIsNotNone(cancelled)
            self.assertEqual(cancelled.status, TaskStatus.CANCELLED)
            self.assertIsNone(cancelled.pending_question)
            self.assertIsNone(store.latest_active())

    def test_execute_plan_honors_cancel_before_action(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {
                "summary": "read",
                "clarification": None,
                "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "test"}],
            }
            with self.assertRaises(ExecutionCancelled):
                execute_plan(
                    plan,
                    self.settings(root),
                    "list files",
                    confirmer=lambda _t, _a: True,
                    audit=AuditLog(root / "audit.db"),
                    should_cancel=lambda: True,
                )

    def test_desktop_context_snapshot_has_stable_shape(self):
        snapshot = capture_desktop_context()
        self.assertIn("platform", snapshot)
        self.assertIn("active_app", snapshot)
        self.assertIn("current_folder", snapshot)
        self.assertIn("selected_files", snapshot)
        self.assertIsInstance(snapshot["selected_files"], list)

    def test_zero_move_result_is_friendly(self):
        self.assertEqual(format_result({"moved": [], "count": 0, "paths": []}), "No matching files found to move.")


if __name__ == "__main__":
    unittest.main()
