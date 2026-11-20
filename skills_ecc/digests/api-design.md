# REST API design
- URLs: plural nouns, lowercase kebab-case, no verbs (/api/v1/team-members); nest for ownership (/users/:id/orders); filter, sort and paginate with query params; a non-CRUD action is POST /orders/:id/cancel.
- Methods: GET read (safe), POST create or action, PUT replace, PATCH partial update, DELETE remove.
- Status codes: 200 OK, 201 Created (+ Location header), 204 No Content, 400 malformed or validation, 401 not authenticated, 403 not allowed, 404 missing, 409 conflict, 422 semantically invalid, 429 rate limited (+ Retry-After), 500 unexpected (no details), 502/503 upstream or overload. Never 200 with an error body; never 500 for validation.
- Responses: {"data": ...}; lists add "meta" (total, page, per_page) and "links"; errors are {"error": {"code": "not_found", "message": "...", "details": [field errors]}}.
- Pagination: offset for small data and admin pages; opaque cursor for feeds and large data; always cap per_page.
- Rate limits: send X-RateLimit-Limit, -Remaining, -Reset; tiers per IP, user and API key.
- Versioning: /api/v1 in the path; add fields without breaking clients; deprecate before removing.
- Before shipping: schema-validated input, auth and ownership checks, no internal leaks, consistent field naming, OpenAPI updated.
