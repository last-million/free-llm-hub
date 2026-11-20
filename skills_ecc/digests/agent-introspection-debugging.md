# Agent self-debugging
For a run that keeps failing, loops, burns tokens without progress or drifts from the goal. Diagnose before retrying.
1. Capture: the error, the last successful step, the last failed command, what you were trying to do, the repeating pattern, and assumptions to verify (cwd, branch, services, expected files).
2. Diagnose by pattern:
- same command again and again: a loop; inspect the last N calls
- reasoning getting worse: bloated or duplicated context
- ECONNREFUSED or timeout: service down or wrong port
- 429: retry storm; add backoff
- file missing after write: wrong cwd or branch; recheck the path and git status
- tests still fail after a fix: wrong hypothesis; isolate the one failing test
Ask: logic, state, environment or policy failure? Transient or deterministic?
3. Recover with the smallest reversible action: stop retrying and restate the goal, trim low-signal context, re-check real file and process state, narrow to one command, file or test, observe instead of speculating; escalate to the user when blocked or risky.
4. Report: failure, root cause, recovery action, result (success, partial, blocked), follow-up.
Order: restate goal, verify world state, shrink scope, run one discriminating check, then retry. Never retry the same action three times with new wording. Never say "fixed" without evidence.
