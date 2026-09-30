# Security model — V0.3

V0.3 follows least privilege and treats model output as untrusted input.

## Enforced today

1. Only explicitly allowlisted filesystem tools exist.
2. Every filesystem path is resolved inside configured allowed roots.
3. Required/unknown tool arguments are validated before execution.
4. Wildcards are forbidden as direct move/copy sources.
5. Every write-capable tool requires an explicit interactive approval.
6. No delete capability exists.
7. No arbitrary shell execution exists.
8. No browser, email, purchase, or generic desktop-control capability exists.
9. Every tool execution is recorded to a local SQLite audit log.
10. Durable task state is separate from the chat UI.
11. Successful execution steps are checkpointed.
12. Write postconditions are verified.
13. A failed task is never automatically retried after a write has occurred.
14. Mid-flight tasks are not automatically resumed after a process restart.

## Trust boundaries

```text
Untrusted:
- natural-language request
- local model output
- model-proposed paths / arguments

Trusted control plane:
- orchestrator state machine
- tool schema validation
- path sandbox
- risk classification
- approval decision
- deterministic tool implementations
- verifier
```

The local model can suggest what to do; it cannot directly access Python functions, arbitrary OS APIs, shell commands, or the filesystem.

## Clarification safety

A clarification is durable task state, not free-form prompt concatenation. Path answers such as `Desktop` are resolved through the sandbox before being stored as `source_folder`/`destination_folder`. If they cannot be safely grounded, execution does not continue.

## Crash / replay safety

The task database records the completed step prefix. On restart, tasks that were mid-flight are marked `interrupted`. V0.3 refuses to automatically replay them because the OS side effect may have succeeded immediately before the crash even if the checkpoint did not.

## Not yet a complete security boundary

Before public release the project still needs:

- signed/notarized binaries and update packages
- dependency/SBOM scanning
- stronger OS sandboxing
- a formal capability-token model as tools expand
- path traversal/property fuzzing
- secure credential storage if connected services are added
- authenticated local IPC when runtime/UI become separate processes
- adversarial prompt/tool-use testing
- installer/uninstaller/upgrade security tests
