# Backend patterns
- Layers: route or controller (parse, validate, respond) -> service (business rules) -> repository (data access). No SQL in routes; depend on interfaces so layers are testable.
- API: REST resources, validate every body and query with a schema, one response envelope, middleware for auth, logging and rate limiting.
- Database: select only needed columns, index filter and sort columns, paginate, pool connections. Prevent N+1: fetch related rows in one query (IN or join) and map them. Use a transaction for multi-step writes.
- Caching: cache-aside (read cache; on miss read the DB and set with a TTL; invalidate on write). Always set a TTL and fall back to the DB when the cache is down.
- Errors: one centralized handler mapping known error classes to status codes; log details server-side, return generic messages for unexpected failures; retry transient failures with exponential backoff and a capped attempt count.
- Auth: verify the JWT on every protected route; role-to-permission map checked in middleware; never trust a user id sent by the client.
- Rate limit per IP or user, stricter on expensive endpoints.
- Background jobs: move slow work (email, exports, embeddings) to a queue with retries and a failure log.
- Logging: structured JSON with request id, level and context; never log secrets.
