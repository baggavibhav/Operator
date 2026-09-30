# R-004 — Local-First Personal Operator

## Research question

How much useful personal-agent autonomy can be achieved on consumer hardware while keeping execution local, resource use low, and consequential actions under explicit user control?

## V0.3 milestone: orchestration under constraints

The early field tests showed that the central reliability problem was not only model quality. The prototype allowed a small local planner to implicitly own too much conversational/task state.

Observed failures included:

1. Python module namespace collision (`operator.py`)
2. OS path grounding (`Downloads` treated as process-relative)
3. cross-OS path hallucination (`/home/user/Downloads` on Windows)
4. multi-step source/destination context loss
5. source ambiguity
6. invalid wildcard execution plans
7. logically identical source/destination plans
8. clarification answer not bound to the pending task
9. required tool argument (`search_files.path`) dropped after clarification
10. mixed clarification + execution planner state

V0.3 tests a different hypothesis:

> A small local model becomes substantially more useful when a deterministic orchestrator owns task lifecycle/state and uses the model only for bounded planning decisions.

## Experimental architecture

The orchestrator now owns:

- task identity
- missing-input state
- known context
- planning invocation
- execution lifecycle
- approvals
- step checkpoints
- verification
- bounded recovery

The model owns:

- language interpretation that is not deterministically derivable
- candidate tool selection
- candidate dependency ordering

The model does **not** own the task state machine.

## Measurements to collect on the Windows field machine

- task success/failure
- clarification count
- repeated clarification rate
- planner repair count
- execution recovery count
- model planning latency
- end-to-end latency
- tool count
- approval count
- user interventions
- path/tool hallucination rate
- RAM / CPU use
- model size / disk footprint

## Immediate regression task

```text
Create a folder called PDFs inside OperatorTest and move all PDF files into it.
```

Expected behavior:

```text
orchestrator detects missing source
→ asks source once
→ user answers Desktop
→ source_folder persists in task DB
→ destination can be grounded from original goal if unambiguous
→ planner receives structured task context
→ validated plan
→ write approvals
→ execution checkpoints
→ verification
→ completed
```

The next useful research comparison is whether this architecture allows a 1.5B model to achieve reliability closer to a larger planner on the same deterministic task suite.
