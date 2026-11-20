# TDD workflow
Failing test first, then the least code that passes, then refactor.
0. Find the project's real test command first (package.json scripts.test, lockfile, config). The package manager and the test runner are different things; never assume `npm test` (`bun test` is not `bun run test`).
1. Turn the request into user journeys, then test cases: happy path, empty or invalid input, boundaries, error and fallback paths.
2. Run the tests and see them FAIL for the right reason (red).
3. Write the smallest code that passes (green). Run again.
4. Refactor with tests green: remove duplication, improve names.
5. Run coverage; target 80%+ across unit, integration and E2E.
Test types: unit (functions, component logic), integration (API routes, database, services), E2E (critical user flows).
Rules:
- Test behavior the user sees, not internal state.
- Semantic selectors (role, text, data-testid), never CSS classes.
- Every test independent: own setup, no order dependence.
- Mock external services (database, cache, third-party APIs) so tests are fast and deterministic.
- Name tests by behavior ("returns empty list for empty query").
Do not report done until the full test command passes; show its output.
