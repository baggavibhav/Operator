# Changelog

## 0.3.2

- Made resolved orchestrator path slots authoritative during plan compilation.
- Fixed a regression where a small model could shorten a nested destination such as `Desktop/OperatorTest/PDFs` to `Desktop/PDFs`.
- Source discovery now always uses the resolved `source_folder` when it feeds a move/copy operation.
- Search extension now always preserves the orchestrator's deterministic file-type constraint (for example `.pdf`).
- Destination creation and move/copy steps now always use the resolved `destination_folder`.
- Added an end-to-end regression test reproducing the Windows `Desktop\PDFs` failure.

## 0.3.1

Control-plane correction discovered during the first real Windows/Qwen field test.

- moved lifecycle semantics out of the Ollama backend; it now returns raw model candidates
- made the orchestrator resolve mixed clarification + executable-step responses
- stale clarification for an already-known task field no longer aborts a valid executable candidate
- genuinely new clarification safely discards speculative executable steps and waits for the user
- durable task fields are rebound into underspecified tool arguments before plan compilation
- PDF intent is persisted as a `.pdf` task constraint so a small model cannot accidentally broaden the move to all files
- kept bounded re-proposal and no-auto-retry-after-write safety rules
- added exact regression coverage for the Windows failure observed after replying `Desktop`

## 0.3.0

Architectural reset from prompt-centric execution to a persistent orchestrator.

- added durable SQLite task state / checkpoint database
- added explicit task lifecycle states
- moved clarification ownership out of the desktop UI
- added deterministic missing-input manager
- added structured orchestrator context for planning
- added per-step persistent checkpoints
- added write-result verification
- added bounded read-only recovery and no-retry-after-write policy
- added crash/interruption marking instead of automatic write replay
- made audit SQLite connection safe for desktop worker-thread use
- added task state display in desktop chat
- added right-click orb menu: Open / Settings / Quit
- added recent task inspection to the CLI
- removed obsolete V0.2 clarification-prompt concatenation module
- expanded regression suite around real Windows failures

## 0.2.x

Desktop prototype, path sandboxing, tool contracts, clarification experiments, local Ollama planning, approval dialogs, audit log, and initial Windows/macOS shell.
