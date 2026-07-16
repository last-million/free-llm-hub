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
- Severity: Medium
- Location: `app.py:1567` (`_local_control_guard`), `app.py:1910`
  (`api_provider_reveal_key`), and `app.py:4037` (`api_cli_instructions`)
- Evidence: The guard limits `Host` and `Origin` to loopback and requires the
  literal `X-Free-LLM-Hub: dashboard` header for writes, but does not require a
  secret or authenticate the caller. The key-reveal and CLI-instructions routes
  are GET endpoints and return secrets to a loopback caller.
- Impact: On a shared machine, another local user/process that can connect to
  the bound loopback port can read provider/gateway keys, alter CLI settings,
  spend quota, or stop the hub. This is distinct from a web-site CSRF attack.
- Fix: Add an authenticated local control plane (for example, a per-install
  bearer token stored with `0600` permissions and supplied by the dashboard),
  or use an OS-authenticated local transport such as a Unix-domain socket.
- Mitigation: Keep the service bound to loopback, use a single-user account,
  and do not run it on multi-user hosts. The current implementation blocks
  browser cross-site/DNS-rebinding writes but does not separate OS users.

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
