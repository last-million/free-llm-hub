# Coding standards
Readability first: clear names, small functions, self-documenting code; comment the why, not the what. KISS, DRY (extract repeated logic), YAGNI (no speculative features).
- Names: descriptive nouns for variables, verb phrases for functions (fetchMarketData, isValidEmail).
- Immutability (critical): copy with spread, never mutate shared data (no items.push, no obj.x = ...).
- Errors: wrap risky async work in try/catch, check response.ok, log the cause, throw a clear message; never swallow errors.
- Async: run independent awaits in parallel with Promise.all.
- Types: no `any`; interfaces for data shapes.
- React: function components with typed props; extract custom hooks; functional state updates (setCount(c => c + 1)).
- API: REST resource URLs, one response envelope, validate input.
- Performance: memoize costly work, lazy-load heavy components, select only needed columns, never query inside a loop.
- Tests: arrange-act-assert; name by behavior.
- Smells to fix: functions over ~50 lines (split), nesting deeper than ~3 levels (early returns), magic numbers (named constants).
- Files: small and focused, PascalCase components, camelCase utilities, grouped by feature.
