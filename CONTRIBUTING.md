# Contributing

Keep this small: a local macOS wrapper around the supported Hermes update command. Avoid agent frameworks, cloud schedulers, package-manager reimplementations, or dependencies for ordinary file/process handling.

Use Python 3.9-compatible syntax and the standard library. Run `python3 -m unittest discover -s tests -v`. Add behavioral tests for lifecycle, scheduling, concurrency, reporting, or recovery changes. Tests must use temporary repositories and synthetic process fixtures, never real Hermes sessions or credentials.

Keep these invariants:

- No force-quit of Hermes Desktop, automatic branch switching, or cleanup of user source edits.
- Restore an originally open app after the update attempt; retain interrupted recovery intent until readiness is observed.
- Require a fresh matching native receipt and installed revision for success. A process exit alone is insufficient.
- Never transform a skipped/failed run into an “updated” notification. Keep diagnostics out of notifications.
- One scheduled attempt per local day and one active wrapper. No notification replay loop.
- Installed runtime changes require an explicit reinstall; preserve configuration on replacement.
- No secrets, user-specific configuration, raw update logs, receipts, or full-desktop screenshots in commits.

When Hermes changes its command flags, receipt schema, app layout, or health endpoint, check the official source and document compatibility. Native app reopening and actual banner visibility need macOS smoke verification in addition to unit tests.
