from __future__ import annotations

import ast
import unittest
from pathlib import Path

from operator_desktop.formatting import format_result
from operator_desktop.platforms import current_platform
from operator_desktop.state import AgentState, presentation_for


class DesktopSupportTests(unittest.TestCase):
    def test_state_presentations_are_complete(self):
        for state in AgentState:
            presentation = presentation_for(state)
            self.assertTrue(presentation.label)
            self.assertTrue(presentation.symbol)

    def test_waiting_for_input_state_exists(self):
        self.assertEqual(AgentState.WAITING_FOR_INPUT.value, "waiting_for_input")

    def test_platform_profile_has_hotkey(self):
        profile = current_platform()
        self.assertTrue(profile.name)
        self.assertTrue(profile.default_hotkey)
        self.assertTrue(profile.display_hotkey)

    def test_format_result_for_files(self):
        text = format_result({"files": [{"name": "a.bin", "size_bytes": 1024}]})
        self.assertIn("a.bin", text)
        self.assertIn("KB", text)

    def test_desktop_app_no_longer_owns_clarification_state(self):
        app_path = Path(__file__).resolve().parents[1] / "operator_desktop" / "app.py"
        source = app_path.read_text(encoding="utf-8")
        self.assertNotIn("_pending_clarification", source)
        self.assertNotIn("apply_clarification", source)

    def test_desktop_worker_imports_orchestrator(self):
        path = Path(__file__).resolve().parents[1] / "operator_desktop" / "worker.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in tree.body:
            if isinstance(node, ast.ImportFrom):
                imports.append((node.module, [alias.name for alias in node.names]))
        self.assertTrue(any(module == "local_operator.orchestrator" and "OperatorOrchestrator" in names for module, names in imports))


if __name__ == "__main__":
    unittest.main()
