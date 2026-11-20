# Frontend patterns (React / Next.js)
- Composition over inheritance: small components with typed props; compound components share state through context; render props for reusable behavior.
- Custom hooks: useToggle, useDebounce(value, delay), a fetch hook returning {data, loading, error} with cancellation and cleanup.
- State: local state first; Context + useReducer for shared state; functional updates; compute derived values instead of storing them.
- Performance: useMemo for costly derivations, useCallback for handlers passed to memoized children, React.memo sparingly, lazy() + Suspense for heavy components and routes, virtualize long lists.
- Forms: controlled inputs, schema validation (zod), field-level errors, disable submit while pending.
- Error boundaries around risky regions, with a recoverable fallback.
- Accessibility: semantic elements, labels on inputs, full keyboard use (arrows, Enter, Escape), manage focus (trap in modals, restore on close), ARIA only where HTML has no role, visible focus styles.
- Motion: animate transform and opacity; respect prefers-reduced-motion.
- Privacy: synthetic data in examples; never log or store credentials, tokens or payment data; no analytics or third-party scripts without approval.
