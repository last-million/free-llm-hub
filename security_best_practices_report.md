# Security best-practices review

## Executive summary

The lifecycle and multimodal changes add atomic state writes, checksum-guarded
CLI snapshots, strict image validation, and browser hardening. No Critical or
High issue was found in the reviewed code. Two residual risks are inherent to
the current localhost control-plane design and one supply-chain hygiene issue
remain for a production or multi-user deployment.

## Scope and checks

- Python/Flask backend, browser JavaScript, shell launchers, and dependency
  declarations.
- State-changing routes, outbound HTTP, subprocess use, filesystem writes,
  image handling, DOM sinks, and response headers.
- `python -m pytest -q` (13 passed), Python compilation, JavaScript syntax
  validation, and Git whitespace validation.

## Medium findings

### SEC-001: Any local OS user can access the localhost control plane

- Rule ID: FLASK-AUTH-001 / FLASK-CSRF-001
- Severity: Medium (accepted for this deployment model, see below)
- Location: `app.py` `_local_control_guard`, `api_provider_reveal_key`,
  `api_cli_instructions`, and the `index()` route that renders the token.
- Evidence: The guard limits `Host`/`Origin` to loopback, requires the literal
  `X-Free-LLM-Hub: dashboard` header for writes, and now also requires
  `X-Free-LLM-Hub-Token: <token>` on every `/api/*` request. The token is
  generated once (`config.ensure_control_token()`), stored `0600`, and printed
  at startup — but is ALSO rendered directly into `index.html` (`{{
  control_token | tojson }}`) so the dashboard needs no manual paste step.
- Impact: A cross-site browser request still cannot succeed (no Origin
  spoofing, no custom header, can't read the token from a page it never
  loaded). A DIFFERENT local OS account on a shared machine that loads the
  SAME dashboard URL in its own browser gets the token from the page same as
  the legitimate user, so it is not actually isolated from the control plane —
  the token only closes the gap for callers that cannot load `/` at all
  (e.g. a bare `curl`/script pointed at `/api/*` without ever fetching the
  page).
- Fix (if the shared-machine case matters for a given install): stop rendering
  the token into the page; require it to be entered once (e.g. via a browser
  prompt into `localStorage`) sourced only from the process's console output
  or the `0600` config file, or use an OS-authenticated transport (Unix-domain
  socket) instead of a shared loopback TCP port.
- Mitigation / accepted trade-off: this hub targets a single-user local dev
  machine; the maintainer explicitly chose zero-friction auto-embedding over
  the stricter prompt-once flow. Keep the service bound to loopback, use a
  single-user account, and do not run it on a multi-user host if that
  assumption changes.

### SEC-002: Automatic `git pull` executes newly fetched repository code

- Rule ID: FLASK-SUPPLY-001
- Severity: Medium
- Location: `app.py:5198` (`_do_update_check`) and `app.py:5227`
  (`_reexec_soon`)
- Evidence: The updater runs `git pull --ff-only` and then `os.execv` when the
  commit changes. It verifies neither signed commits nor a release artifact.
- Impact: A compromised upstream repository, remote, or developer Git trust
  configuration can lead to code execution as the hub user.
- Fix: Make updates explicit/opt-in, or verify signed tags/commits from pinned
  trusted keys before re-execution. A signed, checksummed release updater is
  stronger still.
- Mitigation: Pin the `origin` remote to a trusted repository, protect the
  local Git configuration, and disable automatic updates in sensitive setups.

## Low findings

### SEC-003: Runtime dependencies are not constrained to reviewed versions

- Rule ID: FLASK-SUPPLY-001
- Severity: Low
- Location: `requirements.txt:1-2`
- Evidence: Dependencies use broad lower bounds: `flask>=2.2` and
  `requests>=2.28`.
- Impact: Fresh installs can resolve materially different, unreviewed releases
  and do not provide reproducible vulnerability remediation.
- Fix: Add a locked requirements file with exact versions and hashes, then
  review/update it regularly.
- Mitigation: Install into isolated environments and periodically scan the
  resolved environment for advisories.

## Resolved in this change

- Cross-site localhost control requests are rejected by the loopback
  Host/Origin guard and the non-simple control header at `app.py:1567`.
- Browser responses receive a nonce-based script CSP plus `nosniff`, anti-frame,
  no-referrer, permissions, CORP, and API no-store headers at `app.py:1586`.
- Custom provider endpoints reject credentials, non-HTTP(S) schemes, query/
  fragment components, and cleartext non-loopback URLs at `app.py:224`.
- Image data is bounded and validated before routing; remote image URLs are not
  fetched by the hub itself at `app.py:472`.
- Config, intentional-stop markers, CLI writes, and snapshot restores use
  atomic replacement and lifecycle state uses revision-checked updates
  (`config.py:263`, `config.py:498`, `config.py:590`).
