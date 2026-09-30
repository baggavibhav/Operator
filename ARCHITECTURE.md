# UNNAMED Local Operator V0.3.2 — Architecture

## Design principle

The LLM is a planner, not the operating system controller.

```text
                              ┌─────────────────┐
                              │      User       │
                              └────────┬────────┘
                                       │
                                       ▼
                              ┌─────────────────┐
                              │ Desktop Surface │
                              │ orb/chat/tray   │
                              └────────┬────────┘
                                       │ message
                                       ▼
                         ┌──────────────────────────┐
                         │       ORCHESTRATOR       │
                         │ owns the task lifecycle │
                         └────────────┬─────────────┘
                                      │
             ┌────────────────────────┼────────────────────────┐
             ▼                        ▼                        ▼
     Requirement manager       Persistent task state       Planner adapter
     missing inputs / slots       SQLite checkpoints          local model
             │                        │                        │
             └────────────────────────┼────────────────────────┘
                                      ▼
                              Validated candidate plan
                                      │
                        ┌─────────────┴─────────────┐
                        ▼                           ▼
                 Security / policy            Approval gate
                 allowlist + sandbox          user for writes
                        └─────────────┬─────────────┘
                                      ▼
                             Deterministic executor
                                      │
                              per-step checkpoint
                                      │
                                      ▼
                                  Verifier
                                      │
                       success ───────┴────── failure
                          │                      │
                       complete          bounded recovery
                                         (read-only only)
```

## Core modules

### `local_operator/orchestrator.py`
Control plane. Owns state transitions, requirement resolution, planning calls, execution, checkpointing, verification, and recovery policy.

### `local_operator/task_state.py`
Durable task state in SQLite. Stores:

- task ID and original goal
- lifecycle status
- known context / resolved fields
- pending field/question
- plan
- completed result prefix
- current step
- last error
- revision / timestamps

SQLite WAL mode and a schema-version metadata record are used. Mid-flight tasks are marked `interrupted` on a new runtime start; writes are not replayed automatically.

### `local_operator/requirements.py`
Deterministic intake/context layer. It asks for obvious missing information before invoking the model and binds a user's answer to a named task field. Path-like answers are grounded through the filesystem sandbox.

This layer can also derive narrow deterministic facts from an existing goal. Example: once `Desktop` is known as the source, `folder called PDFs inside OperatorTest` can resolve to an allowed `OperatorTest/PDFs` destination when the parent can be uniquely grounded.

### `local_operator/planner.py`
Model adapter + deterministic plan compiler. The local model receives the original user goal plus a structured `ORCHESTRATOR_CONTEXT_JSON` block and returns a **candidate**, not an executable authority. The orchestrator then binds durable task fields into that candidate and asks the compiler to enforce:

- tool allowlist
- required/unknown argument contracts
- step count
- reference ordering
- no wildcard move/copy sources
- sandboxed static paths
- obvious source/destination contradictions

If a small model emits a clarification and executable steps together, the orchestrator resolves that conflict from durable task state. A clarification for an already-known field is treated as stale metadata; a genuinely new clarification pauses the task and discards the proposed executable steps. Invalid executable candidates receive at most one bounded re-proposal before execution.

### `local_operator/executor.py`
Runs validated tools only. It resolves step references, asks for approval before writes, records every action in the audit log, and emits a checkpoint callback after each successful step.

### `local_operator/verifier.py`
Checks observable filesystem postconditions for write operations: created folder exists, moved/copied files exist at result paths, renamed file exists.

### `local_operator/security.py`
Capability/path boundary. Only explicitly allowlisted tools run. Filesystem access must remain under configured roots.

### `operator_desktop/`
Presentation only. It does not own task semantics anymore. The desktop shell sends messages to `OperatorWorker`, displays task snapshots, asks user approvals, and renders results.

## Task lifecycle

```text
NEW
 │
 ├── missing information ──> WAITING_FOR_INPUT
 │                              │
 │                         user answer
 │                              ▼
 └──────────────────────────── READY
                                │
                                ▼
                            PLANNING
                                │
                         candidate plan
                                ▼
                              READY
                                │
                                ▼
                           EXECUTING
                                │
                      checkpoint each step
                                ▼
                           VERIFYING
                           /       \
                          /         \
                   COMPLETED       FAILED
```

If the process stops while `PLANNING`, `EXECUTING`, or `VERIFYING`, the next runtime marks the task `INTERRUPTED`. It does not replay a possibly partially-completed write task.

## Recovery policy

V0.3.2 uses deliberately bounded recovery:

- stale clarification for an already-known field: orchestrator resolves it from durable state
- malformed/invalid executable candidate: one orchestrator-requested re-proposal before execution
- execution failure before any write: one orchestrator replan is allowed
- execution failure after any write: **no automatic retry**
- process restart mid-write: **no automatic resume**

This keeps autonomy subordinate to deterministic safety.

## Process boundary

V0.3 separates UI and agent responsibilities in code but not yet at the OS-process level. The desktop worker currently hosts the orchestrator in a background thread and communicates with the Qt UI using signals.

A later version can move the runtime into a background service/daemon and replace the in-process boundary with authenticated local IPC (named pipe / Unix-domain socket / equivalent). MQTT/Redis are not justified for a single-device prototype at this stage.

## Model boundary

`ModelBackend` keeps candidate generation backend-independent. Ollama + `qwen2.5:1.5b` remains the development backend. The backend cannot transition task state or authorize execution; those decisions remain in the orchestrator. Public packaging should eventually replace the external Ollama requirement with an embedded inference runtime and managed model downloads.
