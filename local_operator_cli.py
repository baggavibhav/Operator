#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from local_operator.audit import AuditLog
from local_operator.config import CONFIG_PATH, load_settings
from local_operator.executor import ExecutionError, execute_plan, terminal_confirmer
from local_operator.model_backend import OllamaBackend
from local_operator.orchestrator import OperatorOrchestrator, OrchestratorHooks
from local_operator.planner import PlannerError, ollama_available, plan_with_ollama, prepare_plan
from local_operator.task_state import TaskStore


def print_plan(plan: dict) -> None:
    print("\nPLAN")
    print("=" * 60)
    if plan.get("summary"):
        print(plan["summary"])
    if plan.get("clarification"):
        print(f"CLARIFICATION: {plan['clarification']}")
        return
    for i, step in enumerate(plan.get("steps", []), 1):
        print(f"\n{i}. {step['tool']}")
        if step.get("reason"):
            print(f"   {step['reason']}")
        print("   " + json.dumps(step.get("args", {}), default=str))


def print_results(results: list[dict]) -> None:
    print("\nRESULT")
    print("=" * 60)
    if not results:
        print("No actions were required.")
        return
    print(json.dumps(results[-1], indent=2, default=str)[:10000])


def main() -> int:
    parser = argparse.ArgumentParser(
        description="UNNAMED Local Operator V0.3.2 — orchestrated local-first filesystem agent"
    )
    parser.add_argument("request", nargs="*", help="Natural-language task")
    parser.add_argument("--model", help="Override local Ollama model")
    parser.add_argument("--plan", type=Path, help="Execute a JSON plan file instead of calling the model")
    parser.add_argument("--plan-only", action="store_true", help="Raw planner preview only; skips orchestration/execution")
    parser.add_argument("--show-config", action="store_true", help="Show current configuration")
    parser.add_argument("--history", type=int, metavar="N", help="Show N recent audited runs")
    parser.add_argument("--tasks", type=int, metavar="N", help="Show N recent durable task states")
    args = parser.parse_args()

    settings = load_settings(args.model)
    audit = AuditLog()
    store = TaskStore()

    if args.show_config:
        print(f"Config file: {CONFIG_PATH}")
        print(f"Model: {settings.model}")
        print(f"Ollama: {settings.ollama_url}")
        print("Allowed roots:")
        for root in settings.allowed_roots:
            print(f"  - {root}")
        return 0

    if args.history is not None:
        for run in audit.recent_runs(args.history):
            when = datetime.fromtimestamp(run["started_at"]).isoformat(timespec="seconds")
            print(f"#{run['id']} {when} [{run['status']}] {run['request']}")
        return 0

    if args.tasks is not None:
        for task in store.recent(args.tasks):
            when = datetime.fromtimestamp(task.updated_at).isoformat(timespec="seconds")
            print(f"{task.id[:8]} {when} [{task.status.value}] step={task.current_step} {task.goal}")
        return 0

    request_text = " ".join(args.request).strip()

    try:
        if args.plan:
            plan = prepare_plan(json.loads(args.plan.read_text(encoding="utf-8")), settings)
            if not request_text:
                request_text = f"Plan file: {args.plan}"
            print_plan(plan)
            if args.plan_only:
                return 0
            results = execute_plan(
                plan,
                settings=settings,
                request=request_text,
                confirmer=terminal_confirmer,
                audit=audit,
                model_name="plan-file",
            )
            print_results(results)
            return 0

        if not request_text:
            request_text = input("What should I do? ").strip()
        if not request_text:
            print("No task provided.")
            return 2

        if args.plan_only:
            ok, detail = ollama_available(settings)
            if not ok:
                print(f"Cannot reach Ollama at {settings.ollama_url}: {detail}")
                return 3
            plan = plan_with_ollama(request_text, settings)
            print_plan(plan)
            return 0

        backend = OllamaBackend(settings)
        orchestrator = OperatorOrchestrator(settings, backend, store=store, audit=audit)
        hooks = OrchestratorHooks(on_plan=print_plan)
        message = request_text
        while True:
            outcome = orchestrator.handle_message(message, confirmer=terminal_confirmer, hooks=hooks)
            if outcome.kind == "clarification":
                answer = input(f"{outcome.message} ").strip()
                if not answer:
                    print("Cancelled: clarification was not provided.")
                    return 2
                message = answer
                continue
            print_results(outcome.results or [])
            return 0

    except (PlannerError, ExecutionError, ValueError, FileNotFoundError, FileExistsError, PermissionError,
            json.JSONDecodeError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
