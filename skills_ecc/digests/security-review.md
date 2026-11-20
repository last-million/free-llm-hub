# Security review checklist
Run it before shipping auth, user input, secrets, endpoints, payments or sensitive data.
1. Secrets: none hardcoded; read from environment; .env in .gitignore; none in git history; fail loudly when missing.
2. Input: validate every input with a schema (zod, pydantic); allow-list, not block-list; uploads limited by size, MIME type and extension; errors must not leak internals.
3. SQL: parameterized queries only, never string concatenation.
4. Auth: tokens in httpOnly + Secure + SameSite cookies, not localStorage; check role or ownership before every sensitive action; row-level security where the database supports it.
5. XSS: sanitize user HTML (DOMPurify, tight tag allow-list); set a Content-Security-Policy.
6. CSRF: tokens on state-changing requests; SameSite=Strict.
7. Rate limit every endpoint, stricter on expensive ones (search, login).
8. Data exposure: never log passwords, tokens or card data; users get generic errors, stack traces stay in server logs.
9. Web3: verify wallet signatures, validate recipient, amount and balance, never sign blindly.
10. Dependencies: npm audit clean, lock files committed, `npm ci` in CI.
Test it: 401 without auth, 403 for the wrong role, 400 for bad input, 429 over the limit. End with a pass/fail line per item.
