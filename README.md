# UNNAMED Local Operator V0.3.2

A local-first personal computer operator experiment for consumer machines.

V0.3 is an architectural reset: the language model is no longer the task controller. A persistent **orchestrator** owns task state, clarification, planning, execution, approvals, checkpoints, verification, and bounded recovery.

## What changed in V0.3

```text
User / desktop UI
        │
        ▼
  Orchestrator
  ├─ durable task state (SQLite)
  ├─ requirement / clarification manager
  ├─ task-context grounding
  ├─ planner invocation
  ├─ execution controller
  ├─ approval boundary
  ├─ per-step checkpoints
  ├─ verifier
  └─ bounded recovery policy
        │
        ▼
  Validated tool plan
        │
        ▼
 Deterministic filesystem tools
```

Key behavior:

- clarification answers belong to a durable task field, not to a reconstructed chat prompt
- waiting tasks survive app restarts
- obvious missing information can be requested before the model is invoked
- planner context is passed as structured JSON
- model output is treated as an untrusted candidate; the orchestrator resolves clarification-vs-execution conflicts
- known task constraints (for example `source_folder` and requested `.pdf` file type) are rebound deterministically before compilation
- required tool arguments are validated before execution
- filesystem paths remain sandboxed to configured roots
- every write requires explicit approval
- each successful step is checkpointed to SQLite
- filesystem write results are verified after execution
- read-only failures may receive one bounded replan
- tasks are **never automatically retried after a write has occurred**
- in-flight tasks are marked interrupted after an unexpected process restart rather than silently replayed
- the chat UI displays the current durable task state
- right-clicking the orb now exposes Open / Settings / Quit

## Current capability scope

V0.3 supports:

- list/search files
- search text files
- find largest files
- inspect/read text-like files
- create folders
- move/copy files
- rename files

V0.3 intentionally has **no delete, arbitrary shell, browser control, email sending, purchasing, or generic desktop control**.

## Windows development test

Extract the ZIP, open PowerShell inside the extracted folder, and run:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
```

The development bootstrap creates a local `.venv`, installs the GUI dependencies if required, checks the local Ollama model, and starts the desktop app.

Then retry the field-test workflow:

> Create a folder called PDFs inside OperatorTest and move all PDF files into it.

If the source was not specified, the orchestrator should ask:

> Which folder should I move/copy the files from?

Reply:

> Desktop

That answer is persisted as `source_folder` on the existing task. It is not treated as a new task and the original goal is not rebuilt from chat text.

## macOS development test

```bash
./scripts/bootstrap_macos.sh
```

The global shortcut may require Accessibility/Input Monitoring permission depending on macOS policy.

## Developer CLI

```bash
python local_operator_cli.py "Show me the five largest files in Downloads"
```

Recent durable tasks:

```bash
python local_operator_cli.py --tasks 10
```

Recent execution audit entries:

```bash
python local_operator_cli.py --history 10
```

## Local data

Runtime state is stored under:

```text
~/.unnamed_operator/
```

including:

- `config.json` — local configuration / allowed roots
- `tasks.db` — persistent task/checkpoint state
- `audit.db` — execution/action audit trail
- `desktop.log` — desktop application log

## Development runtime vs release target

V0.3 is still a developer build and uses Python + Ollama. This is not the intended public installation experience.

The release target remains:

```text
Download installer
→ install
→ approve OS permissions
→ local model/runtime handled automatically
→ assistant appears
→ updates handled by the app
```

Windows should eventually ship as a signed installer; macOS as a signed/notarized app/DMG. The final release should not require users to install Python, pip, or Ollama manually.

## Important architecture boundary

The desktop UI and orchestrator are now logically separated, but V0.3 still hosts them in the same application process and uses a background worker thread + Qt signals. A true background runtime / local IPC process boundary is deliberately deferred until the orchestration contract is stable; adding a broker or daemon before that would create complexity without improving this experiment yet.

See `ARCHITECTURE.md`, `SECURITY.md`, `RESEARCH.md`, and `CHANGELOG.md`.
