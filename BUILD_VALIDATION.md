# V0.3.2 build validation

Validated in the build environment:

- Python syntax compilation for orchestrator/planner/model backend/requirements
- 39/39 automated tests passing
- exact regression: user asks to create `OperatorTest/PDFs` and move PDFs; orchestrator asks for source; user replies `Desktop`; model returns **both** stale clarification metadata and executable steps with missing path/destination arguments; orchestrator resolves the stale clarification, binds `Desktop`, `.pdf`, and destination state, executes, and leaves non-PDF files untouched
- genuinely new mixed clarification is converted to `WAITING_FOR_INPUT` and speculative steps are not executed
- previous restart/checkpoint/no-retry-after-write tests remain passing

Not validated here:

- native Windows GUI launch
- Windows tray/hotkey behavior
- live Qwen/Ollama output on the user's machine

Those remain field-integration tests on Windows.

## V0.3.2 regression

- 39/39 automated tests pass.
- Added exact regression coverage proving a planner-proposed `Desktop/PDFs` path is overridden by durable task state `Desktop/OperatorTest/PDFs`.
- Verified the source search remains `Desktop`, the extension remains `.pdf`, no stray `Desktop/PDFs` directory is created, and the PDF is moved into the nested destination.
