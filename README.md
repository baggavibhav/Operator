# Operator

**A local-first AI desktop agent for safe, privacy-conscious computer automation.**

> **Alpha / active R&D.** Operator is an experimental project under active development. Capabilities, interfaces, installation steps, and security boundaries may change. It is not yet intended for production use.

Operator explores a simple question:

> **How much useful personal-agent autonomy can be achieved on consumer hardware while keeping execution local, resource use low, and consequential actions under explicit user control?**

Instead of giving a language model unrestricted access to the computer, Operator separates reasoning from execution. A small local model interprets requests and proposes bounded actions; a deterministic orchestrator owns task state, validation, approvals, execution, verification, and recovery.

## Why Operator?

Many desktop-agent designs place a powerful model close to the execution layer. Operator takes the opposite approach:

- **Local-first:** development inference runs locally through Ollama.
- **Small-model oriented:** the current development model is Phi-4 Mini.
- **Deterministic execution:** models propose actions; validated tools perform them.
- **Persistent orchestration:** tasks, clarifications, checkpoints, and results survive beyond individual chat messages.
- **Least privilege:** ordinary conversation has no filesystem, desktop, web, shell, or app access.
- **Human control:** write operations require explicit approval.
- **Cross-platform:** the desktop prototype targets Windows and macOS.
- **Research-driven:** reliability, resource use, intervention rate, latency, and task success are treated as measurable engineering questions.

## Architecture

```text
                       User
                        │
                        ▼
              Desktop companion / chat
                        │
                        ▼
              Positive-intent router
              ┌─────────┼─────────┐
              │         │         │
              ▼         ▼         ▼
       Conversation   Web      Computer
        no tools    read-only    tasks
              │         │         │
              │         │         ▼
              │         │   Persistent orchestrator
              │         │   ├─ task state / SQLite
              │         │   ├─ clarification manager
              │         │   ├─ planner adapter
              │         │   ├─ validation + sandbox
              │         │   ├─ approval boundary
              │         │   ├─ deterministic executor
              │         │   ├─ checkpoints
              │         │   └─ verifier / recovery
              │         │
              ▼         ▼
          Local LLM   Read-only web tools
```

The key boundary is deliberate:

```text
LLM reasoning ≠ execution authority
```

The model can propose what should happen. It does not directly receive arbitrary Python, shell, filesystem, or operating-system access.

## Current V0.5 development scope

### Local conversation

Normal conversational requests are routed to a tool-free local model path. In this mode Operator has **no access** to the filesystem, desktop context, web, shell, applications, or private computer data.

Tool access requires positive, deterministic intent detection rather than a model deciding to grant itself capabilities.

### Local computer tasks

The current deterministic filesystem toolset supports:

- listing and searching files
- searching text files
- finding large files
- reading supported text-like files
- creating folders
- moving and copying files
- renaming files
- grounding requests against supported desktop/file context

Filesystem paths remain constrained to configured allowed roots. Write-capable operations require approval before execution.

### Read-only web research

V0.5 includes an experimental read-only web-agent path. The local reasoning model can request bounded web search/open operations and synthesize an answer from observed content.

Webpage content is treated as untrusted observation rather than executable instruction. The current web path is intentionally read-only; it does not provide browser interaction, form submission, purchasing, authentication actions, or arbitrary downloads.

### Persistent tasks

The orchestrator stores durable task state in SQLite, including:

- original goal
- known/resolved context
- missing information
- pending clarification
- candidate plan
- completed step prefix
- current step
- status and errors

Clarification answers are bound to the existing task rather than treated as unrelated chat messages.

## Safety model

Operator currently enforces several boundaries:

- explicit tool allowlisting
- sandboxed filesystem roots
- tool argument validation
- positive-intent gating before privileged local access
- tool-free ordinary conversation
- explicit approval for filesystem writes
- no delete capability
- no arbitrary shell execution
- local SQLite audit trail
- per-step checkpoints
- write-result verification
- bounded recovery for eligible pre-write/read-only failures
- no automatic retry after a write
- no automatic replay of interrupted write tasks after restart

This is **not a complete security boundary**. The project still requires additional adversarial testing, stronger OS-level isolation, packaging security, dependency/SBOM work, and broader fuzz/regression coverage before it should be treated as production software.

See [SECURITY.md](SECURITY.md) for the current security model.

## Desktop experience

The prototype includes a lightweight desktop companion built with PySide6:

- floating companion/orb
- compact chat surface
- system tray integration
- global shortcut
- task-state feedback
- approval prompts
- cancellation / stop control
- state-driven visual feedback

The UI is intentionally separate from task semantics: the orchestrator, not the interface, owns the task lifecycle.

## Development setup

### Requirements

The current development build requires:

- Python 3.11+
- Ollama for model-backed conversation/planning/research
- the Phi-4 Mini development model
- Windows or macOS for desktop field testing

The bootstrap scripts create and use a project-local `.venv`.

### Windows

From PowerShell in the repository root:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\bootstrap_windows.ps1
```

The bootstrap installs/synchronizes desktop dependencies, checks the local development model, and starts Operator.

### macOS

```bash
./scripts/bootstrap_macos.sh
```

> **Known alpha issue:** the current macOS development bootstrap has a recurring Qt `cocoa` platform-plugin issue on at least one field machine. This is being tracked and does not affect the Windows development path. macOS permissions may also be required for global shortcut/input monitoring behavior.

## Developer CLI

Run a local request:

```bash
python local_operator_cli.py "Show me the five largest files in Downloads"
```

Inspect recent durable tasks:

```bash
python local_operator_cli.py --tasks 10
```

Inspect recent execution audit entries:

```bash
python local_operator_cli.py --history 10
```

## Local data

Runtime state is stored locally under:

```text
~/.unnamed_operator/
```

Current state includes:

```text
config.json    local configuration / allowed roots
tasks.db       persistent task and checkpoint state
audit.db       execution/action audit trail
desktop.log    desktop application log
```

## Testing and CI

GitHub Actions runs the core test suite and source compilation across:

| OS | Python |
| --- | --- |
| Windows | 3.11, 3.12 |
| macOS | 3.11, 3.12 |
| Ubuntu | 3.11, 3.12 |

CI is supplemented by real-device field testing because GUI, permissions, platform plugins, and operating-system integration cannot be fully validated by unit tests alone.

## Research direction

The project is investigating whether a small local model paired with a deterministic control plane can approach the reliability of larger agents on common desktop workflows while using substantially fewer resources and keeping sensitive execution local.

Measurements planned/under collection include:

- task success rate
- clarification and repeated-clarification rate
- planner repair/recovery count
- end-to-end and model latency
- tool-call count
- user approvals/interventions
- path/tool hallucination rate
- RAM / CPU / disk footprint

See [RESEARCH.md](RESEARCH.md) and [ARCHITECTURE.md](ARCHITECTURE.md) for the evolving experiment and design.

## Current limitations

Operator is an alpha research prototype. It currently does **not** provide:

- arbitrary shell access
- file deletion
- generic autonomous desktop control
- authenticated website actions
- email sending
- purchasing
- a production installer or auto-updater
- a fully isolated background runtime/IPC boundary
- production security guarantees

The development build still depends on Python and Ollama. The long-term release target is a signed application with a managed local runtime/model and a conventional installer/update experience.

## Roadmap

Near-term work is focused on:

1. hardening the V0.5 capability router and agent guardrails
2. completing Windows and macOS field validation
3. fixing the macOS Qt bootstrap issue
4. expanding adversarial and regression testing
5. measuring task reliability and resource usage
6. preparing a reproducible alpha release
7. later separating the persistent runtime from the desktop UI through authenticated local IPC

Features in this roadmap are plans, not claims about the current build.

## Project status

Operator is being developed in public as an experimental system, not presented as finished software. Issues, architecture changes, and failed experiments are expected during the alpha phase.

For version history, see [CHANGELOG.md](CHANGELOG.md).
