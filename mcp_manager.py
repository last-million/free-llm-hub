"""Calvoun Free LLM Hub -- MCP server config manager for agent CLIs.

WHY this exists: the hub now speaks MCP itself (POST /mcp, tools crew_run /
crew_start / crew_result), so an agent CLI that can reach the hub's crews as
NATIVE tools is far more useful than one that can only chat. Every supported
CLI already grew MCP support, but each stores server entries in a different
file in a different shape (TOML tables for kimi/codex, two incompatible JSON
shapes for claude/opencode). Hand-editing four config dialects is exactly the
kind of chore the hub's one-click Connect buttons already removed for provider
wiring -- this module does the same for MCP entries, so the dashboard can
offer "enable hub crews in this CLI" plus generic list/add/remove.

Design rules (mirroring app.py's auto-fixers, which this complements):
  * stdlib only -- tomllib READS TOML; the small TOML additions are
    hand-written text blocks exactly the way _autofix_kimi writes
    [providers.free-hub], so no third-party TOML writer is needed.
  * ADDITIVE and REVERSIBLE: unrelated config content (other providers, other
    MCP servers, comments) is never touched; a one-time .mcp-manager-bak
    backup is taken before the first write, and a non-empty file that cannot
    be backed up is refused rather than risked.
  * never-raising public API returning (ok, msg) / plain dicts -- the HTTP
    layer turns these straight into dashboard answers.
  * HOME resolution is injectable (MCP_MANAGER_HOME env var, or monkeypatch
    _home()) so tests run fully under a temp dir; the repo-local opencode
    config dir is likewise a module-level (_REPO_DIR) for the same reason.
  * never log or echo secrets: env values are written to the CLI's config but
    never appear in returned messages.
"""

import json
import os
import re
import shutil
import tempfile

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11 has no stdlib TOML reader
    tomllib = None


# Repo root: the repo-local .opencode/opencode.json takes precedence over the
# global one when it exists (that is opencode's own project-config rule).
# Module-level so tests can point it at a temp dir.
_REPO_DIR = os.path.dirname(os.path.abspath(__file__))

_BACKUP_SUFFIX = ".mcp-manager-bak"

# claude stores {"type": "http", ...} for remote servers; opencode stores
# {"type": "remote", ...}. stdio is the default type for both (claude accepts
# an explicit "stdio"; opencode spells it "local" with a single command ARRAY
# -- documented shapes, hence the per-CLI adapters below).
_SUPPORTED = ("kimi", "codex", "claude", "opencode")


def _home():
    """Injectable HOME: MCP_MANAGER_HOME wins (tests), else the real home."""
    override = os.environ.get("MCP_MANAGER_HOME")
    if override:
        return os.path.abspath(os.path.expanduser(override))
    return os.path.expanduser("~")


def _xdg_config():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.join(_home(), ".config")


def _isolated_dir(cli):
    """The hub's OWN private config dir for `cli` -- what agentic_chat.py hands
    the subprocess as CODEX_HOME / CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME."""
    return os.path.join(_home(), ".free-llm-hub", "isolated-clis", cli, "config")


def _config_path(cli, isolated=False):
    """Absolute path of `cli`'s MCP-bearing config file (None-safe).

    `isolated=True` targets the hub's OWN copy of the CLI instead of the user's
    global install. This matters because the two are deliberately separate:
    agentic_chat.py runs every /agent session with CODEX_HOME /
    CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME pointed at ~/.free-llm-hub/
    isolated-clis/<cli>/config, so a server registered globally is INVISIBLE to
    the hub's own agent chat -- which is exactly where the crew tools are most
    useful. Note the layouts differ rather than nest: CODEX_HOME points AT the
    directory holding config.toml, so the isolated path is not simply the
    global one under a different home."""
    if isolated:
        d = _isolated_dir(cli)
        if cli in ("kimi", "codex"):
            return os.path.join(d, "config.toml")
        if cli == "claude":
            return os.path.join(d, ".claude.json")
        if cli == "opencode":
            return os.path.join(d, "opencode", "opencode.json")
        return None
    if cli == "kimi":
        return os.path.join(_home(), ".kimi", "config.toml")
    if cli == "codex":
        return os.path.join(_home(), ".codex", "config.toml")
    if cli == "claude":
        return os.path.join(_home(), ".claude.json")
    if cli == "opencode":
        local = os.path.join(_REPO_DIR, ".opencode", "opencode.json")
        if os.path.isfile(local):
            return local
        return os.path.join(_xdg_config(), "opencode", "opencode.json")
    return None


# Which top-level container key holds MCP servers in each file, and whether
# the file is TOML or JSON.
def _format(cli):
    if cli in ("kimi", "codex"):
        return "toml"
    return "json"


# ---------------------------------------------------------------------------
# small file helpers (same discipline as app.py's _backup_once/_cli_write_text)


def _backup_once(path):
    """Copy path -> path + .mcp-manager-bak exactly once (never clobber an
    existing backup). Returns the backup path if one exists, else None."""
    bak = path + _BACKUP_SUFFIX
    try:
        if os.path.isfile(path) and not os.path.exists(bak):
            shutil.copy2(path, bak)
        return bak if os.path.exists(bak) else None
    except OSError:
        return None


def _write_text(path, text):
    """Atomic write (temp file + os.replace) so a crash mid-write can never
    leave a half-written config behind."""
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".mcp-manager-write-", dir=parent or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if os.name == "posix":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_text(path):
    """File text, or None when the file does not exist. utf-8-sig strips a
    leading BOM so it can't land mid-file after edits (breaks TOML parsing).
    Raises OSError on a genuine read failure."""
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8-sig", errors="ignore") as f:
        return f.read()


# ---------------------------------------------------------------------------
# TOML handling (kimi + codex): tomllib for reads, hand-written text for writes


_TOML_TABLE_RE = re.compile(r"^\s*\[")
_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_str(value):
    """Double-quote a scalar as a TOML basic string."""
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return '"%s"' % s


def _toml_key(name):
    """Bare key when legal, quoted otherwise (server names can contain dots
    or spaces, which MUST be quoted to stay a single key)."""
    if _TOML_BARE_KEY_RE.match(name):
        return name
    return _toml_str(name)


def _remove_toml_table(text, table_name):
    """Remove a '[<table_name>]' block (header through the line before the
    next table header, or EOF). Ported from app.py's helper of the same name
    so removal semantics match the hub's other TOML edits exactly. Returns
    (new_text, removed_bool)."""
    lines = text.splitlines()
    target = re.compile(r"^\s*\[\s*%s\s*\]\s*$" % re.escape(table_name))
    out, removed, i, n = [], False, 0, len(lines)
    while i < n:
        if target.match(lines[i]):
            removed = True
            i += 1
            while i < n and not _TOML_TABLE_RE.match(lines[i]):
                i += 1
            continue
        out.append(lines[i])
        i += 1
    new_text = "\n".join(out).rstrip("\n")
    return (new_text + "\n" if new_text else ""), removed


def _remove_toml_server(text, name):
    """Remove [mcp_servers.<name>] AND any [mcp_servers.<name>.*] sub-tables
    (some CLIs spell env as a sub-table; a force-overwrite must not leave a
    stale one behind). The name may be stored bare or quoted -- both header
    spellings match. Returns (new_text, removed_bool)."""
    spellings = list(dict.fromkeys((name, _toml_key(name))))
    name_alt = "|".join(re.escape(s) for s in spellings)
    removed = False
    # The server table itself.
    text, did = _remove_toml_table(text, "mcp_servers.%s" % _toml_key(name))
    removed = removed or did
    # Sub-tables: find headers like [mcp_servers.<name>.env] (bare or quoted
    # name segment) and drop each.
    header_re = re.compile(
        r"^\s*\[\s*mcp_servers\.(%s)\.([^\]]+)\]\s*$" % name_alt)
    for ln in list(text.splitlines()):
        m = header_re.match(ln)
        if m:
            text, did = _remove_toml_table(
                text, "mcp_servers.%s.%s" % (m.group(1), m.group(2).strip()))
            removed = removed or did
    return text, removed


def _toml_server_block(name, spec):
    """The hand-written TOML block for one server, in the shape kimi and codex
    both document: a [mcp_servers.<name>] table with command/args/env (stdio)
    or url (http). env is an inline table so the whole entry stays ONE block
    that _remove_toml_table can lift out cleanly."""
    lines = ["[mcp_servers.%s]" % _toml_key(name)]
    if spec.get("url"):
        lines.append("url = %s" % _toml_str(spec["url"]))
    else:
        lines.append("command = %s" % _toml_str(spec["command"]))
        args = spec.get("args") or []
        if args:
            lines.append("args = [%s]" % ", ".join(_toml_str(a) for a in args))
        env = spec.get("env") or {}
        if env:
            pairs = ", ".join(
                "%s = %s" % (_toml_key(k), _toml_str(v)) for k, v in env.items())
            lines.append("env = {%s}" % pairs)
    return "\n".join(lines)


def _toml_servers(text):
    """Parse mcp_servers out of TOML text -> (entries_dict, error_or_None).
    An absent file/section is NOT an error; broken TOML is."""
    if not text or not text.strip():
        return {}, None
    if tomllib is None:
        return None, "tomllib unavailable (Python 3.11+ required)"
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return None, "invalid TOML: %s" % exc
    servers = data.get("mcp_servers")
    if servers is None:
        return {}, None
    if not isinstance(servers, dict):
        return None, "mcp_servers is not a table"
    return servers, None


# ---------------------------------------------------------------------------
# JSON handling (claude + opencode): whole-document load/modify/dump, which by
# construction preserves every unrelated key.


def _json_container_key(cli):
    return "mcpServers" if cli == "claude" else "mcp"


def _json_servers(cli, text):
    """-> (whole_doc_dict, entries_dict, error_or_None)."""
    if not text or not text.strip():
        return {}, {}, None
    try:
        data = json.loads(text)
    except ValueError as exc:
        return None, None, "invalid JSON: %s" % exc
    if not isinstance(data, dict):
        return None, None, "top-level JSON is not an object"
    servers = data.get(_json_container_key(cli))
    if servers is None:
        return data, {}, None
    if not isinstance(servers, dict):
        return None, None, "%s is not an object" % _json_container_key(cli)
    return data, servers, None


def _json_entry_shape(cli, spec):
    """The CLI's documented config shape for one server."""
    if spec.get("url"):
        if cli == "claude":
            return {"type": "http", "url": spec["url"]}
        return {"type": "remote", "url": spec["url"]}  # opencode
    if cli == "claude":
        entry = {"type": "stdio", "command": spec["command"]}
        if spec.get("args"):
            entry["args"] = list(spec["args"])
        if spec.get("env"):
            entry["env"] = dict(spec["env"])
        return entry
    # opencode stdio: a single command ARRAY, env under "environment".
    entry = {"type": "local",
             "command": [spec["command"]] + list(spec.get("args") or [])}
    if spec.get("env"):
        entry["environment"] = dict(spec["env"])
    return entry


# ---------------------------------------------------------------------------
# normalisation: every backend entry -> {name, transport, command?, args?,
# url?, env?} so the dashboard renders one shape regardless of CLI dialect.


def _normalize_entry(cli, name, entry):
    out = {"name": name}
    if not isinstance(entry, dict):
        out["transport"] = "unknown"
        return out
    env = entry.get("env")
    if env is None:
        env = entry.get("environment")  # opencode stdio spelling
    url = entry.get("url")
    if isinstance(url, str) and url:
        out["transport"] = "http"
        out["url"] = url
    else:
        out["transport"] = "stdio"
        command = entry.get("command")
        if cli == "opencode" and isinstance(command, list):
            # opencode folds command+args into one array; split it back out.
            if command:
                out["command"] = command[0]
            if len(command) > 1:
                out["args"] = list(command[1:])
        else:
            if isinstance(command, str):
                out["command"] = command
            args = entry.get("args")
            if isinstance(args, list) and args:
                out["args"] = list(args)
    if isinstance(env, dict) and env:
        out["env"] = dict(env)
    return out


# ---------------------------------------------------------------------------
# spec validation


def _validate_spec(spec):
    """-> (clean_spec, error_or_None). Accepts {command, args?, env?} (stdio)
    or {url} (http); 'force' is consumed here and never stored."""
    if not isinstance(spec, dict):
        return None, "spec must be an object"
    clean = {}
    url = spec.get("url")
    command = spec.get("command")
    if url is not None:
        if not isinstance(url, str) or not url.strip():
            return None, "spec.url must be a non-empty string"
        clean["url"] = url.strip()
        return clean, None
    if not isinstance(command, str) or not command.strip():
        return None, "spec needs either a non-empty 'url' (http) or 'command' (stdio)"
    clean["command"] = command
    args = spec.get("args")
    if args is not None:
        if not isinstance(args, list) or not all(isinstance(a, (str, int, float)) for a in args):
            return None, "spec.args must be a list of strings"
        clean["args"] = [str(a) for a in args]
    env = spec.get("env")
    if env is not None:
        if not isinstance(env, dict) or not all(isinstance(k, str) for k in env):
            return None, "spec.env must be an object of string keys"
        clean["env"] = {k: str(v) for k, v in env.items()}
    return clean, None


# ---------------------------------------------------------------------------
# public API (never raises)


def supported_clis():
    """CLI ids this module can manage, in dashboard display order."""
    return list(_SUPPORTED)


def list_servers(isolated=False):
    """-> {cli_id: [normalised entries], "errors": [...]}. A config that is
    missing is simply an empty list; one that cannot be read or parsed is an
    "errors" entry ({cli, path, error}) so the dashboard can say WHY a CLI's
    list is empty instead of silently showing nothing.

    `isolated=True` reports the hub's OWN copies of the CLIs (see
    _config_path) instead of the user's global installs."""
    out = {}
    errors = []
    for cli in _SUPPORTED:
        path = _config_path(cli, isolated)
        try:
            text = _read_text(path)
        except OSError as exc:
            errors.append({"cli": cli, "path": path, "error": "unreadable: %s" % exc})
            out[cli] = []
            continue
        if _format(cli) == "toml":
            servers, err = _toml_servers(text)
        else:
            _doc, servers, err = _json_servers(cli, text)
        if err:
            errors.append({"cli": cli, "path": path, "error": err})
            out[cli] = []
            continue
        out[cli] = [_normalize_entry(cli, name, entry)
                    for name, entry in sorted(servers.items())]
    out["errors"] = errors
    return out


def _prepare_write(path):
    """Backup-once + refuse-if-unbackable guard shared by add/remove. Returns
    an error string, or None when writing is safe."""
    backup = _backup_once(path)
    try:
        if backup is None and os.path.isfile(path) and os.path.getsize(path) > 0:
            return ("could not back up the existing config (%s) — refusing to "
                    "overwrite it" % path)
    except OSError:
        return ("could not back up the existing config (%s) — refusing to "
                "overwrite it" % path)
    return None


def add_server(cli, name, spec, isolated=False):
    """Add one MCP server entry to `cli`'s config. -> (ok, msg).

    `isolated=True` writes to the hub's OWN copy of the CLI (see _config_path)
    rather than the user's global install -- the /agent sessions run there.

    Adding an existing name is (False, 'exists') unless spec has force:true,
    in which case the old entry is replaced cleanly (same remove-then-append
    discipline as the hub's Kimi reconnect). Unparseable existing configs are
    refused, never clobbered."""
    try:
        if cli not in _SUPPORTED:
            return False, "unsupported cli %r (supported: %s)" % (
                cli, ", ".join(_SUPPORTED))
        if not isinstance(name, str) or not name.strip():
            return False, "name must be a non-empty string"
        name = name.strip()
        clean, err = _validate_spec(spec)
        if err:
            return False, err
        force = bool(isinstance(spec, dict) and spec.get("force"))
        path = _config_path(cli, isolated)
        try:
            text = _read_text(path)
        except OSError as exc:
            return False, "could not read %s: %s" % (path, exc)

        if _format(cli) == "toml":
            body = text or ""
            servers, perr = _toml_servers(body)
            if perr:
                return False, "refusing to edit unparseable config %s: %s" % (path, perr)
            if name in servers and not force:
                return False, "exists"
            guard = _prepare_write(path)
            if guard:
                return False, guard
            if name in servers:
                body, _ = _remove_toml_server(body, name)
            block = _toml_server_block(name, clean)
            new_text = body.rstrip("\n")
            new_text = (new_text + "\n\n" if new_text else "") + block + "\n"
            _write_text(path, new_text)
        else:
            doc, servers, perr = _json_servers(cli, text)
            if perr:
                return False, "refusing to edit unparseable config %s: %s" % (path, perr)
            if name in servers and not force:
                return False, "exists"
            guard = _prepare_write(path)
            if guard:
                return False, guard
            servers[name] = _json_entry_shape(cli, clean)
            doc[_json_container_key(cli)] = servers
            _write_text(path, json.dumps(doc, indent=2) + "\n")
        return True, "added %r to %s (%s)" % (name, cli, path)
    except Exception as exc:  # never-raising contract
        return False, "unexpected error: %s" % exc


def remove_server(cli, name, isolated=False):
    """Remove one MCP server entry from `cli`'s config. -> (ok, msg).
    Removing a name that is not there is (False, 'not found')."""
    try:
        if cli not in _SUPPORTED:
            return False, "unsupported cli %r (supported: %s)" % (
                cli, ", ".join(_SUPPORTED))
        if not isinstance(name, str) or not name.strip():
            return False, "name must be a non-empty string"
        name = name.strip()
        path = _config_path(cli, isolated)
        try:
            text = _read_text(path)
        except OSError as exc:
            return False, "could not read %s: %s" % (path, exc)

        if _format(cli) == "toml":
            body = text or ""
            servers, perr = _toml_servers(body)
            if perr:
                return False, "refusing to edit unparseable config %s: %s" % (path, perr)
            if name not in servers:
                return False, "not found"
            guard = _prepare_write(path)
            if guard:
                return False, guard
            new_text, _ = _remove_toml_server(body, name)
            _write_text(path, new_text)
        else:
            doc, servers, perr = _json_servers(cli, text)
            if perr:
                return False, "refusing to edit unparseable config %s: %s" % (path, perr)
            if name not in servers:
                return False, "not found"
            guard = _prepare_write(path)
            if guard:
                return False, guard
            del servers[name]
            doc[_json_container_key(cli)] = servers
            _write_text(path, json.dumps(doc, indent=2) + "\n")
        return True, "removed %r from %s (%s)" % (name, cli, path)
    except Exception as exc:  # never-raising contract
        return False, "unexpected error: %s" % exc
