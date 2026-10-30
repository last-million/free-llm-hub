"""MCP servers the OWNER registers once in the hub, re-exposed through the
hub's own MCP endpoint (POST /mcp) to every CLI (2026-10-29).

WHY: every connected CLI and every Build / Multi session already has the hub's
own MCP entry. A server added ONCE here -- Shopify's dev server, GitHub's,
Supabase's, Stripe's, Cloudflare's -- therefore reaches all of them, with no
per-CLI config file to write and nothing to keep in sync.

WHAT:
  * stdio servers (command + args + env) and streamable-HTTP servers (url +
    optional bearer token). Their tools are listed through the hub as
    `<server>__<tool>` (sanitized to the client tool-name rules, at most
    MAX_TOOL_NAME characters so `mcp__free-llm-hub__` + the name still fits a
    64-character limit) and a call is proxied: time-boxed, output capped,
    every failure a TOOL error the model can read.
  * stdio servers start lazily (the first call, or the owner's own Add/Refresh),
    stay alive while used, are stopped -- as a whole process tree -- after
    IDLE_TTL seconds unused and at hub exit, and never more than MAX_PROCS run
    at once (the least recently used idle one makes room).
  * tools/list is answered from a CACHE (persisted in the state folder): a CLI
    connecting never waits for (or spawns) a server. The cache is refreshed
    when the owner adds/updates a server, whenever a server is started for a
    call, and when it says its tool list changed.

SAFETY:
  * Registering is an explicit owner action (the route needs confirm: true,
    because a stdio server is a program the hub will run as the owner).
  * Secrets -- env values and the bearer token -- are stored encrypted
    (secretstore, like the provider keys), never logged, never returned by a
    route: a server is shown with its env KEY names and `has_token` only, and
    args that look like a secret are masked.
  * HTTP: https only (http only to a loopback address), no credentials in the
    URL, and redirects are NOT followed -- a redirect would carry the bearer
    token to another host.
  * The hub never logs in to anything and never runs a CATALOG entry until the
    owner adds it.

Every side effect (spawn, http, which, clock, store) is injectable; the module
default effects are fenced in tests/conftest.py. Stdlib only.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

_log = logging.getLogger("free-llm-hub")

PROTOCOL_VERSION = "2025-03-26"
CLIENT_INFO = {"name": "free-llm-hub-gateway", "version": "1"}
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
MAX_TOOL_NAME = 45
MAX_SERVERS = 30
MAX_PROCS = 4
IDLE_TTL = 600.0
INIT_TIMEOUT = 90.0
LIST_TIMEOUT = 30.0
CALL_TIMEOUT = 120.0
MAX_RESULT_CHARS = 60000
MAX_LINE_BYTES = 8 * 1024 * 1024
MAX_HTTP_BYTES = 8 * 1024 * 1024
MAX_TOOLS_PER_SERVER = 128
MAX_DESC_CHARS = 1024
MAX_SCHEMA_CHARS = 20000
MAX_ARGS = 40
MAX_ENV = 40
REAP_EVERY = 30.0
CACHE_FILE = "mcp-gateway-tools.json"
CONFIG_KEY = "mcp_gateway"

# cmd.exe metacharacters: a .cmd/.bat target (npx.cmd on Windows) is run
# through cmd.exe, which would interpret them inside an argument.
_CMD_META_RE = re.compile(r'[&|<>^%"!\r\n]')
_SECRET_ARG_FLAG_RE = re.compile(r"(key|token|secret|password|passwd|auth|credential)", re.I)
_SECRET_VALUE_RE = re.compile(
    r"^(sk|rk|pk)_(live|test)_|^(ghp|gho|ghu|ghs|github_pat)_|^sbp_|^xox[abprs]-|"
    r"^eyJ[A-Za-z0-9_-]{10,}\.|^AKIA[0-9A-Z]{12,}|^[A-Za-z0-9_\-]{32,}$")


class GatewayError(Exception):
    """A refusal or failure with a stable `code` and a plain message."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------- #
# THE CATALOG -- official servers the owner can add in one click. Every entry
# was read on the vendor's own docs/repo on 2026-10-29 (`source`). Nothing here
# runs until the owner adds it. OAuth-only servers (Vercel's, Sentry's) are left
# out: this gateway sends a bearer token at most, it does not do OAuth.
# `auth`: none | token (bearer, required) | token_optional | env (see env_keys).
# --------------------------------------------------------------------------- #

CATALOG = (
    {"id": "shopify-dev", "title": "Shopify Dev MCP", "transport": "stdio",
     "command": "npx", "args": ["-y", "@shopify/dev-mcp@latest"], "auth": "none",
     "note": "Shopify docs and API schemas. Needs Node 18+.",
     "source": "https://shopify.dev/docs/apps/build/devmcp"},
    {"id": "github", "title": "GitHub", "transport": "http",
     "url": "https://api.githubcopilot.com/mcp/", "auth": "token",
     "note": "Paste a GitHub personal access token as the bearer token.",
     "source": "https://docs.github.com/en/copilot/how-tos/provide-context/use-mcp/set-up-the-github-mcp-server"},
    {"id": "supabase", "title": "Supabase", "transport": "http",
     "url": "https://mcp.supabase.com/mcp", "auth": "token",
     "note": "Paste a Supabase personal access token as the bearer token.",
     "source": "https://supabase.com/docs/guides/getting-started/mcp"},
    {"id": "stripe", "title": "Stripe", "transport": "http",
     "url": "https://mcp.stripe.com", "auth": "token",
     "note": "Paste a Stripe API key with the Agent tag (Stripe refuses full-access "
             "secret keys from December 2026).",
     "source": "https://docs.stripe.com/mcp"},
    {"id": "cloudflare-docs", "title": "Cloudflare docs", "transport": "http",
     "url": "https://docs.mcp.cloudflare.com/mcp", "auth": "none",
     "note": "Cloudflare documentation search. No account needed.",
     "source": "https://github.com/cloudflare/mcp-server-cloudflare/tree/main/apps/docs-ai-search"},
    {"id": "cloudflare", "title": "Cloudflare API", "transport": "http",
     "url": "https://mcp.cloudflare.com/mcp", "auth": "token",
     "note": "Paste a Cloudflare API token as the bearer token.",
     "source": "https://developers.cloudflare.com/agents/model-context-protocol/mcp-servers-for-cloudflare/"},
    {"id": "netlify", "title": "Netlify", "transport": "stdio",
     "command": "npx", "args": ["-y", "@netlify/mcp"], "auth": "env",
     "env_keys": ["NETLIFY_PERSONAL_ACCESS_TOKEN"], "env_optional": True,
     "note": "Optional: a Netlify personal access token.",
     "source": "https://github.com/netlify/netlify-mcp"},
    {"id": "firebase", "title": "Firebase", "transport": "stdio",
     "command": "npx", "args": ["-y", "firebase-tools@latest", "mcp"], "auth": "none",
     "note": "Uses your Firebase CLI login (run `firebase login` yourself first).",
     "source": "https://firebase.google.com/docs/ai-assistance/mcp-server"},
    {"id": "playwright", "title": "Playwright", "transport": "stdio",
     "command": "npx", "args": ["@playwright/mcp@latest"], "auth": "none",
     "note": "Browser automation by Microsoft.",
     "source": "https://github.com/microsoft/playwright-mcp"},
    {"id": "context7", "title": "Context7", "transport": "http",
     "url": "https://mcp.context7.com/mcp", "auth": "token_optional",
     "note": "Up-to-date library docs. A key is optional (higher rate limits).",
     "source": "https://context7.com/docs/resources/all-clients"},
    {"id": "aws-knowledge", "title": "AWS Knowledge", "transport": "http",
     "url": "https://knowledge-mcp.global.api.aws", "auth": "none",
     "note": "AWS documentation. No AWS account needed (rate-limited).",
     "source": "https://awslabs.github.io/mcp/servers/aws-knowledge-mcp-server"},
    {"id": "terraform", "title": "Terraform", "transport": "stdio",
     "command": "docker", "args": ["run", "-i", "--rm", "hashicorp/terraform-mcp-server"],
     "auth": "none", "note": "Terraform registry tools. Needs Docker running.",
     "source": "https://github.com/hashicorp/terraform-mcp-server"},
    {"id": "neon", "title": "Neon", "transport": "http",
     "url": "https://mcp.neon.tech/mcp", "auth": "token",
     "note": "Paste a Neon API key as the bearer token.",
     "source": "https://neon.com/docs/ai/neon-mcp-server"},
    {"id": "linear", "title": "Linear", "transport": "http",
     "url": "https://mcp.linear.app/mcp", "auth": "token",
     "note": "Paste a Linear API key as the bearer token.",
     "source": "https://linear.app/docs/mcp"},
)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #

def _clean_str(v, field, max_len, required=False):
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise GatewayError("bad_request", "'%s' is required" % field)
        return None
    if not isinstance(v, str):
        raise GatewayError("bad_request", "'%s' must be a string" % field)
    v = v.strip()
    if len(v) > max_len or "\x00" in v or "\n" in v or "\r" in v:
        raise GatewayError("bad_request", "'%s' is too long or has a line break" % field)
    return v


def _is_loopback(host):
    if not host:
        return False
    if host.lower() in ("localhost",) or host.lower().endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")).is_loopback
    except ValueError:
        return False


def validate_url(url):
    """The URL as given, or GatewayError. https, or http to loopback only; no
    credentials and no fragment."""
    url = _clean_str(url, "url", 2000, required=True)
    p = urllib.parse.urlsplit(url)
    if p.scheme not in ("https", "http") or not p.hostname:
        raise GatewayError("bad_url", "the URL must start with https://")
    if p.scheme == "http" and not _is_loopback(p.hostname):
        raise GatewayError("bad_url", "plain http is only allowed to this computer; use https://")
    if p.username or p.password or "@" in (p.netloc or ""):
        raise GatewayError("bad_url", "put the token in the token field, not in the URL")
    if p.fragment:
        raise GatewayError("bad_url", "the URL must not have a #fragment")
    return url


def _validate_token(token):
    if token is None or token == "":
        return None
    if not isinstance(token, str):
        raise GatewayError("bad_request", "'token' must be a string")
    t = token.strip()
    if t.lower().startswith("bearer "):
        t = t[7:].strip()
    if not t or len(t) > 8000 or re.search(r"[\s\x00-\x1f\x7f]", t):
        raise GatewayError("bad_request", "the token must be one line with no spaces")
    return t


def _validate_env(env):
    if env is None:
        return None
    if not isinstance(env, dict):
        raise GatewayError("bad_request", "'env' must be an object of NAME: value")
    if len(env) > MAX_ENV:
        raise GatewayError("bad_request", "too many environment variables")
    out = {}
    for k, v in env.items():
        if not isinstance(k, str) or not ENV_KEY_RE.match(k):
            raise GatewayError("bad_request", "bad environment variable name: %r" % (k,))
        if v is None:
            v = ""
        if not isinstance(v, str) or len(v) > 4000 or "\x00" in v:
            raise GatewayError("bad_request", "bad value for %s" % k)
        out[k] = v
    return out


def _validate_args(args):
    if args is None:
        return []
    if isinstance(args, str):
        args = args.split()
    if not isinstance(args, list) or len(args) > MAX_ARGS:
        raise GatewayError("bad_request", "'args' must be a list of at most %d strings" % MAX_ARGS)
    out = []
    for a in args:
        if not isinstance(a, str) or len(a) > 1000 or "\x00" in a or "\n" in a or "\r" in a:
            raise GatewayError("bad_request", "every argument must be one line of text")
        out.append(a)
    return out


def mask_args(args):
    """Args for display: the value after a key/token/secret flag, a --flag=value
    of that kind, and anything that looks like a credential become '***'."""
    out, hide_next = [], False
    for a in args or []:
        a = str(a)
        if hide_next:
            out.append("***")
            hide_next = False
            continue
        if a.startswith("-") and "=" in a:
            flag, _v = a.split("=", 1)
            out.append(flag + "=***" if _SECRET_ARG_FLAG_RE.search(flag) else a)
            continue
        if a.startswith("-") and _SECRET_ARG_FLAG_RE.search(a):
            out.append(a)
            hide_next = True
            continue
        out.append("***" if _SECRET_VALUE_RE.search(a) else a)
    return out


def _display_url(url):
    try:
        p = urllib.parse.urlsplit(url or "")
        q = "?..." if p.query else ""
        return urllib.parse.urlunsplit((p.scheme, p.netloc, p.path, "", "")) + q
    except Exception:                                            # noqa: BLE001
        return ""


def _sanitize_tool(name):
    return re.sub(r"[^A-Za-z0-9_-]", "_", str(name))


def exposed_name(server, tool, taken=()):
    """`<server>__<tool>`, sanitized to [A-Za-z0-9_-] and at most MAX_TOOL_NAME
    characters; a truncated or colliding name gets a short hash of the real
    tool name instead of its tail. Deterministic."""
    clean = _sanitize_tool(tool) or "tool"
    name = "%s__%s" % (server, clean)
    if len(name) <= MAX_TOOL_NAME and name not in taken:
        return name
    h = hashlib.sha1(str(tool).encode("utf-8", "replace")).hexdigest()[:6]
    keep = max(1, MAX_TOOL_NAME - len(server) - 2 - 7)
    name = "%s__%s_%s" % (server, clean[:keep], h)
    i = 2
    while name in taken:
        name = "%s__%s_%s%d" % (server, clean[:keep - 2], h, i)
        i += 1
    return name


def _spec_sig(row):
    """A stable signature of a server's shape (never of its secret VALUES):
    a changed command/args/url/env keys/token presence invalidates its cache."""
    shape = {"t": row.get("transport"), "c": row.get("command"), "a": row.get("args"),
             "u": row.get("url"), "e": sorted((row.get("env") or {}).keys()),
             "k": bool(row.get("token"))}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# Storage: config["mcp_gateway"]["servers"], secrets encrypted
# --------------------------------------------------------------------------- #

class ConfigStore:
    """Server definitions under ``config["mcp_gateway"]`` -- outside
    ``providers``, like the GitHub token, so migrations' key fingerprint and
    the providers-only encrypt pass never touch them. Env VALUES and the token
    are encrypted with secretstore; `secret()` decrypts one."""

    def __init__(self, config_module=None, secretstore_module=None):
        self._config = config_module
        self._ss = secretstore_module

    def _cfg(self):
        if self._config is None:
            import config
            self._config = config
        return self._config

    def _sec(self):
        if self._ss is None:
            import secretstore
            self._ss = secretstore
        return self._ss

    def _path(self):
        return self._cfg()._config_path()

    def state_dir(self):
        return self._cfg().state_dir()

    def servers(self):
        try:
            block = self._cfg().load_config().get(CONFIG_KEY)
        except Exception:                                        # noqa: BLE001
            return {}
        rows = block.get("servers") if isinstance(block, dict) else None
        return {k: dict(v) for k, v in rows.items()
                if isinstance(rows, dict) and isinstance(v, dict) and NAME_RE.match(str(k))} \
            if isinstance(rows, dict) else {}

    def encrypt(self, value):
        return self._sec().encrypt(value, self._path()) if value else value

    def secret(self, value):
        if not value:
            return value
        return self._sec().decrypt(value, self._path())

    def put(self, name, row):
        cfg_mod = self._cfg()
        with cfg_mod._LOCK:
            cfg = cfg_mod.load_config(strict=True)
            block = cfg.get(CONFIG_KEY) if isinstance(cfg.get(CONFIG_KEY), dict) else {}
            servers = block.get("servers") if isinstance(block.get("servers"), dict) else {}
            servers[name] = row
            block["servers"] = servers
            cfg[CONFIG_KEY] = block
            cfg_mod.save_config(cfg)

    def delete(self, name):
        cfg_mod = self._cfg()
        with cfg_mod._LOCK:
            cfg = cfg_mod.load_config(strict=True)
            block = cfg.get(CONFIG_KEY) if isinstance(cfg.get(CONFIG_KEY), dict) else {}
            servers = block.get("servers") if isinstance(block.get("servers"), dict) else {}
            if name not in servers:
                return False
            servers.pop(name, None)
            block["servers"] = servers
            cfg[CONFIG_KEY] = block
            cfg_mod.save_config(cfg)
            return True


# --------------------------------------------------------------------------- #
# Default effects (fenced in tests/conftest.py)
# --------------------------------------------------------------------------- #

def _default_spawn(argv, env, cwd):
    kw = {"stdin": subprocess.PIPE, "stdout": subprocess.PIPE,
          "stderr": subprocess.DEVNULL, "env": env, "cwd": cwd}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    else:
        kw["start_new_session"] = True
    return subprocess.Popen(list(argv), **kw)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):   # noqa: D401
        return None


def _parse_sse(lines):
    """Yield the JSON payload of each `data:` event in an SSE stream."""
    data = []
    for raw in lines:
        line = raw.decode("utf-8", "replace").rstrip("\r\n") if isinstance(raw, bytes) else raw
        if line == "":
            if data:
                yield "\n".join(data)
                data = []
            continue
        if line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
    if data:
        yield "\n".join(data)


def _default_http_post(url, body, headers, timeout, want_id=None):
    """POST `body`; -> (status, {lower header: value}, [json messages]). For an
    SSE answer, reading stops at the message whose id is `want_id`. Never
    follows a redirect. Raises GatewayError on a transport failure."""
    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, data=body, method="POST", headers=headers)
    deadline = time.monotonic() + timeout
    try:
        resp = opener.open(req, timeout=timeout)
    except urllib.error.HTTPError as exc:
        hdrs = {k.lower(): v for k, v in (exc.headers or {}).items()}
        return exc.code, hdrs, []
    except Exception as exc:                                     # noqa: BLE001
        raise GatewayError("unreachable", "could not reach the server (%s)"
                           % type(exc).__name__)
    with resp:
        status = getattr(resp, "status", 200)
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        ctype = hdrs.get("content-type", "")
        msgs, total = [], 0
        if "text/event-stream" in ctype:
            def lines():
                nonlocal total
                while time.monotonic() < deadline:
                    line = resp.readline(MAX_HTTP_BYTES)
                    if not line:
                        return
                    total += len(line)
                    if total > MAX_HTTP_BYTES:
                        return
                    yield line
            for chunk in _parse_sse(lines()):
                try:
                    msg = json.loads(chunk)
                except ValueError:
                    continue
                msgs.append(msg)
                if want_id is not None and isinstance(msg, dict) and msg.get("id") == want_id:
                    break
        else:
            raw = resp.read(MAX_HTTP_BYTES + 1)
            if len(raw) > MAX_HTTP_BYTES:
                raise GatewayError("too_large", "the server's answer was too large")
            if raw.strip():
                try:
                    msg = json.loads(raw.decode("utf-8", "replace"))
                except ValueError:
                    raise GatewayError("bad_reply", "the server's answer was not JSON")
                msgs = msg if isinstance(msg, list) else [msg]
        return status, hdrs, msgs


def _kill_tree(proc):
    """Stop `proc` and what it started. Windows: taskkill /T, only while it
    still runs (an exited pid may belong to someone else by now). POSIX: its
    own session's group. Never raises."""
    if proc is None:
        return
    try:
        running = proc.poll() is None
    except Exception:                                            # noqa: BLE001
        running = False
    try:
        if running:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                               capture_output=True, timeout=15,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            else:
                import signal
                try:
                    pgid = os.getpgid(proc.pid)
                except OSError:
                    pgid = None
                if pgid and pgid != os.getpgrp():
                    os.killpg(pgid, signal.SIGKILL)
                else:
                    proc.kill()
    except Exception:                                            # noqa: BLE001
        try:
            proc.kill()
        except Exception:                                        # noqa: BLE001
            pass
    try:
        proc.wait(timeout=5)
    except Exception:                                            # noqa: BLE001
        pass


# --------------------------------------------------------------------------- #
# Connections
# --------------------------------------------------------------------------- #

class _StdioConn:
    """Newline-delimited JSON-RPC over a child's stdin/stdout."""

    transport = "stdio"

    def __init__(self, proc, kill):
        self.proc = proc
        self._kill = kill
        self._lock = threading.Lock()
        self._wlock = threading.Lock()
        self._pending = {}
        self._next = 0
        self.alive = True
        self.tools_changed = False
        self._reader = threading.Thread(target=self._read, daemon=True,
                                        name="mcp-gateway-reader")
        self._reader.start()

    def _read(self):
        f = self.proc.stdout
        skipping = False
        try:
            while True:
                line = f.readline(MAX_LINE_BYTES)
                if not line:
                    break
                if not line.endswith(b"\n") and len(line) >= MAX_LINE_BYTES:
                    skipping = True
                    continue
                if skipping:
                    skipping = False
                    continue
                try:
                    msg = json.loads(line.decode("utf-8", "replace"))
                except ValueError:
                    continue
                if isinstance(msg, dict):
                    self._dispatch(msg)
        except Exception:                                        # noqa: BLE001
            pass
        finally:
            self.alive = False
            with self._lock:
                pending, self._pending = self._pending, {}
            for slot in pending.values():
                slot[0].set()

    def _dispatch(self, msg):
        if "method" in msg:
            method = msg.get("method")
            if method == "notifications/tools/list_changed":
                self.tools_changed = True
            if "id" in msg:      # a request from the server: answer, never hang it
                reply = ({"jsonrpc": "2.0", "id": msg["id"], "result": {}}
                         if method == "ping" else
                         {"jsonrpc": "2.0", "id": msg["id"],
                          "error": {"code": -32601, "message": "not supported by the hub"}})
                self._write(reply)
            return
        rid = msg.get("id")
        if not isinstance(rid, (int, str)):
            return       # not one of ours (and an unhashable id must not end the reader)
        with self._lock:
            slot = self._pending.get(rid)
        if slot is not None:
            slot[1] = msg
            slot[0].set()

    def _write(self, obj):
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        with self._wlock:
            self.proc.stdin.write(data)
            self.proc.stdin.flush()

    def request(self, method, params, timeout):
        if not self.alive:
            raise GatewayError("stopped", "the server is not running")
        with self._lock:
            self._next += 1
            rid = self._next
            slot = [threading.Event(), None]
            self._pending[rid] = slot
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except Exception:                                        # noqa: BLE001
            with self._lock:
                self._pending.pop(rid, None)
            raise GatewayError("stopped", "the server stopped")
        if not slot[0].wait(timeout):
            with self._lock:
                self._pending.pop(rid, None)
            raise GatewayError("timeout", "the server did not answer in %d s" % int(timeout))
        with self._lock:
            self._pending.pop(rid, None)
        if slot[1] is None:
            raise GatewayError("stopped", "the server stopped before answering")
        return slot[1]

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._write(msg)
        except Exception:                                        # noqa: BLE001
            pass

    def close(self):
        self.alive = False
        try:
            self._kill(self.proc)
        except Exception:                                        # noqa: BLE001
            pass


class _HttpConn:
    """Streamable HTTP: one POST per JSON-RPC message."""

    transport = "http"

    def __init__(self, url, token, http_post):
        self.url = url
        self._token = token
        self._post = http_post
        self._lock = threading.Lock()
        self._next = 0
        self.session = None
        self.version = PROTOCOL_VERSION
        self.alive = True
        self.tools_changed = False

    def _headers(self):
        h = {"Content-Type": "application/json",
             "Accept": "application/json, text/event-stream",
             "MCP-Protocol-Version": self.version}
        if self._token:
            h["Authorization"] = "Bearer " + self._token
        if self.session:
            h["Mcp-Session-Id"] = self.session
        return h

    def request(self, method, params, timeout):
        with self._lock:
            self._next += 1
            rid = self._next
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        status, hdrs, msgs = self._post(self.url, json.dumps(msg).encode("utf-8"),
                                        self._headers(), timeout, want_id=rid)
        if status in (401, 403):
            raise GatewayError("auth", "the server refused the token (HTTP %d)" % status)
        if status == 404 and self.session:
            self.alive = False
            raise GatewayError("session", "the server ended the session (HTTP 404)")
        if not 200 <= status < 300:
            # 3xx included: a redirect is never followed (it would carry the
            # bearer token to wherever it points).
            raise GatewayError("http", "the server answered HTTP %d" % status)
        sid = (hdrs or {}).get("mcp-session-id")
        if sid and method == "initialize":
            self.session = sid
        for m in msgs or []:
            if isinstance(m, dict) and m.get("id") == rid:
                return m
        raise GatewayError("bad_reply", "the server did not answer the request")

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self._post(self.url, json.dumps(msg).encode("utf-8"), self._headers(),
                       LIST_TIMEOUT)
        except Exception:                                        # noqa: BLE001
            pass

    def close(self):
        self.alive = False


# --------------------------------------------------------------------------- #
# The gateway
# --------------------------------------------------------------------------- #

def _result_text(result):
    """Cap a tools/call result: text items keep at most MAX_RESULT_CHARS in
    all; other items (images ...) are replaced by a note when they would not
    fit. structuredContent is rendered as text when there is no content."""
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list) or not content:
        sc = result.get("structuredContent") if isinstance(result, dict) else None
        content = [{"type": "text", "text": json.dumps(sc, ensure_ascii=False)}] \
            if sc is not None else []
    out, left = [], MAX_RESULT_CHARS
    for item in content:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "text":
            text = str(item.get("text") or "")
            if len(text) > left:
                text = text[:max(0, left)] + "\n[output cut by the hub: over %d characters]" \
                    % MAX_RESULT_CHARS
            left -= len(text)
            out.append({"type": "text", "text": text})
        else:
            size = len(json.dumps(item, ensure_ascii=False))
            if size <= left:
                out.append(item)
                left -= size
            else:
                out.append({"type": "text",
                            "text": "[%s content omitted by the hub: too large]"
                                    % str(item.get("type") or "binary")})
        if left <= 0:
            break
    return out


def tool_error(text):
    return {"content": [{"type": "text", "text": text}], "isError": True}


class Gateway:
    def __init__(self, store=None, spawn=None, http_post=None, which=None,
                 kill=None, clock=None, enabled=None, env_filter=None,
                 cache_path=None, max_procs=MAX_PROCS, idle_ttl=IDLE_TTL,
                 start_reaper=True, cwd=None):
        self._store = store
        self._spawn = spawn
        self._http_post = http_post
        self._which = which
        self._kill = kill
        self._clock = clock or time.monotonic
        self._enabled = enabled
        self._env_filter = env_filter
        self._cache_path = cache_path
        self.max_procs = max_procs
        self.idle_ttl = idle_ttl
        self._start_reaper = start_reaper
        self._cwd = cwd
        self._lock = threading.RLock()
        self._conns = {}        # name -> conn
        self._busy = {}         # name -> calls in flight
        self._used = {}         # name -> clock of last use
        self._starting = {}     # name -> Lock (one start at a time per server)
        self._status = {}       # name -> {"state", "error"}
        self._cache = None      # name -> {"sig", "tools": [...], "at"}
        self._reaper = None

    # -- configuration --
    def configure(self, **kw):
        for key in ("store", "spawn", "http_post", "which", "kill", "enabled",
                    "env_filter", "cache_path", "cwd"):
            if key in kw:
                setattr(self, "_" + key, kw[key])
        for key in ("max_procs", "idle_ttl"):
            if key in kw:
                setattr(self, key, kw[key])

    def enabled(self):
        fn = self._enabled
        if fn is None:
            return True
        try:
            return bool(fn())
        except Exception:                                        # noqa: BLE001
            return False

    def store(self):
        if self._store is None:
            self._store = ConfigStore()
        return self._store

    # -- registration --
    def register(self, name, command=None, args=None, env=None, url=None,
                 token=None, clear_token=False):
        """Add or update a server. Secrets are encrypted at rest; on an update
        an env value left "" keeps the stored one, a token left out keeps the
        stored one (clear_token=True removes it). Returns the public view."""
        name = _clean_str(name, "name", 24, required=True)
        if not NAME_RE.match(name or ""):
            raise GatewayError("bad_name", "use 1-24 lowercase letters, digits or '-' "
                                           "(starting with a letter or digit)")
        command = _clean_str(command, "command", 300)
        url_given = url is not None and not (isinstance(url, str) and not url.strip())
        if bool(command) == bool(url_given):
            raise GatewayError("bad_request", "give a command OR a URL")
        store = self.store()
        servers = store.servers()
        old = servers.get(name) or {}
        if name not in servers and len(servers) >= MAX_SERVERS:
            raise GatewayError("too_many", "at most %d servers" % MAX_SERVERS)
        env = _validate_env(env)
        row = {"created_at": old.get("created_at") or time.time(), "updated_at": time.time()}
        if command:
            row.update(transport="stdio", command=command, args=_validate_args(args))
            old_env = old.get("env") if isinstance(old.get("env"), dict) else {}
            new_env = {}
            for k, v in (env if env is not None else
                         {k: "" for k in old_env}).items():
                if v == "" and k in old_env:
                    new_env[k] = old_env[k]          # still encrypted
                elif v != "":
                    new_env[k] = store.encrypt(v)
            row["env"] = new_env
        else:
            row.update(transport="http", url=validate_url(url))
            tok = _validate_token(token)
            if tok:
                row["token"] = store.encrypt(tok)
            elif old.get("token") and not clear_token and old.get("transport") == "http":
                row["token"] = old["token"]
        store.put(name, row)
        self._stop(name)
        with self._lock:
            self._status[name] = {"state": "idle", "error": ""}
        return self.server_view(name)

    def remove(self, name):
        if not isinstance(name, str) or not NAME_RE.match(name):
            return False
        self._stop(name)
        ok = self.store().delete(name)
        with self._lock:
            self._status.pop(name, None)
            cache = self._load_cache()
            if name in cache:
                cache.pop(name, None)
                self._save_cache()
        return ok

    # -- views (never a secret) --
    def server_view(self, name, row=None):
        row = row if row is not None else self.store().servers().get(name)
        if not isinstance(row, dict):
            return None
        cache = self._load_cache().get(name) or {}
        fresh = cache.get("sig") == _spec_sig(row)
        with self._lock:
            st = dict(self._status.get(name) or {"state": "idle", "error": ""})
            running = name in self._conns and getattr(self._conns[name], "alive", False)
        view = {"name": name, "transport": row.get("transport"),
                "tool_count": len(cache.get("tools") or []) if fresh else 0,
                "tools": [t.get("name") for t in (cache.get("tools") or [])][:50] if fresh else [],
                "state": st.get("state") or "idle", "error": st.get("error") or "",
                "running": bool(running)}
        if row.get("transport") == "stdio":
            view.update(command=row.get("command"), args=mask_args(row.get("args")),
                        env_keys=sorted((row.get("env") or {}).keys()))
        else:
            view.update(url=_display_url(row.get("url")), has_token=bool(row.get("token")))
        return view

    def servers_view(self):
        rows = self.store().servers()
        return [self.server_view(n, rows[n]) for n in sorted(rows)]

    # -- tool cache --
    def _cache_file(self):
        p = self._cache_path
        if callable(p):
            try:
                p = p()
            except Exception:                                    # noqa: BLE001
                p = None
        if p:
            return p
        try:
            return os.path.join(self.store().state_dir(), CACHE_FILE)
        except Exception:                                        # noqa: BLE001
            return None

    def _load_cache(self):
        with self._lock:
            if self._cache is None:
                self._cache = {}
                path = self._cache_file()
                if path:
                    try:
                        with open(path, "r", encoding="utf-8") as f:
                            data = json.load(f)
                        if isinstance(data, dict):
                            self._cache = {k: v for k, v in data.items()
                                           if isinstance(v, dict) and NAME_RE.match(str(k))}
                    except (OSError, ValueError):
                        pass
            return self._cache

    def _save_cache(self):
        path = self._cache_file()
        if not path:
            return
        try:
            with self._lock:
                data = json.dumps(self._cache or {}, ensure_ascii=False)
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception:                                        # noqa: BLE001
            _log.debug("[mcp-gateway] could not save the tool cache", exc_info=True)

    def _remember_tools(self, name, row, tools):
        clean = []
        for t in (tools or [])[:MAX_TOOLS_PER_SERVER]:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t["name"]:
                continue
            schema = t.get("inputSchema")
            if not isinstance(schema, dict) or schema.get("type") != "object" \
                    or len(json.dumps(schema, ensure_ascii=False)) > MAX_SCHEMA_CHARS:
                schema = {"type": "object"}
            clean.append({"name": t["name"][:200],
                          "description": str(t.get("description") or "")[:MAX_DESC_CHARS],
                          "inputSchema": schema})
        with self._lock:
            self._load_cache()[name] = {"sig": _spec_sig(row), "tools": clean, "at": time.time()}
        self._save_cache()

    def _exposed(self, rows):
        """[(exposed name, server, real tool name, tool dict)] in a stable order."""
        out, taken = [], set()
        cache = self._load_cache()
        for name in sorted(rows):
            entry = cache.get(name) or {}
            if entry.get("sig") != _spec_sig(rows[name]):
                continue
            for t in entry.get("tools") or []:
                ex = exposed_name(name, t["name"], taken)
                taken.add(ex)
                out.append((ex, name, t["name"], t))
        return out

    def list_tools(self):
        """The cached tools of every registered server, as MCP tool dicts with
        prefixed names. Never starts a server. [] while switched off."""
        if not self.enabled():
            return []
        try:
            rows = self.store().servers()
            return [{"name": ex,
                     "description": ("[%s, via the hub] " % server) + (t.get("description") or ""),
                     "inputSchema": t.get("inputSchema") or {"type": "object"}}
                    for ex, server, _real, t in self._exposed(rows)]
        except Exception:                                        # noqa: BLE001
            _log.debug("[mcp-gateway] list failed", exc_info=True)
            return []

    # -- connections --
    def _which_fn(self):
        return self._which or shutil.which

    def _connect(self, name, row):
        if row.get("transport") == "http":
            token = self.store().secret(row.get("token")) if row.get("token") else None
            if row.get("token") and not token:
                raise GatewayError("secret", "the stored token cannot be read; add it again")
            return _HttpConn(row["url"], token, self._http_post or _default_http_post)
        command = row.get("command") or ""
        path = self._which_fn()(command)
        if not path:
            raise GatewayError("not_found", "command not found on this computer: %s" % command)
        args = list(row.get("args") or [])
        if os.name == "nt" and path.lower().endswith((".cmd", ".bat")):
            if any(_CMD_META_RE.search(a) for a in args):
                raise GatewayError("bad_args", "an argument has a character cmd.exe would "
                                               "interpret (& | < > ^ %% \" !)")
        env = dict(os.environ)
        if self._env_filter:
            try:
                env = self._env_filter(env) or env
            except Exception:                                    # noqa: BLE001
                pass
        for k, v in (row.get("env") or {}).items():
            val = self.store().secret(v)
            if val is None:
                raise GatewayError("secret", "the stored value of %s cannot be read; add it "
                                             "again" % k)
            env[k] = val
        cwd = self._cwd or os.path.expanduser("~")
        try:
            proc = (self._spawn or _default_spawn)([path] + args, env, cwd)
        except GatewayError:
            raise
        except Exception as exc:                                 # noqa: BLE001
            raise GatewayError("spawn", "could not start the server (%s)" % type(exc).__name__)
        return _StdioConn(proc, self._kill or _kill_tree)

    def _make_room(self, name):
        """Stop the least recently used IDLE stdio server when MAX_PROCS run."""
        with self._lock:
            live = [n for n, c in self._conns.items()
                    if c.transport == "stdio" and c.alive and n != name]
            if len(live) < self.max_procs:
                return
            idle = sorted((self._used.get(n, 0.0), n) for n in live if not self._busy.get(n))
            if not idle:
                raise GatewayError("busy", "%d MCP servers are busy; try again in a moment"
                                   % len(live))
            victim = idle[0][1]
        self._stop(victim)

    def _ensure(self, name, row):
        """A live, initialized connection for `name` (started on demand)."""
        with self._lock:
            conn = self._conns.get(name)
            if conn is not None and conn.alive:
                return conn
            lock = self._starting.setdefault(name, threading.Lock())
        with lock:
            with self._lock:
                conn = self._conns.get(name)
                if conn is not None and conn.alive:
                    return conn
            if conn is not None:
                # Its reader ended but the process may still run: stop it (whole
                # tree) before a new one replaces it, or nothing would track it.
                self._stop(name)
            if row.get("transport") == "stdio":
                self._make_room(name)
            with self._lock:
                self._status[name] = {"state": "starting", "error": ""}
            conn = None
            try:
                conn = self._connect(name, row)
                init = conn.request("initialize", {
                    "protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                    "clientInfo": CLIENT_INFO}, INIT_TIMEOUT)
                if "error" in init:
                    raise GatewayError("init", "the server refused to start a session")
                res = init.get("result") if isinstance(init.get("result"), dict) else {}
                if isinstance(res.get("protocolVersion"), str) and hasattr(conn, "version"):
                    conn.version = res["protocolVersion"][:20]
                conn.notify("notifications/initialized")
                tools = self._fetch_tools(conn)
            except Exception as exc:
                if conn is not None:
                    conn.close()
                msg = exc.message if isinstance(exc, GatewayError) else \
                    "could not start (%s)" % type(exc).__name__
                with self._lock:
                    self._status[name] = {"state": "error", "error": str(msg)[:300]}
                _log.info("[mcp-gateway] %s: %s", name, str(msg)[:200])
                raise
            self._remember_tools(name, row, tools)
            with self._lock:
                self._conns[name] = conn
                self._used[name] = self._clock()
                self._status[name] = {"state": "ok", "error": ""}
            self._ensure_reaper()
            _log.info("[mcp-gateway] %s started (%s, %d tools)", name, conn.transport, len(tools))
            return conn

    def _fetch_tools(self, conn):
        tools, cursor = [], None
        for _page in range(10):
            msg = conn.request("tools/list", {"cursor": cursor} if cursor else {}, LIST_TIMEOUT)
            if "error" in msg:
                raise GatewayError("list", "the server could not list its tools")
            res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            page = res.get("tools") if isinstance(res.get("tools"), list) else []
            tools.extend(page)
            cursor = res.get("nextCursor")
            if not cursor or len(tools) >= MAX_TOOLS_PER_SERVER:
                break
        return tools

    def refresh(self, name):
        """Start (or reuse) the server and re-read its tools. Returns the view.
        Records a plain error in the status instead of raising."""
        row = self.store().servers().get(name)
        if not isinstance(row, dict):
            return None
        try:
            with self._lock:
                conn = self._conns.get(name)
                live = conn is not None and conn.alive
            if live:
                conn.tools_changed = False
                self._remember_tools(name, row, self._fetch_tools(conn))
            else:
                self._ensure(name, row)          # starts it and records its tools
        except GatewayError as exc:
            self._fail(name, exc.message)
        except Exception as exc:                                 # noqa: BLE001
            self._fail(name, "failed (%s)" % type(exc).__name__)
        return self.server_view(name)

    def refresh_async(self, name):
        threading.Thread(target=self.refresh, args=(name,), daemon=True,
                         name="mcp-gateway-refresh").start()

    def _fail(self, name, message):
        with self._lock:
            self._status[name] = {"state": "error", "error": str(message)[:300]}
        self._stop(name)
        _log.info("[mcp-gateway] %s: %s", name, str(message)[:200])

    def call_tool(self, exposed, arguments):
        """Proxy one tools/call. None when `exposed` is not a gateway tool (or
        the gateway is switched off), else a tools/call result dict (a failure
        is a tool error with isError). Never raises."""
        if not isinstance(exposed, str) or "__" not in exposed or not self.enabled():
            return None
        try:
            rows = self.store().servers()
            match = [(s, real) for ex, s, real, _t in self._exposed(rows) if ex == exposed]
        except Exception:                                        # noqa: BLE001
            return None
        if not match:
            return None
        server, real = match[0]
        if not isinstance(arguments, dict):
            return tool_error("arguments must be an object")
        with self._lock:
            self._busy[server] = self._busy.get(server, 0) + 1
        try:
            msg = None
            for attempt in (1, 2):
                conn = self._ensure(server, rows[server])
                try:
                    msg = conn.request("tools/call", {"name": real, "arguments": arguments},
                                       CALL_TIMEOUT)
                    break
                except GatewayError as exc:
                    # A stdio server that died, or an HTTP session the server
                    # dropped: one fresh start, then give up.
                    if exc.code in ("stopped", "session") and attempt == 1:
                        self._stop(server)
                        continue
                    raise
            with self._lock:
                self._used[server] = self._clock()
                self._status[server] = {"state": "ok", "error": ""}
            if getattr(conn, "tools_changed", False):
                self.refresh_async(server)
            if "error" in msg:
                err = msg.get("error") if isinstance(msg.get("error"), dict) else {}
                return tool_error("%s refused the call: %s"
                                  % (server, str(err.get("message") or "error")[:500]))
            res = msg.get("result") if isinstance(msg.get("result"), dict) else {}
            out = {"content": _result_text(res)}
            if res.get("isError"):
                out["isError"] = True
            return out
        except GatewayError as exc:
            if exc.code in ("not_found", "spawn", "init", "secret", "auth", "unreachable"):
                self._fail(server, exc.message)
            return tool_error("%s: %s" % (server, exc.message))
        except Exception as exc:                                 # noqa: BLE001
            return tool_error("%s: the call failed (%s)" % (server, type(exc).__name__))
        finally:
            with self._lock:
                self._busy[server] = max(0, self._busy.get(server, 1) - 1)

    # -- lifetime --
    def _stop(self, name):
        with self._lock:
            conn = self._conns.pop(name, None)
            self._used.pop(name, None)
        if conn is not None:
            conn.close()
            _log.info("[mcp-gateway] %s stopped", name)

    def reap(self, now=None):
        """Stop every server idle for idle_ttl seconds. Returns their names."""
        now = self._clock() if now is None else now
        with self._lock:
            idle = [n for n, c in self._conns.items()
                    if not self._busy.get(n) and (not c.alive or
                                                  now - self._used.get(n, now) >= self.idle_ttl)]
        for n in idle:
            self._stop(n)
        return idle

    def _ensure_reaper(self):
        if not self._start_reaper:
            return
        with self._lock:
            if self._reaper is not None and self._reaper.is_alive():
                return
            self._reaper = threading.Thread(target=self._reap_loop, daemon=True,
                                            name="mcp-gateway-reaper")
            self._reaper.start()

    def _reap_loop(self):
        while True:
            time.sleep(REAP_EVERY)
            try:
                self.reap()
            except Exception:                                    # noqa: BLE001
                pass
            with self._lock:
                if not self._conns:
                    self._reaper = None
                    return

    def running(self):
        with self._lock:
            return sorted(n for n, c in self._conns.items() if c.alive)

    def shutdown(self):
        with self._lock:
            names = list(self._conns)
        for n in names:
            self._stop(n)


def catalog_view():
    return [dict(e) for e in CATALOG]


default = Gateway()
