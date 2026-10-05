from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from local_operator.audit import AuditLog
from local_operator.config import Settings
from local_operator.model_backend import _normalize_agent_decision, _valid_agent_decision
from local_operator.orchestrator import OperatorOrchestrator
from local_operator.task_state import TaskStatus, TaskStore
from operator_desktop.formatting import format_result


class AgentBackend:
    name = "fake-agent"

    def __init__(self, turns): self.turns = list(turns)
    def available(self): return True, "ok"
    def propose(self, request: str): raise AssertionError("explicit web research should not use the one-shot planner")
    def agent_turn(self, goal: str, observations):
        if not self.turns: raise AssertionError("unexpected agent turn")
        value = self.turns.pop(0); return value(goal, observations) if callable(value) else value


class AgentLoopTests(unittest.TestCase):
    def settings(self, root: Path) -> Settings: return Settings("phi4-mini", "http://localhost", (root,), 12000, 8, True)

    def test_web_research_searches_reads_then_synthesizes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            backend = AgentBackend([
                {"type":"tool","tool":"web_search","args":{"query":"latest NVIDIA AI announcement","limit":5},"reason":"find it"},
                lambda _goal, obs: {"type":"tool","tool":"web_open","args":{"url":obs[0]["result"]["results"][0]["url"],"max_chars":12000},"reason":"read primary result"},
                {"type":"final","answer":"NVIDIA announced a new AI platform.","suggestions":["Compare this with AMD's latest AI announcement.","Save this summary locally."]},
            ])
            orchestrator = OperatorOrchestrator(self.settings(root), backend, store=TaskStore(root/"tasks.db"), audit=AuditLog(root/"audit.db"))
            search_result={"query":"latest NVIDIA AI announcement","count":1,"results":[{"title":"NVIDIA News","url":"https://example.com/nvidia","snippet":"AI platform"}]}
            open_result={"url":"https://example.com/nvidia","title":"NVIDIA News","content":"NVIDIA announced a new AI platform.","chars":36,"truncated":False,"links":[]}
            with patch.dict("local_operator.orchestrator.TOOL_FUNCTIONS",{"web_search":lambda _settings,**_args:search_result,"web_open":lambda _settings,**_args:open_result},clear=False):
                outcome=orchestrator.handle_message("Search the web for the latest NVIDIA AI announcement and summarize the top result",confirmer=lambda _tool,_args:True)
            self.assertEqual(outcome.kind,"completed"); self.assertEqual(outcome.task.status,TaskStatus.COMPLETED); self.assertEqual(outcome.task.context["plan_source"],"bounded_web_agent")
            self.assertIn("NVIDIA announced",outcome.results[-1]["answer"]); self.assertEqual(len(outcome.results[-1]["suggestions"]),2)

    def test_agent_blocks_unobserved_hallucinated_url(self):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td).resolve(); backend=AgentBackend([
                {"type":"tool","tool":"web_search","args":{"query":"example","limit":5},"reason":"find"},
                {"type":"tool","tool":"web_open","args":{"url":"https://hallucinated.example/page"},"reason":"read"},
            ])
            orchestrator=OperatorOrchestrator(self.settings(root),backend,store=TaskStore(root/"tasks.db"),audit=AuditLog(root/"audit.db"))
            with patch.dict("local_operator.orchestrator.TOOL_FUNCTIONS",{"web_search":lambda _settings,**_args:{"query":"example","count":1,"results":[{"title":"Safe","url":"https://example.com/safe","snippet":""}]}},clear=False):
                with self.assertRaisesRegex(RuntimeError,"not present in prior observations"):
                    orchestrator.handle_message("Search the web for example and summarize it",confirmer=lambda _t,_a:True)

    def test_schema_drift_normalizes_search_action(self):
        decision=_normalize_agent_decision({"action":"search","query":"latest NVIDIA AI","limit":5,"reason":"find news"})
        self.assertEqual(decision["type"],"tool"); self.assertEqual(decision["tool"],"web_search"); self.assertEqual(decision["args"]["query"],"latest NVIDIA AI")
        self.assertTrue(_valid_agent_decision(decision))

    def test_schema_drift_normalizes_final_summary(self):
        decision=_normalize_agent_decision({"action":"done","summary":"Grounded summary.","suggestions":[]})
        self.assertEqual(decision["type"],"final"); self.assertEqual(decision["answer"],"Grounded summary."); self.assertTrue(_valid_agent_decision(decision))

    def test_invalid_decision_is_not_accepted(self):
        self.assertFalse(_valid_agent_decision(_normalize_agent_decision({"action":"dance","value":"nope"})))

    def test_synthesized_answer_formats_with_followups(self):
        text=format_result({"answer":"Short summary.","suggestions":["Save this summary locally.","Compare it with AMD."]})
        self.assertIn("Short summary.",text); self.assertIn("You can continue with:",text); self.assertIn("Save this summary locally.",text)


if __name__ == "__main__": unittest.main()
