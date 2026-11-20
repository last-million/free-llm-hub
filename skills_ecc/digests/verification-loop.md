# Verification loop
Before saying a task is done (and before a PR), run every phase and report real results:
1. Build - if it fails, stop and fix first.
2. Types - tsc --noEmit or pyright; fix the errors that matter.
3. Lint - the project's linter.
4. Tests - full suite with coverage; report total, passed, failed and coverage %, target 80%+.
5. Leftovers - grep for hardcoded keys (sk-, api_key) and stray console.log or print.
6. Diff - `git diff --stat`; review each changed file for unintended changes, missing error handling, edge cases.
Use the project's own commands (check package.json or pyproject).
End with a report:
VERIFICATION REPORT - Build, Types, Lint, Tests (X/Y, Z%), Security (N issues), Diff (N files) -> READY or NOT READY, then the issues to fix.
On long tasks re-run after each function, component or major change, not only at the end.
Never mark a phase PASS that you did not run.
