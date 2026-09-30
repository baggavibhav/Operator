# Packaging notes

V0.3 remains a development build. It still uses external Python and Ollama because the inference/runtime design is under evaluation.

The supplied PyInstaller scripts can freeze the application shell for engineering tests, but that is not yet the intended public installer.

## Release target

### Windows

- signed per-user installer (`.exe` or `.msi`)
- bundled application runtime
- embedded/versioned local inference runtime
- model selection/download during onboarding
- start-at-login option
- system tray integration
- in-app update flow
- preserve `~/.unnamed_operator` data across upgrades

### macOS

- signed/notarized `.app` distributed through a `.dmg` or installer
- request Accessibility/Microphone permissions only when those capabilities are enabled
- embedded/versioned inference runtime
- in-app update flow

## Upgrade/data rules

- application binaries and user data remain separate
- task/audit databases require explicit schema migrations
- upgrades must never silently delete user memory/task history
- uninstall must not delete user data without opt-in
- failed upgrades must be recoverable

## Runtime/UI process split

A future release should run the orchestrator as a persistent background service and connect the desktop surface over authenticated local IPC. V0.3 deliberately keeps both in one process until task-state and execution contracts stabilize.
