from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from local_operator.audit import AuditLog
from local_operator.config import Settings
from local_operator.orchestrator import OperatorOrchestrator
from local_operator.planner import PlannerError
from local_operator.requirements import compile_deterministic_plan, derive_context
from local_operator.task_state import TaskState, TaskStore


class FakeBackend:
    name = "fake"

    def __init__(self, plans=None, turns=None):
        self.plans = list(plans or [])
        self.turns = list(turns or [])

    def available(self):
        return True, "ok"

    def propose(self, request):
        return self.plans.pop(0)

    def agent_turn(self, goal, observations):
        return self.turns.pop(0)


class V05Tests(unittest.TestCase):
    def settings(self, root: Path) -> Settings:
        return Settings("test", "http://localhost", (root,), 12000, 10)

    def test_png_move_into_new_child_folder_uses_desktop_as_source(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop = base / "Desktop"
            desktop.mkdir()
            operator_test = desktop / "OperatorTest"
            operator_test.mkdir()
            task = TaskState.create("Create a folder called ImagesTest inside OperatorTest and move all PNG files from my Desktop into it.")
            task.context["desktop_context"] = {"current_folder": str(desktop), "selected_files": []}
            derive_context(task, self.settings(desktop))
            self.assertEqual(task.context["source_folder"], str(desktop))
            self.assertEqual(task.context["file_extension"], ".png")
            self.assertEqual(task.context["destination_folder"], str(operator_test / "ImagesTest"))
            plan = compile_deterministic_plan(task)
            self.assertEqual([s["tool"] for s in plan["steps"]], ["create_folder", "search_files", "move_files"])
            self.assertEqual(plan["steps"][1]["args"]["path"], str(desktop))
            self.assertEqual(plan["steps"][2]["args"]["sources"], "$steps.1.paths")

    def test_largest_files_is_one_read_only_deterministic_step(self):
        with tempfile.TemporaryDirectory() as td:
            desktop = Path(td).resolve() / "Desktop"
            desktop.mkdir()
            task = TaskState.create("Find the 5 largest files on my Desktop and tell me their names and sizes.")
            derive_context(task, self.settings(desktop))
            plan = compile_deterministic_plan(task)
            self.assertEqual(len(plan["steps"]), 1)
            self.assertEqual(plan["steps"][0]["tool"], "largest_files")
            self.assertEqual(plan["steps"][0]["args"]["limit"], 5)

    def test_selected_file_info_uses_captured_path_not_step_reference(self):
        with tempfile.TemporaryDirectory() as td:
            desktop = Path(td).resolve() / "Desktop"
            desktop.mkdir()
            selected = desktop / "hello.txt"
            selected.write_text("hi")
            task = TaskState.create("Tell me what file I have selected.")
            task.context["desktop_context"] = {"current_folder": str(desktop), "selected_files": [str(selected)]}
            derive_context(task, self.settings(desktop))
            plan = compile_deterministic_plan(task)
            self.assertEqual(plan["steps"][0]["tool"], "file_info")
            self.assertEqual(plan["steps"][0]["args"]["path"], str(selected))

    def test_read_only_goal_rejects_model_invented_write(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {"summary": "bad", "clarification": None, "steps": [
                {"tool": "create_folder", "args": {"path": str(root / "oops")}, "reason": "invented"}
            ]}
            orchestrator = OperatorOrchestrator(self.settings(root), FakeBackend(plans=[plan, plan]),
                                                store=TaskStore(root / "tasks.db"), audit=AuditLog(root / "audit.db"))
            with self.assertRaises(PlannerError):
                orchestrator.handle_message("Tell me what files are here", confirmer=lambda _t, _a: True)
            self.assertFalse((root / "oops").exists())

    def test_web_summary_cannot_finish_before_opening_source(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            backend = FakeBackend(turns=[
                {"type": "final", "answer": "Nothing found", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
                {"type": "final", "answer": "Still nothing", "suggestions": []},
            ])
            orchestrator = OperatorOrchestrator(self.settings(root), backend,
                                                store=TaskStore(root / "tasks.db"), audit=AuditLog(root / "audit.db"))
            with self.assertRaisesRegex(RuntimeError, "evidence/completion contract"):
                orchestrator.handle_message("Search the web for the latest NVIDIA AI announcement and summarize the top result", confirmer=lambda _t, _a: True)


if __name__ == "__main__":
    unittest.main()
