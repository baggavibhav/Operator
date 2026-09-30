from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_operator.audit import AuditLog
from local_operator.config import Settings
from local_operator.executor import ExecutionError, execute_plan, resolve_refs
from local_operator.orchestrator import OperatorOrchestrator
from local_operator.planner import PlannerError, plan_with_ollama, prepare_plan
from local_operator.requirements import derive_context, infer_field_from_question, infer_initial_requirements
from local_operator.security import SecurityError, assert_allowed, normalize_path, resolve_allowed_path
from local_operator.task_state import TaskState, TaskStatus, TaskStore
from local_operator.tools import search_files, search_text
from local_operator.verifier import verify_plan


class FakeBackend:
    name = "fake"

    def __init__(self, plans):
        self.plans = list(plans)
        self.requests: list[str] = []

    def available(self):
        return True, "ok"

    def plan(self, request: str):
        self.requests.append(request)
        if not self.plans:
            raise AssertionError("FakeBackend received an unexpected planning call")
        value = self.plans.pop(0)
        if isinstance(value, Exception):
            raise value
        return value(request) if callable(value) else value


class OperatorTests(unittest.TestCase):
    def make_settings(self, root: Path) -> Settings:
        return Settings(
            model="test",
            ollama_url="http://localhost",
            allowed_roots=(root,),
            max_read_chars=1000,
            max_steps=10,
        )

    def make_orchestrator(self, settings: Settings, backend: FakeBackend, base: Path) -> OperatorOrchestrator:
        store = TaskStore(base / "tasks.db")
        audit = AuditLog(base / "audit.db")
        return OperatorOrchestrator(settings, backend, store=store, audit=audit)

    def test_sandbox_blocks_outside_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            with self.assertRaises(SecurityError):
                assert_allowed(root.parent / "outside.txt", (root,))

    def test_downloads_alias_resolves_to_home(self):
        expected = (Path.home() / "Downloads").resolve(strict=False)
        self.assertEqual(normalize_path("Downloads"), expected)
        self.assertEqual(normalize_path("~/Downloads"), expected)

    def test_configured_desktop_alias_is_preferred(self):
        with tempfile.TemporaryDirectory() as td:
            desktop = Path(td).resolve() / "Desktop"
            desktop.mkdir()
            self.assertEqual(resolve_allowed_path("Desktop", (desktop,)), desktop)

    def test_invented_unix_downloads_path_resolves_to_current_home(self):
        expected = (Path.home() / "Downloads").resolve(strict=False)
        self.assertEqual(normalize_path("/home/user/Downloads"), expected)

    def test_search_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "alpha.pdf").write_text("a")
            (root / "beta.txt").write_text("b")
            result = search_files(self.make_settings(root), str(root), extension="pdf")
            self.assertEqual(result["count"], 1)
            self.assertTrue(result["paths"][0].endswith("alpha.pdf"))

    def test_search_text(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            (root / "a.py").write_text("raise FetchError('boom')\n")
            result = search_text(self.make_settings(root), str(root), "FetchError")
            self.assertEqual(result["count"], 1)
            self.assertEqual(result["matches"][0]["line"], 1)

    def test_reference_resolution(self):
        self.assertEqual(resolve_refs("$steps.0.paths", [{"paths": ["a", "b"]}]), ["a", "b"])

    def test_write_requires_confirmation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            plan = {
                "summary": "create folder",
                "clarification": None,
                "steps": [{"tool": "create_folder", "args": {"path": str(root / "x")}, "reason": "test"}],
            }
            with self.assertRaises(ExecutionError):
                execute_plan(plan, self.make_settings(root), "test", confirmer=lambda _tool, _args: False,
                             audit=AuditLog(root / "audit.db"))
            self.assertFalse((root / "x").exists())

    def test_move_with_reference(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            source = root / "source"
            source.mkdir()
            (source / "a.pdf").write_text("hello")
            dest = root / "dest"
            plan = {
                "summary": "move pdf",
                "clarification": None,
                "steps": [
                    {"tool": "search_files", "args": {"path": str(source), "extension": ".pdf"}, "reason": "find"},
                    {"tool": "create_folder", "args": {"path": str(dest)}, "reason": "dest"},
                    {"tool": "move_files", "args": {"sources": "$steps.0.paths", "destination_dir": str(dest)}, "reason": "move"},
                ],
            }
            results = execute_plan(plan, self.make_settings(root), "test", confirmer=lambda _tool, _args: True,
                                   audit=AuditLog(root / "audit.db"))
            self.assertEqual(results[-1]["count"], 1)
            self.assertTrue((dest / "a.pdf").exists())

    def test_relative_child_path_resolves_under_unique_existing_parent(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            downloads, desktop, project = base / "Downloads", base / "Desktop", base / "Project"
            for item in (downloads, desktop, project):
                item.mkdir()
            (desktop / "OperatorTest").mkdir()
            resolved = resolve_allowed_path("OperatorTest/PDFs", (downloads, desktop, project))
            self.assertEqual(resolved, (desktop / "OperatorTest" / "PDFs").resolve(strict=False))

    def test_ambiguous_relative_path_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            a, b = base / "A", base / "B"
            a.mkdir(); b.mkdir(); (a / "Shared").mkdir(); (b / "Shared").mkdir()
            with self.assertRaises(SecurityError):
                resolve_allowed_path("Shared/file.txt", (a, b))

    def test_prepare_plan_rejects_wildcard_move_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bad = {
                "summary": "bad wildcard move",
                "clarification": None,
                "steps": [{"tool": "move_files", "args": {"sources": "*.pdf", "destination_dir": str(root / "PDFs")}, "reason": "bad"}],
            }
            with self.assertRaises(PlannerError):
                prepare_plan(bad, self.make_settings(root))

    def test_prepare_plan_rejects_missing_required_search_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bad = {
                "summary": "find PDFs",
                "clarification": None,
                "steps": [{"tool": "search_files", "args": {"extension": ".pdf"}, "reason": "find"}],
            }
            with self.assertRaisesRegex(PlannerError, "missing required argument.*path"):
                prepare_plan(bad, self.make_settings(root))

    def test_prepare_plan_rejects_mixed_clarification_and_steps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bad = {
                "summary": "mixed",
                "clarification": "Which folder?",
                "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "inspect"}],
            }
            with self.assertRaisesRegex(PlannerError, "either ask for clarification or contain executable steps"):
                prepare_plan(bad, self.make_settings(root))

    def test_prepare_plan_rejects_searching_destination_for_move_sources(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            dest = root / "PDFs"
            dest.mkdir()
            bad = {
                "summary": "bad",
                "clarification": None,
                "steps": [
                    {"tool": "search_files", "args": {"path": str(dest), "extension": ".pdf"}, "reason": "wrong"},
                    {"tool": "move_files", "args": {"sources": "$steps.0.paths", "destination_dir": str(dest)}, "reason": "move"},
                ],
            }
            with self.assertRaises(PlannerError):
                prepare_plan(bad, self.make_settings(root))

    def test_requirement_analyzer_requests_source_for_bulk_move(self):
        task = TaskState.create("Create a PDFs folder and move all PDF files into it")
        missing = infer_initial_requirements(task)
        self.assertEqual(missing[0].field, "source_folder")

    def test_requirement_analyzer_stops_requesting_after_context_is_known(self):
        task = TaskState.create("Create a PDFs folder and move all PDF files into it")
        task.context["source_folder"] = "/tmp/Desktop"
        self.assertEqual(infer_initial_requirements(task), [])

    def test_planner_question_maps_to_source_slot(self):
        self.assertEqual(infer_field_from_question("Where are the PDF files located?"), "source_folder")

    def test_destination_is_derived_after_source_is_known(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop = base / "Desktop"
            desktop.mkdir()
            (desktop / "OperatorTest").mkdir()
            task = TaskState.create("Create a folder called PDFs inside OperatorTest and move all PDF files into it.")
            task.context["source_folder"] = str(desktop)
            derive_context(task, Settings("test", "http://localhost", (desktop,), 1000, 10))
            self.assertEqual(task.context["destination_folder"], str(desktop / "OperatorTest" / "PDFs"))

    def test_task_store_round_trip(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks.db")
            task = store.create("hello")
            task.status = TaskStatus.WAITING_FOR_INPUT
            task.pending_field = "source_folder"
            task.pending_question = "Where?"
            task.context["x"] = 1
            store.save(task)
            loaded = store.get(task.id)
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.status, TaskStatus.WAITING_FOR_INPUT)
            self.assertEqual(loaded.context["x"], 1)

    def test_task_store_marks_inflight_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            store = TaskStore(Path(td) / "tasks.db")
            task = store.create("hello")
            task.status = TaskStatus.EXECUTING
            store.save(task)
            self.assertEqual(store.mark_inflight_interrupted(), 1)
            self.assertEqual(store.get(task.id).status, TaskStatus.INTERRUPTED)

    def test_planner_binds_orchestrator_source_into_missing_search_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            desktop = root / "Desktop"
            desktop.mkdir()
            target = desktop / "OperatorTest"
            target.mkdir()
            settings = Settings("test", "http://localhost", (desktop,), 1000, 10)
            request = (
                "USER_GOAL:\nmove PDFs\n\n"
                "ORCHESTRATOR_CONTEXT_JSON_BEGIN\n"
                + json.dumps({"known": {"source_folder": str(desktop)}})
                + "\nORCHESTRATOR_CONTEXT_JSON_END"
            )
            model_plan = {
                "summary": "organize",
                "clarification": None,
                "steps": [
                    {"tool": "search_files", "args": {"extension": ".pdf"}, "reason": "find"},
                    {"tool": "create_folder", "args": {"path": "OperatorTest/PDFs"}, "reason": "create"},
                    {"tool": "move_files", "args": {"sources": "$steps.0.paths", "destination_dir": "OperatorTest/PDFs"}, "reason": "move"},
                ],
            }
            with patch("local_operator.planner._call_ollama", return_value=json.dumps(model_plan)):
                plan = plan_with_ollama(request, settings)
            self.assertEqual(plan["steps"][0]["args"]["path"], str(desktop))
            self.assertEqual(plan["steps"][2]["args"]["destination_dir"], str(target / "PDFs"))

    def test_planner_has_one_validation_repair_attempt(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            bad = {"summary": "bad", "clarification": None, "steps": [{"tool": "search_files", "args": {}, "reason": "find"}]}
            good = {"summary": "good", "clarification": None, "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "list"}]}
            with patch("local_operator.planner._call_ollama", side_effect=[json.dumps(bad), json.dumps(good)]) as mocked:
                plan = plan_with_ollama("list files", self.make_settings(root))
            self.assertEqual(mocked.call_count, 2)
            self.assertEqual(plan["steps"][0]["tool"], "list_files")

    def test_exact_pdf_conversation_is_owned_by_orchestrator(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop, downloads, documents = base / "Desktop", base / "Downloads", base / "Documents"
            for item in (desktop, downloads, documents):
                item.mkdir()
            operator_test = desktop / "OperatorTest"
            operator_test.mkdir()
            (desktop / "one.pdf").write_text("1")
            (desktop / "two.pdf").write_text("2")
            (desktop / "note.txt").write_text("n")
            settings = Settings("test", "http://localhost", (downloads, desktop, documents), 1000, 10)

            def plan_from_context(request: str):
                self.assertIn(str(desktop), request)
                return {
                    "summary": "Organize PDFs",
                    "clarification": None,
                    "steps": [
                        {"tool": "search_files", "args": {"path": str(desktop), "extension": ".pdf"}, "reason": "find PDFs"},
                        {"tool": "create_folder", "args": {"path": str(operator_test / "PDFs")}, "reason": "create destination"},
                        {"tool": "move_files", "args": {"sources": "$steps.0.paths", "destination_dir": str(operator_test / "PDFs")}, "reason": "move PDFs"},
                    ],
                }

            backend = FakeBackend([plan_from_context])
            orchestrator = self.make_orchestrator(settings, backend, base)

            first = orchestrator.handle_message(
                "Create a folder called PDFs inside OperatorTest and move all PDF files into it.",
                confirmer=lambda _tool, _args: True,
            )
            self.assertEqual(first.kind, "clarification")
            self.assertEqual(first.task.pending_field, "source_folder")
            self.assertEqual(len(backend.requests), 0, "orchestrator should ask obvious missing input before invoking the model")

            second = orchestrator.handle_message("Desktop", confirmer=lambda _tool, _args: True)
            self.assertEqual(second.kind, "completed")
            self.assertEqual(second.task.status, TaskStatus.COMPLETED)
            self.assertEqual(len(backend.requests), 1)
            self.assertTrue((operator_test / "PDFs" / "one.pdf").exists())
            self.assertTrue((operator_test / "PDFs" / "two.pdf").exists())
            self.assertTrue((desktop / "note.txt").exists())

    def test_mixed_stale_clarification_and_steps_is_resolved_by_orchestrator(self):
        """Regression: Qwen may echo the old question while also proposing work.

        Once Desktop is durable task state, the orchestrator must own that fact,
        discard the stale clarification metadata, bind missing tool arguments,
        and compile only the executable candidate.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop, downloads, documents = base / "Desktop", base / "Downloads", base / "Documents"
            for item in (desktop, downloads, documents):
                item.mkdir()
            operator_test = desktop / "OperatorTest"
            operator_test.mkdir()
            (desktop / "one.pdf").write_text("1")
            (desktop / "two.pdf").write_text("2")
            (desktop / "note.txt").write_text("n")
            settings = Settings("test", "http://localhost", (downloads, desktop, documents), 1000, 10)

            mixed_candidate = {
                "summary": "Organize the PDFs",
                "clarification": "Which folder should I move/copy the files from?",
                "steps": [
                    {"tool": "search_files", "args": {}, "reason": "find requested files"},
                    {"tool": "create_folder", "args": {}, "reason": "create destination"},
                    {"tool": "move_files", "args": {"sources": "$steps.0.paths"}, "reason": "move files"},
                ],
            }
            backend = FakeBackend([mixed_candidate])
            orchestrator = self.make_orchestrator(settings, backend, base)

            first = orchestrator.handle_message(
                "Create a folder called PDFs inside OperatorTest and move all PDF files into it.",
                confirmer=lambda _tool, _args: True,
            )
            self.assertEqual(first.kind, "clarification")
            second = orchestrator.handle_message("Desktop", confirmer=lambda _tool, _args: True)

            self.assertEqual(second.kind, "completed")
            self.assertEqual(len(backend.requests), 1)
            self.assertEqual(second.task.context["source_folder"], str(desktop))
            self.assertEqual(second.task.context["file_extension"], ".pdf")
            self.assertEqual(second.task.context["planner_stale_clarification"], "Which folder should I move/copy the files from?")
            self.assertTrue((operator_test / "PDFs" / "one.pdf").exists())
            self.assertTrue((operator_test / "PDFs" / "two.pdf").exists())
            self.assertTrue((desktop / "note.txt").exists(), "non-PDF files must not be broadened into the move")

    def test_authoritative_task_paths_override_conflicting_model_paths(self):
        """Regression: durable source/destination must beat model-invented paths.

        Reproduces the Windows failure where the model shortened
        Desktop/OperatorTest/PDFs to Desktop/PDFs after the user had already
        resolved the source as Desktop.
        """
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop = base / "Desktop"
            downloads = base / "Downloads"
            documents = base / "Documents"
            for item in (desktop, downloads, documents):
                item.mkdir()
            operator_test = desktop / "OperatorTest"
            operator_test.mkdir()
            (desktop / "one.pdf").write_text("1")
            (desktop / "note.txt").write_text("n")
            settings = Settings("test", "http://localhost", (downloads, desktop, documents), 1000, 10)

            wrong_model_destination = desktop / "PDFs"
            candidate = {
                "summary": "Organize PDFs",
                "clarification": None,
                "steps": [
                    {"tool": "create_folder", "args": {"path": str(wrong_model_destination)}, "reason": "create destination"},
                    {"tool": "search_files", "args": {"path": str(wrong_model_destination)}, "reason": "find PDFs"},
                    {"tool": "move_files", "args": {"sources": "$steps.1.paths", "destination_dir": str(wrong_model_destination)}, "reason": "move PDFs"},
                ],
            }
            backend = FakeBackend([candidate])
            orchestrator = self.make_orchestrator(settings, backend, base)

            first = orchestrator.handle_message(
                "Create a folder called PDFs inside OperatorTest and move all PDF files into it.",
                confirmer=lambda _tool, _args: True,
            )
            self.assertEqual(first.kind, "clarification")
            second = orchestrator.handle_message("Desktop", confirmer=lambda _tool, _args: True)

            expected = operator_test / "PDFs"
            self.assertEqual(second.kind, "completed")
            self.assertEqual(second.task.plan["steps"][0]["args"]["path"], str(expected))
            self.assertEqual(second.task.plan["steps"][1]["args"]["path"], str(desktop))
            self.assertEqual(second.task.plan["steps"][1]["args"]["extension"], ".pdf")
            self.assertEqual(second.task.plan["steps"][2]["args"]["destination_dir"], str(expected))
            self.assertTrue((expected / "one.pdf").exists())
            self.assertTrue((desktop / "note.txt").exists())
            self.assertFalse(wrong_model_destination.exists(), "model-invented Desktop/PDFs must never be created")

    def test_mixed_new_clarification_discards_executable_steps(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            settings = self.make_settings(root)
            candidate = {
                "summary": "Need a target",
                "clarification": "Which destination folder should I use?",
                "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "should not run"}],
            }
            backend = FakeBackend([candidate])
            orchestrator = self.make_orchestrator(settings, backend, root)
            outcome = orchestrator.handle_message("List these files and put them somewhere", confirmer=lambda _t, _a: True)
            self.assertEqual(outcome.kind, "clarification")
            self.assertEqual(outcome.task.pending_field, "destination_folder")
            self.assertFalse(outcome.task.results)

    def test_pending_clarification_survives_orchestrator_restart(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve()
            desktop = base / "Desktop"; downloads = base / "Downloads"; documents = base / "Documents"
            for p in (desktop, downloads, documents): p.mkdir()
            (desktop / "OperatorTest").mkdir()
            settings = Settings("test", "http://localhost", (downloads, desktop, documents), 1000, 10)
            store = TaskStore(base / "tasks.db")
            audit = AuditLog(base / "audit.db")

            first_backend = FakeBackend([])
            first_orchestrator = OperatorOrchestrator(settings, first_backend, store=store, audit=audit)
            outcome = first_orchestrator.handle_message(
                "Create a folder called PDFs inside OperatorTest and move all PDF files into it.",
                confirmer=lambda _tool, _args: True,
            )
            self.assertEqual(outcome.kind, "clarification")

            plan = {
                "summary": "nothing to move",
                "clarification": None,
                "steps": [{"tool": "search_files", "args": {"path": str(desktop), "extension": ".pdf"}, "reason": "find"}],
            }
            second_backend = FakeBackend([plan])
            second_orchestrator = OperatorOrchestrator(settings, second_backend, store=TaskStore(base / "tasks.db"), audit=AuditLog(base / "audit2.db"))
            resumed = second_orchestrator.handle_message("Desktop", confirmer=lambda _tool, _args: True)
            self.assertEqual(resumed.kind, "completed")
            self.assertEqual(resumed.task.context["source_folder"], str(desktop))
            self.assertEqual(len(second_backend.requests), 1)

    def test_duplicate_planner_question_for_known_field_is_replanned_once(self):
        with tempfile.TemporaryDirectory() as td:
            base = Path(td).resolve(); desktop = base / "Desktop"; desktop.mkdir(); (desktop / "OperatorTest").mkdir()
            settings = Settings("test", "http://localhost", (desktop,), 1000, 10)
            duplicate = {"summary": "need source", "clarification": "Where are the PDF files located?", "steps": []}
            good = {"summary": "list", "clarification": None, "steps": [{"tool": "list_files", "args": {"path": str(desktop)}, "reason": "inspect"}]}
            backend = FakeBackend([duplicate, good])
            orchestrator = self.make_orchestrator(settings, backend, base)
            first = orchestrator.handle_message("Move all PDF files into OperatorTest", confirmer=lambda _t, _a: True)
            self.assertEqual(first.kind, "clarification")
            second = orchestrator.handle_message("Desktop", confirmer=lambda _t, _a: True)
            self.assertEqual(second.kind, "completed")
            self.assertEqual(len(backend.requests), 2)

    def test_read_only_failure_gets_one_safe_replan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            settings = self.make_settings(root)
            first = {"summary": "bad path", "clarification": None, "steps": [{"tool": "list_files", "args": {"path": str(root / "missing")}, "reason": "list"}]}
            second = {"summary": "recover", "clarification": None, "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "list"}]}
            backend = FakeBackend([first, second])
            orchestrator = self.make_orchestrator(settings, backend, root)
            outcome = orchestrator.handle_message("List files in this folder", confirmer=lambda _t, _a: True)
            self.assertEqual(outcome.kind, "completed")
            self.assertEqual(len(backend.requests), 2)
            self.assertEqual(outcome.task.context["execution_recovery_attempts"], 1)

    def test_failure_after_write_is_not_auto_retried(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            settings = self.make_settings(root)
            plan = {
                "summary": "partial write then fail",
                "clarification": None,
                "steps": [
                    {"tool": "create_folder", "args": {"path": str(root / "dest")}, "reason": "create"},
                    {"tool": "move_files", "args": {"sources": [str(root / "missing.pdf")], "destination_dir": str(root / "dest")}, "reason": "move"},
                ],
            }
            backend = FakeBackend([plan])
            orchestrator = self.make_orchestrator(settings, backend, root)
            with self.assertRaises(FileNotFoundError):
                orchestrator.handle_message("Do a write task", confirmer=lambda _t, _a: True)
            self.assertEqual(len(backend.requests), 1)
            task = orchestrator.store.recent(1)[0]
            self.assertEqual(task.status, TaskStatus.FAILED)
            self.assertTrue((root / "dest").is_dir())

    def test_verifier_accepts_verified_folder_creation(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve(); dest = root / "x"; dest.mkdir()
            plan = {"summary": "x", "clarification": None, "steps": [{"tool": "create_folder", "args": {"path": str(dest)}, "reason": "x"}]}
            check = verify_plan(plan, [{"created": str(dest), "exists": True}])
            self.assertTrue(check.ok)

    def test_checkpoint_callback_runs_per_step(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            seen = []
            plan = {"summary": "list", "clarification": None, "steps": [{"tool": "list_files", "args": {"path": str(root)}, "reason": "list"}]}
            execute_plan(plan, self.make_settings(root), "list", confirmer=lambda _t, _a: True,
                         audit=AuditLog(root / "audit.db"), on_step=lambda i, t, r: seen.append((i, t, r)))
            self.assertEqual(seen[0][0:2], (0, "list_files"))


if __name__ == "__main__":
    unittest.main()
