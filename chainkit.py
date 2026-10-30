"""chainkit: real tools for smart-contract work, served as the hub's chain_* MCP tools.

WHY TOOLS AND NOT ONLY A BRIEF (owner, 2026-10-29)
--------------------------------------------------
A free model writing, testing or reviewing a Solidity project guesses three things
it could simply look up: what is installed (Foundry? slither? only inside WSL?),
what a deployed contract really contains (its verified source and compiler
settings) and what the chain says right now (a balance, a storage slot, a log).
It also has no safe place to try things against real state. These tools answer
each of those questions for EVERY CLI connected to the hub (opencode, codex,
Claude Code ...), through the hub's MCP server:

  chain_tools_status  which of forge/cast/anvil/chisel/solc/slither/aderyn/node/npx
                      are installed natively or inside WSL, with versions, plus the
                      OFFICIAL install command of each missing one. The hub never
                      installs anything.
  chain_source        verified source of a deployed contract from Sourcify (keyless
                      API v2), written under <project>/.chain-src/<chain>-<address>/;
                      the answer is a short file list + compiler settings, never the
                      source itself.
  chain_read          READ-ONLY JSON-RPC (a strict method allowlist) through a public
                      RPC per chain, rate-limited, size-capped; never a transaction.
  chain_fork_start / chain_fork_stop / chain_fork_list
                      a managed local `anvil --fork-url` on 127.0.0.1 (at most 2,
                      stopped when idle and when the hub exits, killed as a tree).
  chain_analyze       slither or aderyn with JSON output, time-boxed; the full report
                      goes under <project>/.chain-reports/, the answer is counts by
                      severity + the top findings.

PURE-ISH: every subprocess, HTTP call, clock, port probe and thread start is a
constructor argument of Kit (the module defaults are only used when none is given,
and tests/conftest.py replaces them with ones that fail loudly). Nothing here
raises out of a tool: a bad ARGUMENT raises ArgError (a ValueError, which hub_mcp
turns into JSON-RPC -32602); everything else comes back as a dict, a failure as
{"error": <plain sentence>, "code": <word>}. Nothing secret is logged: an RPC URL
never reaches a log line or an answer (a private URL can carry an API key).

Verified 2026-10-29 from the official docs:
  Sourcify   GET https://sourcify.dev/server/v2/contract/{chainId}/{address}
             ?fields=sources,compilation,abi,proxyResolution  (v1 was switched off
             2026-07-07); 404 = not verified, 400 = {customCode, message, errorId}.
  RPCs       base mainnet.base.org / sepolia.base.org (docs.base.org), arbitrum
             arb1.arbitrum.io/rpc (docs.arbitrum.io), optimism mainnet.optimism.io
             (docs.optimism.io, rate limited), polygon polygon.drpc.org
             (docs.polygon.technology), bsc bsc-dataseed.bnbchain.org
             (docs.bnbchain.org; eth_getLogs disabled there), ethereum and sepolia
             ethereum-rpc.publicnode.com / ethereum-sepolia-rpc.publicnode.com
             (ethereum.org lists no public RPC; PublicNode's own pages).
  Installers foundryup (getfoundry.sh: Git Bash or WSL, not PowerShell), aderyn's
             README (installer script, npm @cyfrin/aderyn, cyfrinup), slither
             (pip/pipx slither-analyzer).
  Reports    slither --json {success, error, results.detectors[{check, impact,
             confidence, description, elements}]}; aderyn -o x.json {issue_count,
             high_issues.issues[{title, description, detector_name, instances
             [{contract_path, line_no}]}], low_issues}.
"""
from __future__ import annotations

import atexit
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

FLAG = "chain_tools"
RPC_SETTING = "chain_rpc_urls"          # {chain name: url}, optional, owner's own RPCs
TOOLS = ("chain_tools_status", "chain_source", "chain_read", "chain_fork_start",
         "chain_fork_stop", "chain_fork_list", "chain_analyze")

SOURCIFY_BASE = "https://sourcify.dev/server"
SOURCIFY_FIELDS = "sources,compilation,abi,proxyResolution"

CHAINS = {
    "ethereum": {"id": 1, "rpc": "https://ethereum-rpc.publicnode.com"},
    "sepolia": {"id": 11155111, "rpc": "https://ethereum-sepolia-rpc.publicnode.com"},
    "base": {"id": 8453, "rpc": "https://mainnet.base.org"},
    "base-sepolia": {"id": 84532, "rpc": "https://sepolia.base.org"},
    "arbitrum": {"id": 42161, "rpc": "https://arb1.arbitrum.io/rpc"},
    "optimism": {"id": 10, "rpc": "https://mainnet.optimism.io"},
    "polygon": {"id": 137, "rpc": "https://polygon.drpc.org"},
    "bsc": {"id": 56, "rpc": "https://bsc-dataseed.bnbchain.org"},
}
_ALIASES = {"mainnet": "ethereum", "eth": "ethereum", "ethereum-mainnet": "ethereum",
            "arb": "arbitrum", "arbitrum-one": "arbitrum", "op": "optimism",
            "op-mainnet": "optimism", "matic": "polygon", "polygon-pos": "polygon",
            "bnb": "bsc", "binance": "bsc", "bnb-smart-chain": "bsc",
            "base-mainnet": "base", "basesepolia": "base-sepolia"}

READ_METHODS = ("eth_call", "eth_getBalance", "eth_getCode", "eth_getStorageAt",
                "eth_blockNumber", "eth_chainId", "eth_getTransactionByHash",
                "eth_getTransactionReceipt", "eth_getLogs", "eth_getBlockByNumber")
LOGS_MAX_RANGE = 2000                  # blocks per eth_getLogs (many public RPCs cap lower)
READS_PER_MINUTE = 60                  # per public chain; a local fork is not limited
RESULT_MAX_CHARS = 60000               # what one chain_read answer may carry
LIST_MAX_ITEMS = 200                   # log entries / block transactions kept
HTTP_TIMEOUT = 20.0
RPC_MAX_BYTES = 4 * 1024 * 1024
SOURCE_MAX_BYTES = 16 * 1024 * 1024    # the whole Sourcify answer
SOURCE_MAX_FILES = 1000
SOURCE_LIST_SHOWN = 40

FORK_MAX = 2
FORK_IDLE_SECONDS = 3600.0             # stopped after an hour with no use through the hub
FORK_READY_SECONDS = 45.0
FORK_PORTS = range(18545, 18645)
REAPER_INTERVAL = 60.0

ANALYZE_TOOLS = ("slither", "aderyn")
ANALYZE_TIMEOUT = 600.0                # the analyzer is killed (whole tree) after this
ANALYZE_WAIT = 45.0                    # how long one call waits before answering "running"
ANALYZE_MAX_RUNNING = 2
ANALYZE_TOP = 10
JOBS_MAX = 20
REPORT_MAX_BYTES = 64 * 1024 * 1024

STATUS_TTL = 300.0
STATUS_TOOLS = ("forge", "cast", "anvil", "chisel", "solc", "slither", "aderyn", "node", "npx")
VERSION_TIMEOUT = 8.0
WSL_PROBE_TIMEOUT = 30.0

SRC_DIR = ".chain-src"
REPORT_DIR = ".chain-reports"

INSTALL = {
    "foundry": ("Foundry (forge, cast, anvil, chisel): curl -L https://foundry.paradigm.xyz | bash, "
                "then run foundryup -- in WSL, Linux, macOS or Git Bash (PowerShell and cmd are "
                "not supported)."),
    "solc": ("solc: Foundry and Hardhat download the right solc by themselves. For slither on its "
             "own: pip install solc-select, then solc-select install <version> and "
             "solc-select use <version>."),
    "slither": "slither: pipx install slither-analyzer (or: python3 -m pip install slither-analyzer).",
    "aderyn": ("aderyn: curl --proto '=https' --tlsv1.2 -LsSf "
               "https://github.com/cyfrin/aderyn/releases/latest/download/aderyn-installer.sh | bash "
               "(or: npm install -g @cyfrin/aderyn, or cyfrinup)."),
    "node": ("node/npx (Hardhat): Node.js LTS from https://nodejs.org; Hardhat is installed per "
             "project: npm install --save-dev hardhat."),
}
_GROUP = {"forge": "foundry", "cast": "foundry", "anvil": "foundry", "chisel": "foundry",
          "solc": "solc", "slither": "slither", "aderyn": "aderyn", "node": "node", "npx": "node"}

_ADDR_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
_HASH_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
_HEX_DATA_RE = re.compile(r"^0x(?:[0-9a-fA-F]{2})*$")
_QTY_RE = re.compile(r"^0x(?:0|[1-9a-fA-F][0-9a-fA-F]*)$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CHAIN_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_TAGS = ("latest", "earliest", "pending", "safe", "finalized")
_VER_RE = re.compile(r"(\d+\.\d+\.\d+)")
_URL_RE = re.compile(r"\b(?:https?|wss?)://\S+", re.I)
_WIN_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {"COM%d" % i for i in range(1, 10)} | \
    {"LPT%d" % i for i in range(1, 10)}


class ArgError(ValueError):
    """A malformed tool ARGUMENT: hub_mcp answers JSON-RPC -32602 with this text."""


# --------------------------------------------------------------------------- #
# Module defaults: real effects. tests/conftest.py replaces every one of them
# with a function that fails loudly, so no test starts anvil or touches the net.
# --------------------------------------------------------------------------- #

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_a, **_k):                       # noqa: D401
        return None


def _is_loopback_url(url):
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    return host in ("127.0.0.1", "localhost", "::1")


def _default_http(method, url, body=None, headers=None, timeout=HTTP_TIMEOUT,
                  max_bytes=RPC_MAX_BYTES):
    """(status, bytes) -- never follows a redirect, never reads past max_bytes+1,
    and never goes through a proxy for a loopback URL (a local fork)."""
    handlers = [_NoRedirect()]
    if _is_loopback_url(url):
        handlers.append(urllib.request.ProxyHandler({}))
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(url, data=body, method=method, headers=dict(headers or {}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read(max_bytes + 1)
    except urllib.error.HTTPError as exc:
        try:
            data = exc.read(max_bytes + 1)
        except Exception:                                        # noqa: BLE001
            data = b""
        return exc.code, data


def _no_window():
    return getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0


def _default_kill(proc):
    """Kill a process and everything it started; never by a stale pid."""
    try:
        if proc is None or proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=15, creationflags=_no_window())
        else:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            except Exception:                                    # noqa: BLE001
                proc.terminate()
        try:
            proc.wait(5)
        except Exception:                                        # noqa: BLE001
            proc.kill()
    except Exception:                                            # noqa: BLE001
        pass


def _popen_kwargs():
    kw = {"stdin": subprocess.DEVNULL}
    if os.name == "nt":
        kw["creationflags"] = _no_window() | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    else:
        kw["start_new_session"] = True
    return kw


def _default_run(argv, timeout, cwd=None):
    """(returncode, stdout+stderr text) or None; the whole tree is killed at timeout."""
    try:
        proc = subprocess.Popen(list(argv), cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, **_popen_kwargs())
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        _default_kill(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:                                        # noqa: BLE001
            out = b""
        return -9, (out or b"").decode("utf-8", "replace") + "\n[timed out]"
    return proc.returncode, (out or b"").decode("utf-8", "replace")


def _default_spawn(argv, log_path):
    """A long-running process (anvil), its output going to a log file."""
    log = open(log_path, "wb")
    try:
        return subprocess.Popen(list(argv), stdout=log, stderr=subprocess.STDOUT,
                                **_popen_kwargs())
    finally:
        log.close()


def _default_which(name):
    return shutil.which(name)


def _default_port_free(port):
    """Nothing listens on 127.0.0.1:port and it can be bound."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return False
    except OSError:
        pass
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def _default_start_thread(target):
    t = threading.Thread(target=target, daemon=True, name="chainkit")
    t.start()
    return t


def _default_wsl_available():
    return os.name == "nt" and bool(shutil.which("wsl.exe") or shutil.which("wsl"))


# --------------------------------------------------------------------------- #
# Small pure helpers
# --------------------------------------------------------------------------- #

def _is_link(path):
    try:
        if os.path.islink(path):
            return True
        isj = getattr(os.path, "isjunction", None)
        return bool(isj and isj(path))
    except OSError:
        return True


def _real(path):
    return os.path.normcase(os.path.realpath(os.path.abspath(str(path))))


def _inside(root, path):
    try:
        a, b = _real(root), _real(path)
        return os.path.commonpath([a, b]) == a
    except (ValueError, OSError, TypeError):
        return False


def scrub(text, limit=1500):
    """Output safe to hand to a model: URLs (which may carry API keys) removed,
    the tail kept."""
    s = _URL_RE.sub("<url>", str(text or ""))
    s = re.sub(r"\x1b\[[0-9;?]*[ -/]*[@-~]", "", s)
    return s[-limit:].strip()


def parse_version(text):
    m = _VER_RE.search(str(text or ""))
    return m.group(1) if m else None


def safe_rel(raw):
    """A source path from a Sourcify answer as a safe RELATIVE path, or None.
    The paths are chosen by whoever verified the contract: '..', absolute paths,
    drive letters, odd characters and Windows device names never reach the disk."""
    p = str(raw or "").replace("\\", "/")
    p = re.sub(r"^[A-Za-z]:", "", p)
    parts = []
    for seg in p.split("/"):
        seg = seg.strip()
        if seg in ("", ".", ".."):
            continue
        seg = re.sub(r"[^A-Za-z0-9._@+=,-]", "_", seg)[:120].rstrip(". ")
        if not seg:
            seg = "_"
        if seg.startswith("."):
            seg = "_" + seg[1:]
        if seg.split(".")[0].upper() in _WIN_RESERVED:
            seg = "_" + seg
        parts.append(seg)
    parts = parts[-12:]
    return "/".join(parts) or None


def windows_to_wsl(path):
    """C:\\x\\y -> /mnt/c/x/y (the default WSL automount), or None."""
    m = re.match(r"^([A-Za-z]):[\\/]*(.*)$", str(path or ""))
    if not m:
        return None
    rest = m.group(2).replace("\\", "/").strip("/")
    return "/mnt/%s/%s" % (m.group(1).lower(), rest) if rest else "/mnt/%s" % m.group(1).lower()


def _wsl_parse(path):
    try:
        import wslpath
        return wslpath.parse(path)
    except Exception:                                            # noqa: BLE001
        return None


def _valid_rpc_url(url):
    if not isinstance(url, str) or not url.strip() or re.search(r"\s", url.strip()):
        return False
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return False
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and _is_loopback_url(url)


# --------------------------------------------------------------------------- #
# Argument validation (-> ArgError)
# --------------------------------------------------------------------------- #

def _req_str(args, key, required=True):
    v = args.get(key)
    if v is None or (isinstance(v, str) and not v.strip()):
        if required:
            raise ArgError("'%s' must be a non-empty string" % key)
        return None
    if isinstance(v, bool) or not isinstance(v, (str, int)):
        raise ArgError("'%s' must be a string" % key)
    return str(v).strip()


def _address(v, key="address"):
    if not isinstance(v, str) or not _ADDR_RE.match(v.strip()):
        raise ArgError("'%s' must be a 0x-prefixed 20-byte hex address" % key)
    return v.strip()


def _hash(v, key):
    if not isinstance(v, str) or not _HASH_RE.match(v.strip()):
        raise ArgError("'%s' must be a 0x-prefixed 32-byte hex hash" % key)
    return v.strip()


def _quantity(v, key, max_hex=64):
    if isinstance(v, bool):
        raise ArgError("'%s' must be a number" % key)
    if isinstance(v, int):
        if v < 0:
            raise ArgError("'%s' must not be negative" % key)
        return hex(v)
    if isinstance(v, str):
        s = v.strip()
        if s.isdigit():
            return hex(int(s))
        if _QTY_RE.match(s) and len(s) - 2 <= max_hex:
            return s.lower()
    raise ArgError("'%s' must be a number or a 0x hex quantity" % key)


def _block(v, key="block"):
    if isinstance(v, str) and v.strip().lower() in _TAGS:
        return v.strip().lower()
    return _quantity(v, key, max_hex=16)


def _block_number(v, key):
    """A concrete block number (eth_getLogs bounds): int."""
    q = _quantity(v, key, max_hex=16)
    return int(q, 16)


def _hex_data(v, key, max_chars=200000):
    if not isinstance(v, str) or not _HEX_DATA_RE.match(v.strip()) or len(v) > max_chars:
        raise ArgError("'%s' must be 0x-prefixed hex bytes" % key)
    return v.strip()


def _read_params(method, params):
    if params is None:
        params = []
    if not isinstance(params, list):
        raise ArgError("'params' must be a list (the JSON-RPC params array)")
    n = len(params)

    def need(lo, hi):
        if not lo <= n <= hi:
            raise ArgError("%s takes %s params" % (method, lo if lo == hi else "%d-%d" % (lo, hi)))

    if method in ("eth_blockNumber", "eth_chainId"):
        need(0, 0)
        return []
    if method in ("eth_getBalance", "eth_getCode"):
        need(1, 2)
        return [_address(params[0], "params[0]"), _block(params[1] if n > 1 else "latest", "params[1]")]
    if method == "eth_getStorageAt":
        need(2, 3)
        return [_address(params[0], "params[0]"), _quantity(params[1], "params[1]"),
                _block(params[2] if n > 2 else "latest", "params[2]")]
    if method in ("eth_getTransactionByHash", "eth_getTransactionReceipt"):
        need(1, 1)
        return [_hash(params[0], "params[0]")]
    if method == "eth_getBlockByNumber":
        need(1, 2)
        full = params[1] if n > 1 else False
        if not isinstance(full, bool):
            raise ArgError("params[1] must be true or false")
        return [_block(params[0], "params[0]"), full]
    if method == "eth_call":
        need(1, 2)                                     # no state-override third param
        call = params[0]
        if not isinstance(call, dict):
            raise ArgError("params[0] must be a call object {to, data}")
        allowed = {"to", "data", "input", "from", "value", "gas", "gasPrice"}
        extra = set(call) - allowed
        if extra:
            raise ArgError("call object keys not allowed: %s" % ", ".join(sorted(extra)))
        out = {"to": _address(call.get("to"), "params[0].to")}
        for k in ("data", "input"):
            if call.get(k) is not None:
                out[k] = _hex_data(call[k], "params[0].%s" % k)
        if call.get("from") is not None:
            out["from"] = _address(call["from"], "params[0].from")
        for k in ("value", "gas", "gasPrice"):
            if call.get(k) is not None:
                out[k] = _quantity(call[k], "params[0].%s" % k)
        return [out, _block(params[1] if n > 1 else "latest", "params[1]")]
    if method == "eth_getLogs":
        need(1, 1)
        flt = params[0]
        if not isinstance(flt, dict):
            raise ArgError("params[0] must be a filter object")
        allowed = {"address", "topics", "fromBlock", "toBlock", "blockHash"}
        extra = set(flt) - allowed
        if extra:
            raise ArgError("filter keys not allowed: %s" % ", ".join(sorted(extra)))
        out = {}
        addr = flt.get("address")
        if addr is not None:
            if isinstance(addr, list):
                if not addr or len(addr) > 20:
                    raise ArgError("filter address list must hold 1-20 addresses")
                out["address"] = [_address(a, "address") for a in addr]
            else:
                out["address"] = _address(addr, "address")
        topics = flt.get("topics")
        if topics is not None:
            if not isinstance(topics, list) or len(topics) > 4:
                raise ArgError("topics must be a list of at most 4 entries")
            tt = []
            for t in topics:
                if t is None:
                    tt.append(None)
                elif isinstance(t, list):
                    if len(t) > 10:
                        raise ArgError("a topic alternative list holds at most 10 hashes")
                    tt.append([_hash(x, "topic") for x in t])
                else:
                    tt.append(_hash(t, "topic"))
            out["topics"] = tt
        if flt.get("blockHash") is not None:
            if flt.get("fromBlock") is not None or flt.get("toBlock") is not None:
                raise ArgError("use blockHash OR fromBlock/toBlock, not both")
            out["blockHash"] = _hash(flt["blockHash"], "blockHash")
            return [out]
        if flt.get("fromBlock") is None or flt.get("toBlock") is None:
            raise ArgError("eth_getLogs needs numeric fromBlock and toBlock (at most %d blocks "
                           "apart) or a blockHash" % LOGS_MAX_RANGE)
        lo, hi = _block_number(flt["fromBlock"], "fromBlock"), _block_number(flt["toBlock"], "toBlock")
        if hi < lo:
            raise ArgError("toBlock must not be below fromBlock")
        if hi - lo > LOGS_MAX_RANGE:
            raise ArgError("eth_getLogs range is limited to %d blocks" % LOGS_MAX_RANGE)
        out["fromBlock"], out["toBlock"] = hex(lo), hex(hi)
        return [out]
    # Second layer: Kit.validate already refused any method outside READ_METHODS.
    raise ArgError("method not allowed")


# --------------------------------------------------------------------------- #
# MCP schemas
# --------------------------------------------------------------------------- #

def tool_schemas():
    chain_desc = ("Chain name (%s) or numeric chain id." % ", ".join(CHAINS))
    proj = {"type": "string", "description": "Absolute path of the project folder."}
    return [
        {"name": "chain_tools_status",
         "description": ("Which smart-contract tools are installed on this machine, natively or "
                         "inside WSL: forge, cast, anvil, chisel, solc, slither, aderyn, node/npx "
                         "(Hardhat), with versions, plus the official install command of each "
                         "missing one. Read-only. The hub never installs anything: tell the user "
                         "the command."),
         "inputSchema": {"type": "object", "properties": {
             "refresh": {"type": "boolean", "description": "Look again instead of the 5-minute cache."}}}},
        {"name": "chain_source",
         "description": ("Download the VERIFIED source code of a deployed contract from Sourcify "
                         "(no key needed) into <project_dir>/.chain-src/<chain>-<address>/ and "
                         "return the file list and compiler settings -- read the files, the "
                         "source is not returned. A proxy is reported with its implementation "
                         "address(es): fetch those too. Nothing is sent to the chain."),
         "inputSchema": {"type": "object", "properties": {
             "address": {"type": "string", "description": "0x... contract address."},
             "chain": {"type": "string", "description": chain_desc},
             "project_dir": proj},
             "required": ["address", "chain", "project_dir"]}},
        {"name": "chain_read",
         "description": ("READ-ONLY JSON-RPC call to a chain through a public RPC, or to a local "
                         "fork (pass the fork id from chain_fork_start as chain). Allowed methods: "
                         + ", ".join(READ_METHODS) + ". Never sends or signs a transaction. "
                         "eth_getLogs needs fromBlock/toBlock at most %d blocks apart, or a "
                         "blockHash. Large results are cut." % LOGS_MAX_RANGE),
         "inputSchema": {"type": "object", "properties": {
             "chain": {"type": "string", "description": chain_desc + " Or a fork id."},
             "method": {"type": "string", "enum": list(READ_METHODS)},
             "params": {"type": "array", "description": "The JSON-RPC params array."}},
             "required": ["chain", "method"]}},
        {"name": "chain_fork_start",
         "description": ("Start a LOCAL fork of a chain with anvil on 127.0.0.1 and return its RPC "
                         "URL: test against real deployed state (forge test --fork-url <url>, "
                         "cast call --rpc-url <url>) without touching the real chain. At most %d "
                         "at once; a fork stops by itself after %d minutes without use through "
                         "the hub tools, and when the hub exits. block pins the fork."
                         % (FORK_MAX, int(FORK_IDLE_SECONDS // 60))),
         "inputSchema": {"type": "object", "properties": {
             "chain": {"type": "string", "description": chain_desc},
             "block": {"type": "integer", "minimum": 0, "description": "Fork at this block."}},
             "required": ["chain"]}},
        {"name": "chain_fork_stop",
         "description": "Stop a local fork started by chain_fork_start.",
         "inputSchema": {"type": "object", "properties": {
             "id": {"type": "string", "description": "The fork id."}}, "required": ["id"]}},
        {"name": "chain_fork_list",
         "description": "The running local forks (id, chain, block, local RPC URL). Counts as use.",
         "inputSchema": {"type": "object", "properties": {}}},
        {"name": "chain_analyze",
         "description": ("Run a static analyzer (slither or aderyn) on a Solidity project, "
                         "time-boxed. The full JSON report goes to <project_dir>/.chain-reports/; "
                         "the answer is counts by severity and the top findings. A long run "
                         "continues in the background: call again with the job_id. Findings are "
                         "leads to confirm by reading the code and writing a test, not proof."),
         "inputSchema": {"type": "object", "properties": {
             "project_dir": proj,
             "tool": {"type": "string", "enum": list(ANALYZE_TOOLS),
                      "description": "Default slither."},
             "job_id": {"type": "string", "description": "A running analysis to check on."}}}},
    ]


# --------------------------------------------------------------------------- #
# The kit
# --------------------------------------------------------------------------- #

class Kit:
    def __init__(self, *, http=None, run=None, spawn=None, kill=None, which=None,
                 clock=None, wall=None, sleep=None, port_free=None, start_thread=None,
                 wsl_available=None, windows=None, settings=None, guard=None,
                 hub_ports=None, log=None, analyze_wait=None, tmp_dir=None):
        self._http_fn, self._run_fn, self._spawn_fn, self._kill_fn = http, run, spawn, kill
        self._which_fn, self._port_free_fn, self._thread_fn = which, port_free, start_thread
        self._wsl_fn = wsl_available
        self._clock = clock or time.monotonic
        self._wall = wall or time.time
        self._sleep = sleep or time.sleep
        self._windows = (os.name == "nt") if windows is None else bool(windows)
        self._settings = settings
        self._guard = guard
        self._hub_ports = hub_ports
        self._log_fn = log
        self._analyze_wait = ANALYZE_WAIT if analyze_wait is None else float(analyze_wait)
        self._tmp_dir = tmp_dir
        self._lock = threading.RLock()
        self._forks = {}
        self._pending = {}      # fork id -> {"chain", "block", "port"}: starts not up yet
        self._jobs = {}
        self._reads = {}
        self._status = None
        self._status_at = 0.0
        self._reaper = False

    def configure(self, **kw):
        """app.py wires the folder guard, the settings reader, the hub ports and
        the logger after import."""
        for key in ("settings", "guard", "hub_ports"):
            if key in kw:
                setattr(self, "_" + key, kw[key])
        if "log" in kw:
            self._log_fn = kw["log"]

    # ---- effects (module defaults resolved at CALL time) ------------------
    def _http(self, *a, **k):
        return (self._http_fn or _default_http)(*a, **k)

    def _run(self, argv, timeout, cwd=None):
        return (self._run_fn or _default_run)(argv, timeout, cwd)

    def _spawn(self, argv, log_path):
        return (self._spawn_fn or _default_spawn)(argv, log_path)

    def _kill(self, proc):
        try:
            (self._kill_fn or _default_kill)(proc)
        except Exception:                                        # noqa: BLE001
            pass

    def _which(self, name):
        try:
            return (self._which_fn or _default_which)(name)
        except Exception:                                        # noqa: BLE001
            return None

    def _port_free(self, port):
        try:
            return bool((self._port_free_fn or _default_port_free)(port))
        except Exception:                                        # noqa: BLE001
            return False

    def _start_thread(self, target):
        return (self._thread_fn or _default_start_thread)(target)

    def _wsl_ok(self):
        if not self._windows:
            return False
        try:
            return bool((self._wsl_fn or _default_wsl_available)())
        except Exception:                                        # noqa: BLE001
            return False

    def _log(self, msg):
        try:
            if self._log_fn:
                self._log_fn(msg)
        except Exception:                                        # noqa: BLE001
            pass

    def _setting(self, key):
        try:
            return self._settings(key) if self._settings else None
        except Exception:                                        # noqa: BLE001
            return None

    def _check_folder(self, folder):
        """None when the tools may write into / run in `folder`, else the reason.
        Without a wired guard (tests of other modules, other processes) nothing is
        allowed: fail closed."""
        if self._guard is None:
            return "Project folders are not available in this hub."
        try:
            reason = self._guard(folder)
        except Exception:                                        # noqa: BLE001
            return "Could not check that folder."
        return reason or None

    # ---- dispatcher --------------------------------------------------------
    def call(self, name, arguments):
        """Validate (ArgError on a bad argument), then run. Never raises otherwise."""
        if not isinstance(arguments, dict):
            raise ArgError("'arguments' must be an object")
        kwargs = self.validate(name, arguments)
        fn = {"chain_tools_status": self.status, "chain_source": self.source,
              "chain_read": self.read, "chain_fork_start": self.fork_start,
              "chain_fork_stop": self.fork_stop, "chain_fork_list": self.fork_list,
              "chain_analyze": self.analyze}[name]
        try:
            out = fn(**kwargs)
        except Exception as exc:                                 # noqa: BLE001
            self._log("[chain] %s failed: %s" % (name, type(exc).__name__))
            return {"error": "%s failed (%s)." % (name, type(exc).__name__), "code": "failed"}
        return out if isinstance(out, dict) else {"result": out}

    def validate(self, name, args):
        if name not in TOOLS:
            raise ArgError("Unknown tool: %r" % (name,))
        if name == "chain_tools_status":
            refresh = args.get("refresh", False)
            if not isinstance(refresh, bool):
                raise ArgError("'refresh' must be true or false")
            return {"refresh": refresh}
        if name == "chain_source":
            return {"address": _address(args.get("address")),
                    "chain": self._chain_for_source(_req_str(args, "chain")),
                    "project_dir": _req_str(args, "project_dir")}
        if name == "chain_read":
            method = _req_str(args, "method")
            if method not in READ_METHODS:
                raise ArgError("method %r is not allowed: chain_read is read-only (allowed: %s)"
                               % (method, ", ".join(READ_METHODS)))
            chain = self._chain_for_read(_req_str(args, "chain"))
            return {"chain": chain, "method": method,
                    "params": _read_params(method, args.get("params"))}
        if name == "chain_fork_start":
            chain = self._chain_for_read(_req_str(args, "chain"), forks=False)
            block = args.get("block")
            if block is not None:
                block = int(_quantity(block, "block", max_hex=16), 16)
            return {"chain": chain, "block": block}
        if name == "chain_fork_stop":
            fid = _req_str(args, "id")
            if not _ID_RE.match(fid):
                raise ArgError("'id' is not a fork id")
            return {"fork_id": fid}
        if name == "chain_fork_list":
            return {}
        # chain_analyze
        job = _req_str(args, "job_id", required=False)
        if job is not None:
            if not _ID_RE.match(job):
                raise ArgError("'job_id' is not a job id")
            return {"job_id": job}
        tool = _req_str(args, "tool", required=False) or "slither"
        if tool not in ANALYZE_TOOLS:
            raise ArgError("'tool' must be one of: %s" % ", ".join(ANALYZE_TOOLS))
        return {"project_dir": _req_str(args, "project_dir"), "tool": tool}

    # ---- chains ------------------------------------------------------------
    def _overrides(self):
        raw = self._setting(RPC_SETTING)
        out = {}
        if isinstance(raw, dict):
            for k, v in raw.items():
                key = str(k).strip().lower()
                if _CHAIN_NAME_RE.match(key) and _valid_rpc_url(v):
                    out[_ALIASES.get(key, key)] = v.strip()
        return out

    def _known_name(self, value):
        s = str(value).strip().lower()
        s = _ALIASES.get(s, s)
        if s in CHAINS:
            return s
        if s.isdigit():
            for k, v in CHAINS.items():
                if v["id"] == int(s):
                    return k
        return None

    def _chain_for_source(self, value):
        """(label, chain id): any known chain or any numeric chain id Sourcify knows."""
        name = self._known_name(value)
        if name:
            return name, CHAINS[name]["id"]
        s = str(value).strip()
        if s.isdigit() and 0 < int(s) < 2 ** 53:
            return s, int(s)
        raise ArgError("unknown chain %r (known: %s, or a numeric chain id)"
                       % (value, ", ".join(CHAINS)))

    def _chain_for_read(self, value, forks=True):
        """A chain the hub has an RPC for (a default or the owner's setting), or a
        running fork id."""
        s = str(value).strip()
        if forks:
            with self._lock:
                if s in self._forks:
                    return s
        name = self._known_name(s)
        if name:
            return name
        low = s.lower()
        if low in self._overrides():
            return low
        raise ArgError("unknown chain %r (known: %s%s)" % (
            value, ", ".join(CHAINS), ", or a fork id from chain_fork_start" if forks else ""))

    def _rpc_for(self, chain):
        """(url, label) of the upstream RPC of a chain name."""
        over = self._overrides()
        if chain in over:
            return over[chain], "your RPC (setting %s)" % RPC_SETTING
        return CHAINS[chain]["rpc"], "public RPC"

    # ---- chain_tools_status ------------------------------------------------
    def status(self, refresh=False):
        snap = self._tool_snapshot(refresh)
        tools, missing = {}, []
        for t in STATUS_TOOLS:
            row = snap.get(t) or {}
            if row:
                tools[t] = row
            else:
                tools[t] = "missing"
                missing.append(t)
        install = {}
        for t in missing:
            g = _GROUP[t]
            if g == "solc" and (snap.get("forge") or snap.get("node")):
                continue          # Foundry/Hardhat fetch solc themselves
            install[g] = INSTALL[g]
        out = {"tools": tools, "wsl_checked": bool(snap.get("_wsl_checked")),
               "note": ("The hub never installs anything: give the user the command, they run "
                        "it. A tool found only in WSL runs there (wsl -e bash -lic '...').")}
        if install:
            out["install"] = install
        return out

    def _tool_snapshot(self, refresh=False):
        with self._lock:
            if (not refresh and self._status is not None
                    and self._clock() - self._status_at < STATUS_TTL):
                return self._status
        snap = {}
        lock = threading.Lock()

        def one(t):
            path = self._which(t)
            if not path:
                return
            got = self._run([path, "--version"], VERSION_TIMEOUT)
            if not got or got[0] != 0:
                return
            with lock:
                snap[t] = {"native": parse_version(got[1]) or "?", "path": path}

        threads = [threading.Thread(target=one, args=(t,), daemon=True) for t in STATUS_TOOLS]
        for th in threads:
            th.start()
        for th in threads:
            th.join(VERSION_TIMEOUT + 2)
        if self._wsl_ok():
            snap["_wsl_checked"] = True
            for t, ver in self._wsl_versions(None).items():
                row = dict(snap.get(t) or {})
                row["wsl"] = ver or "?"
                snap[t] = row
        with self._lock:
            self._status, self._status_at = snap, self._clock()
        return snap

    def _wsl_versions(self, distro, tools=STATUS_TOOLS):
        """{tool: version or None} for the tools found inside WSL (one call)."""
        script = ("for t in %s; do if command -v \"$t\" >/dev/null 2>&1; then "
                  "printf '@@%%s\\t%%s\\n' \"$t\" \"$(\"$t\" --version 2>&1 | tr '\\n' ' ' | "
                  "cut -c1-160)\"; fi; done" % " ".join(tools))
        argv = ["wsl.exe"] + (["-d", distro] if distro else []) + ["-e", "bash", "-lic", script]
        got = self._run(argv, WSL_PROBE_TIMEOUT)
        out = {}
        if not got:
            return out
        for line in str(got[1] or "").splitlines():
            m = re.match(r"^@@([a-z]+)\t(.*)$", line.strip("\r"))
            if m and m.group(1) in tools:
                out[m.group(1)] = parse_version(m.group(2))
        return out

    def _locate(self, tool, project_dir=None):
        """("native", path) | ("wsl", distro or None) | None for running `tool`.
        A project inside a WSL distro runs its tools in that distro."""
        wsl_proj = _wsl_parse(project_dir) if project_dir else None
        if wsl_proj:
            if not self._wsl_ok():
                return None
            found = self._wsl_versions(wsl_proj[0], (tool,))
            return ("wsl", wsl_proj[0]) if tool in found else None
        snap = self._tool_snapshot()
        row = snap.get(tool) or {}
        if row.get("native") and row.get("path"):
            return "native", row["path"]
        if row.get("wsl"):
            return "wsl", None
        return None

    # ---- chain_source ------------------------------------------------------
    def source(self, address, chain, project_dir):
        label, cid = chain
        reason = self._check_folder(project_dir)
        if reason:
            return {"error": reason, "code": "bad_folder"}
        url = "%s/v2/contract/%d/%s?fields=%s" % (SOURCIFY_BASE, cid, address, SOURCIFY_FIELDS)
        try:
            status, body = self._http("GET", url, None, {"Accept": "application/json",
                                                         "User-Agent": "free-llm-hub-chainkit"},
                                      HTTP_TIMEOUT, SOURCE_MAX_BYTES)
        except Exception as exc:                                 # noqa: BLE001
            return {"error": "Sourcify did not answer (%s)." % type(exc).__name__,
                    "code": "unavailable"}
        if body is not None and len(body) > SOURCE_MAX_BYTES:
            return {"error": "The verified source is larger than %d MB; not downloaded."
                    % (SOURCE_MAX_BYTES // (1024 * 1024)), "code": "too_large"}
        data = None
        try:
            data = json.loads((body or b"").decode("utf-8", "replace"))
        except ValueError:
            data = None
        if status == 404:
            return {"error": "This contract is not verified on Sourcify (chain %s, %s). Its "
                             "source is not public there; work from the bytecode "
                             "(chain_read eth_getCode) or ask the user for the source."
                             % (label, address), "code": "not_verified"}
        if status == 400:
            msg = scrub((data or {}).get("message") if isinstance(data, dict) else "", 300)
            return {"error": "Sourcify refused the request: %s" % (msg or "bad request"),
                    "code": "bad_request"}
        if status == 429:
            return {"error": "Sourcify is rate-limiting; try again in a minute.",
                    "code": "rate_limited"}
        if status != 200 or not isinstance(data, dict):
            return {"error": "Sourcify answered HTTP %s." % status, "code": "unavailable"}
        sources = data.get("sources")
        if not isinstance(sources, dict) or not sources:
            return {"error": "Sourcify returned no source files for this contract.",
                    "code": "no_sources"}
        return self._write_sources(project_dir, label, cid, address, data, sources)

    def _prepare_dir(self, project_dir, sub):
        root = os.path.realpath(os.path.abspath(project_dir))
        d = os.path.join(root, sub)
        if os.path.lexists(d) and (_is_link(d) or not os.path.isdir(d)):
            return None, "%s in the project is a link or not a folder; remove it first." % sub
        os.makedirs(d, exist_ok=True)
        if not _inside(root, d) or _is_link(d):
            return None, "%s resolves outside the project; remove it first." % sub
        gi = os.path.join(d, ".gitignore")
        if not os.path.lexists(gi):
            with open(gi, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("# local copies made by the hub's chain tools\n*\n")
        return d, None

    def _write_sources(self, project_dir, label, cid, address, data, sources):
        base, err = self._prepare_dir(project_dir, SRC_DIR)
        if err:
            return {"error": err, "code": "bad_folder"}
        target = os.path.join(base, "%s-%s" % (re.sub(r"[^a-z0-9-]", "_", label.lower()),
                                               address.lower()))
        if os.path.lexists(target) and (_is_link(target) or not os.path.isdir(target)):
            return {"error": "%s is a link or not a folder; remove it first." % target,
                    "code": "bad_folder"}
        os.makedirs(target, exist_ok=True)
        if not _inside(base, target):
            return {"error": "The download folder resolves outside the project.",
                    "code": "bad_folder"}
        written, skipped, seen = [], 0, set()
        for raw_path, entry in list(sources.items())[:SOURCE_MAX_FILES]:
            content = entry.get("content") if isinstance(entry, dict) else None
            rel = safe_rel(raw_path)
            if not isinstance(content, str) or not rel:
                skipped += 1
                continue
            key = rel.lower()
            n = 2
            while key in seen:                                   # a/X.sol vs a/x.sol on Windows
                stem, dot, ext = rel.rpartition(".")
                rel = ("%s~%d.%s" % (stem, n, ext)) if dot else "%s~%d" % (rel, n)
                key, n = rel.lower(), n + 1
            seen.add(key)
            dest = os.path.join(target, *rel.split("/"))
            parent = os.path.dirname(dest)
            os.makedirs(parent, exist_ok=True)
            if not _inside(target, parent) or (os.path.lexists(dest) and _is_link(dest)):
                skipped += 1
                continue
            with open(dest, "w", encoding="utf-8", newline="") as fh:
                fh.write(content)
            written.append(rel)
        skipped += max(0, len(sources) - SOURCE_MAX_FILES)
        comp = data.get("compilation") if isinstance(data.get("compilation"), dict) else {}
        settings = comp.get("compilerSettings") if isinstance(comp.get("compilerSettings"), dict) else {}
        opt = settings.get("optimizer") if isinstance(settings.get("optimizer"), dict) else {}
        compiler = {k: v for k, v in (
            ("language", comp.get("language")), ("compiler", comp.get("compiler")),
            ("version", comp.get("compilerVersion")), ("contract", comp.get("name")),
            ("fully_qualified_name", comp.get("fullyQualifiedName")),
            ("optimizer", {"enabled": opt.get("enabled"), "runs": opt.get("runs")} if opt else None),
            ("evm_version", settings.get("evmVersion")), ("via_ir", settings.get("viaIR")),
            ("remappings", len(settings.get("remappings") or []) or None)) if v is not None}
        proxy = None
        pr = data.get("proxyResolution")
        if isinstance(pr, dict) and pr.get("isProxy"):
            proxy = {"type": pr.get("proxyType"),
                     "implementations": [
                         {k: v for k, v in (("address", i.get("address")), ("name", i.get("name")))
                          if v} for i in (pr.get("implementations") or [])[:10]
                         if isinstance(i, dict)]}
        meta = {"chain": label, "chain_id": cid, "address": address,
                "match": data.get("match"), "runtime_match": data.get("runtimeMatch"),
                "creation_match": data.get("creationMatch"), "verified_at": data.get("verifiedAt"),
                "compilation": comp, "proxy": proxy, "files": written,
                "source": "https://sourcify.dev (API v2)"}
        with open(os.path.join(target, "chain-source.json"), "w", encoding="utf-8",
                  newline="\n") as fh:
            json.dump(meta, fh, indent=1, ensure_ascii=False)
        if isinstance(data.get("abi"), list):
            with open(os.path.join(target, "abi.json"), "w", encoding="utf-8", newline="\n") as fh:
                json.dump(data["abi"], fh, indent=1, ensure_ascii=False)
        self._log("[chain] source of %s on %s written (%d files)" % (address, label, len(written)))
        out = {"chain": label, "chain_id": cid, "address": address, "folder": target,
               "match": data.get("match"), "files": len(written),
               "file_list": written[:SOURCE_LIST_SHOWN], "compiler": compiler,
               "note": ("Read the files from the folder (compiler settings in chain-source.json, "
                        "ABI in abi.json). It is a local copy, ignored by git.")}
        if len(written) > SOURCE_LIST_SHOWN:
            out["more_files"] = len(written) - SOURCE_LIST_SHOWN
        if skipped:
            out["skipped_files"] = skipped
        if proxy:
            out["proxy"] = proxy
            out["note"] += (" This is a PROXY: the logic lives in the implementation; fetch it "
                            "with chain_source on the implementation address.")
        return out

    # ---- chain_read --------------------------------------------------------
    def _rate_ok(self, chain):
        now = self._clock()
        with self._lock:
            q = [t for t in self._reads.get(chain, []) if now - t < 60.0]
            if len(q) >= READS_PER_MINUTE:
                self._reads[chain] = q
                return max(1, int(60.0 - (now - q[0])) + 1)
            q.append(now)
            self._reads[chain] = q
        return 0

    def read(self, chain, method, params):
        with self._lock:
            fork = self._forks.get(chain)
            if fork:
                fork["last_used"] = self._clock()
        if fork:
            url, via = fork["rpc_url"], "local fork %s" % chain
        else:
            wait = self._rate_ok(chain)
            if wait:
                return {"error": "Too many reads on %s (at most %d a minute through the hub). "
                                 "Retry in %d s, or use a local fork." % (chain, READS_PER_MINUTE, wait),
                        "code": "rate_limited", "retry_after": wait}
            url, via = self._rpc_for(chain)
        payload = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method,
                              "params": params}).encode("utf-8")
        try:
            status, body = self._http("POST", url, payload,
                                      {"Content-Type": "application/json",
                                       "Accept": "application/json",
                                       "User-Agent": "free-llm-hub-chainkit"},
                                      HTTP_TIMEOUT, RPC_MAX_BYTES)
        except Exception as exc:                                 # noqa: BLE001
            return {"error": "The %s for %s did not answer (%s)." % (via, chain, type(exc).__name__),
                    "code": "unavailable"}
        if body is not None and len(body) > RPC_MAX_BYTES:
            return {"error": "The answer is larger than %d MB; narrow the query."
                    % (RPC_MAX_BYTES // (1024 * 1024)), "code": "too_large"}
        if status == 429:
            return {"error": "The %s for %s is rate-limiting; wait a minute or set your own RPC "
                             "in the hub setting %s." % (via, chain, RPC_SETTING),
                    "code": "rate_limited"}
        try:
            data = json.loads((body or b"").decode("utf-8", "replace"))
        except ValueError:
            data = None
        if not isinstance(data, dict):
            return {"error": "The %s for %s answered HTTP %s without JSON-RPC." % (via, chain, status),
                    "code": "unavailable"}
        if data.get("error") is not None:
            err = data["error"] if isinstance(data["error"], dict) else {"message": str(data["error"])}
            out = {"error": "RPC error: %s" % scrub(err.get("message") or "unknown", 400),
                   "code": "rpc_error"}
            if isinstance(err.get("code"), int):
                out["rpc_code"] = err["code"]
            if err.get("data") is not None:
                out["rpc_data"] = scrub(json.dumps(err["data"]) if not isinstance(err["data"], str)
                                        else err["data"], 2000)
            return out
        result = data.get("result")
        out = {"chain": chain, "method": method, "via": via}
        if isinstance(result, list) and len(result) > LIST_MAX_ITEMS:
            out["truncated_items"] = len(result) - LIST_MAX_ITEMS
            result = result[:LIST_MAX_ITEMS]
        if isinstance(result, dict) and isinstance(result.get("transactions"), list) \
                and len(result["transactions"]) > LIST_MAX_ITEMS:
            result = dict(result)
            out["truncated_items"] = len(result["transactions"]) - LIST_MAX_ITEMS
            result["transactions"] = result["transactions"][:LIST_MAX_ITEMS]
        text = json.dumps(result, ensure_ascii=False)
        if len(text) > RESULT_MAX_CHARS:
            out["result_truncated"] = True
            out["result_chars"] = len(text)
            out["result_preview"] = text[:RESULT_MAX_CHARS]
        else:
            out["result"] = result
        if method in ("eth_blockNumber", "eth_chainId", "eth_getBalance") \
                and isinstance(result, str) and _QTY_RE.match(result):
            out["decimal"] = str(int(result, 16))
        return out

    # ---- forks -------------------------------------------------------------
    def _fork_view(self, f):
        idle = FORK_IDLE_SECONDS - (self._clock() - f["last_used"])
        out = {"id": f["id"], "chain": f["chain"], "chain_id": f["chain_id"],
               "rpc_url": f["rpc_url"], "port": f["port"], "runs": f["where"],
               "stops_if_idle_in_minutes": max(0, int(idle // 60))}
        if f.get("block") is not None:
            out["block"] = f["block"]
        return out

    def _hub_port_set(self):
        try:
            ports = set(self._hub_ports() if self._hub_ports else ())
        except Exception:                                        # noqa: BLE001
            ports = set()
        return ports | {8787}

    def _taken_ports(self):
        """Ports of running forks and of starts still coming up (call under the lock)."""
        return {f["port"] for f in self._forks.values()} | \
            {p["port"] for p in self._pending.values() if p.get("port") is not None}

    def _pick_port(self, fid=None):
        """A free port, reserved for the start `fid`. The socket test runs outside
        the lock; the reservation is checked again under it, so two starts at
        once never take the same port (anvil only binds once it has fetched the
        upstream state, so the socket test alone cannot see the first one)."""
        avoid = self._hub_port_set()
        while True:
            with self._lock:
                taken = self._taken_ports()
            cand = next((p for p in FORK_PORTS
                         if p not in avoid and p not in taken and self._port_free(p)), None)
            if cand is None:
                return None
            with self._lock:
                if cand not in self._taken_ports():
                    pend = self._pending.get(fid)
                    if pend is not None:
                        pend["port"] = cand
                    return cand
            avoid.add(cand)

    def fork_start(self, chain, block=None):
        self.sweep()
        fid = "fork-" + uuid.uuid4().hex[:8]
        with self._lock:
            for f in self._forks.values():
                if f["chain"] == chain and f.get("block") == block:
                    f["last_used"] = self._clock()
                    view = self._fork_view(f)
                    view["note"] = "This fork was already running; reusing it."
                    return view
            for p in self._pending.values():
                if p["chain"] == chain and p.get("block") == block:
                    return {"error": "A fork of %s is already starting; call chain_fork_start "
                                     "again in a few seconds to reuse it." % chain,
                            "code": "starting"}
            if len(self._forks) + len(self._pending) >= FORK_MAX:
                out = {"error": "At most %d local forks run at once; stop one with "
                                "chain_fork_stop." % FORK_MAX, "code": "too_many",
                       "forks": [self._fork_view(f) for f in self._forks.values()]}
                if self._pending:
                    out["starting"] = len(self._pending)
                return out
            # The slot is held from here until the fork is registered or has failed.
            self._pending[fid] = {"chain": chain, "block": block, "port": None}
        try:
            return self._fork_start(fid, chain, block)
        finally:
            with self._lock:
                self._pending.pop(fid, None)

    def _fork_start(self, fid, chain, block):
        loc = self._locate("anvil")
        if loc is None:
            return {"error": "anvil (Foundry) is not installed here, natively or in WSL.",
                    "code": "not_installed", "install": INSTALL["foundry"]}
        port = self._pick_port(fid)
        if port is None:
            return {"error": "No free local port for a fork.", "code": "no_port"}
        upstream, _via = self._rpc_for(chain)
        words = ["anvil", "--fork-url", upstream, "--host", "127.0.0.1", "--port", str(port)]
        if block is not None:
            words += ["--fork-block-number", str(block)]
        if loc[0] == "native":
            argv = [loc[1]] + words[1:]
        else:
            argv = ["wsl.exe"] + (["-d", loc[1]] if loc[1] else []) + \
                ["-e", "bash", "-lic", "exec " + shlex.join(words)]
        log_path = os.path.join(self._tmp_dir or tempfile.gettempdir(), "chainkit-%s.log" % fid)
        try:
            proc = self._spawn(argv, log_path)
        except Exception as exc:                                 # noqa: BLE001
            return {"error": "anvil could not start (%s)." % type(exc).__name__, "code": "failed"}
        local = "http://127.0.0.1:%d" % port
        want = CHAINS.get(chain, {}).get("id")      # None: a chain only the owner's setting names
        ready, wrong = False, None
        deadline = self._clock() + FORK_READY_SECONDS
        while self._clock() < deadline:
            if proc.poll() is not None:
                break
            got = self._local_chain_id(local)
            if got is not None:
                if want is None or got == want:
                    ready = True
                else:
                    wrong = got         # not our anvil, or the RPC is another chain
                break
            self._sleep(0.5)
        if wrong is not None:
            self._kill(proc)
            self._unlink(log_path)
            return {"error": "The local port answered as chain id %s, not %s (%s): another "
                             "program is listening there, or the RPC set for %s in the hub "
                             "setting %s is another chain." % (wrong, want, chain, chain,
                                                               RPC_SETTING),
                    "code": "wrong_chain"}
        if not ready:
            self._kill(proc)
            tail = ""
            try:
                with open(log_path, "rb") as fh:
                    tail = fh.read()[-4000:].decode("utf-8", "replace")
            except OSError:
                pass
            tail = scrub(tail.replace(upstream, "<rpc>"), 800)
            self._unlink(log_path)
            return {"error": "The fork did not come up in %d s (a public RPC can be slow or "
                             "refuse forking; set your own in the hub setting %s)."
                             % (int(FORK_READY_SECONDS), RPC_SETTING),
                    "code": "fork_failed", "log_tail": tail}
        now = self._clock()
        rec = {"id": fid, "chain": chain, "chain_id": CHAINS.get(chain, {}).get("id"),
               "block": block, "port": port, "rpc_url": local, "where": loc[0],
               "proc": proc, "log": log_path, "started": now, "last_used": now}
        with self._lock:
            self._pending.pop(fid, None)          # the slot moves to the running fork
            self._forks[fid] = rec
        self._ensure_reaper()
        self._log("[chain] fork %s of %s started on port %d (%s)" % (fid, chain, port, loc[0]))
        view = self._fork_view(rec)
        view["note"] = ("A local copy: transactions sent to it are not real. Use it with "
                        "forge test --fork-url %s or cast ... --rpc-url %s." % (local, local))
        return view

    def _local_chain_id(self, url):
        """The chain id `url` answers eth_chainId with (an int; the raw result when
        it is not a hex quantity), or None when nothing answered."""
        try:
            status, body = self._http("POST", url, json.dumps(
                {"jsonrpc": "2.0", "id": 1, "method": "eth_chainId", "params": []}).encode(),
                {"Content-Type": "application/json"}, 3.0, 65536)
            data = json.loads((body or b"{}").decode("utf-8", "replace"))
        except Exception:                                        # noqa: BLE001
            return None
        if status != 200 or not isinstance(data, dict) or "result" not in data:
            return None
        result = data["result"]
        if isinstance(result, str) and result[:2].lower() == "0x":
            try:
                return int(result[2:], 16)
            except ValueError:
                pass
        return scrub(str(result), 40)

    @staticmethod
    def _unlink(path):
        try:
            os.remove(path)
        except OSError:
            pass

    def _stop(self, rec, why):
        self._kill(rec.get("proc"))
        self._unlink(rec.get("log") or "")
        self._log("[chain] fork %s stopped (%s)" % (rec["id"], why))

    def fork_stop(self, fork_id):
        with self._lock:
            rec = self._forks.pop(fork_id, None)
        if rec is None:
            return {"error": "No running fork %s." % fork_id, "code": "not_found"}
        self._stop(rec, "asked")
        return {"stopped": fork_id}

    def fork_list(self):
        self.sweep()
        with self._lock:
            now = self._clock()
            for f in self._forks.values():
                f["last_used"] = now
            return {"forks": [self._fork_view(f) for f in self._forks.values()],
                    "max": FORK_MAX}

    def sweep(self):
        """Forget forks that exited; stop the ones idle for FORK_IDLE_SECONDS."""
        now = self._clock()
        gone = []
        with self._lock:
            for fid, f in list(self._forks.items()):
                proc = f.get("proc")
                try:
                    dead = proc is None or proc.poll() is not None
                except Exception:                                # noqa: BLE001
                    dead = True
                if dead:
                    gone.append((self._forks.pop(fid), "exited"))
                elif now - f["last_used"] > FORK_IDLE_SECONDS:
                    gone.append((self._forks.pop(fid), "idle"))
        for rec, why in gone:
            self._stop(rec, why)

    def _ensure_reaper(self):
        """One background thread while forks exist: it stops idle ones even when
        no agent calls a chain tool again."""
        with self._lock:
            if self._reaper:
                return
            self._reaper = True
        try:
            self._start_thread(self._reaper_loop)
        except Exception:                                        # noqa: BLE001
            with self._lock:
                self._reaper = False

    def _reaper_loop(self):
        try:
            while True:
                self._sleep(REAPER_INTERVAL)
                self.sweep()
                with self._lock:
                    if not self._forks:
                        self._reaper = False
                        return
        except Exception:                                        # noqa: BLE001
            with self._lock:
                self._reaper = False

    def shutdown(self):
        """Stop every fork (hub exit)."""
        with self._lock:
            recs = list(self._forks.values())
            self._forks.clear()
        for rec in recs:
            self._stop(rec, "hub exit")

    # ---- chain_analyze -----------------------------------------------------
    def analyze(self, project_dir=None, tool="slither", job_id=None):
        if job_id is not None:
            with self._lock:
                job = self._jobs.get(job_id)
            if job is None:
                return {"error": "No analysis job %s (finished long ago, or the hub restarted)."
                                 % job_id, "code": "not_found"}
            return self._job_wait(job, 0.0)
        reason = self._check_folder(project_dir)
        if reason:
            return {"error": reason, "code": "bad_folder"}
        with self._lock:
            running = sum(1 for j in self._jobs.values() if j["state"] == "running")
        if running >= ANALYZE_MAX_RUNNING:
            return {"error": "%d analyses are already running; wait for one (chain_analyze with "
                             "its job_id)." % running, "code": "busy"}
        loc = self._locate(tool, project_dir)
        if loc is None:
            return {"error": "%s is not installed%s." % (
                        tool, " inside that WSL distro" if _wsl_parse(project_dir) else
                        " here, natively or in WSL"),
                    "code": "not_installed", "install": INSTALL[tool]}
        reports, err = self._prepare_dir(project_dir, REPORT_DIR)
        if err:
            return {"error": err, "code": "bad_folder"}
        stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(self._wall()))
        name = "%s-%s.json" % (tool, stamp)
        n = 2
        while os.path.lexists(os.path.join(reports, name)):
            name, n = "%s-%s-%d.json" % (tool, stamp, n), n + 1
        rel = REPORT_DIR + "/" + name
        words = ([tool, ".", "--json", rel] if tool == "slither"
                 else [tool, ".", "--no-snippets", "-o", rel])
        cwd = os.path.realpath(os.path.abspath(project_dir))
        if loc[0] == "native":
            argv, run_cwd = [loc[1]] + words[1:], cwd
        else:
            wp = _wsl_parse(project_dir)
            linux = wp[1] if wp else windows_to_wsl(cwd)
            if not linux:
                return {"error": "This folder cannot be reached from WSL.", "code": "bad_folder"}
            argv = ["wsl.exe"] + (["-d", loc[1]] if loc[1] else []) + \
                ["--cd", linux, "-e", "bash", "-lic", "exec " + shlex.join(words)]
            run_cwd = None
        job = {"id": "an-" + uuid.uuid4().hex[:8], "tool": tool, "state": "running",
               "report": os.path.join(reports, name), "started": self._clock(),
               "done": threading.Event(), "result": None}
        with self._lock:
            self._jobs[job["id"]] = job
            self._evict_jobs()

        def work():
            try:
                got = self._run(argv, ANALYZE_TIMEOUT, run_cwd)
                job["result"] = self._summarize(tool, job["report"], got)
            except Exception as exc:                             # noqa: BLE001
                job["result"] = {"error": "%s failed (%s)." % (tool, type(exc).__name__),
                                 "code": "failed"}
            job["state"] = "error" if job["result"].get("error") else "done"
            job["done"].set()

        self._log("[chain] %s started on %s (%s)" % (tool, os.path.basename(cwd), loc[0]))
        try:
            self._start_thread(work)
        except Exception as exc:                                 # noqa: BLE001
            job["result"] = {"error": "Could not start %s (%s)." % (tool, type(exc).__name__),
                             "code": "failed"}
            job["state"] = "error"
            job["done"].set()
        return self._job_wait(job, self._analyze_wait)

    def _evict_jobs(self):
        while len(self._jobs) > JOBS_MAX:
            done = [(j["started"], k) for k, j in self._jobs.items() if j["state"] != "running"]
            if not done:
                return
            del self._jobs[min(done)[1]]

    def _job_wait(self, job, wait):
        if wait > 0:
            job["done"].wait(wait)
        if job["state"] == "running":
            return {"state": "running", "job_id": job["id"], "tool": job["tool"],
                    "report": job["report"],
                    "running_seconds": int(self._clock() - job["started"]),
                    "note": "Still analysing. Call chain_analyze again with this job_id."}
        out = dict(job["result"] or {})
        out.setdefault("job_id", job["id"])
        return out

    def _summarize(self, tool, report, got):
        rc, text = (got if got else (None, ""))
        try:
            size = os.path.getsize(report)
        except OSError:
            size = None
        if size is None:
            return {"error": "%s produced no report%s." % (
                        tool, " (it ran out of time)" if rc == -9 else ""),
                    "code": "no_report", "output_tail": scrub(text)}
        if size > REPORT_MAX_BYTES:
            return {"error": "The report is too large to summarise; read it yourself.",
                    "code": "too_large", "report": report}
        try:
            with open(report, "r", encoding="utf-8", errors="replace") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            return {"error": "%s wrote a report that is not JSON." % tool, "code": "bad_report",
                    "report": report, "output_tail": scrub(text)}
        if tool == "slither":
            return self._sum_slither(data, report, text)
        return self._sum_aderyn(data, report)

    @staticmethod
    def _sum_slither(data, report, text):
        if not isinstance(data, dict) or data.get("success") is False:
            msg = scrub((data or {}).get("error") if isinstance(data, dict) else "", 600)
            return {"error": "slither could not analyse the project: %s"
                    % (msg or "no detail"), "code": "analyzer_failed", "report": report,
                    "output_tail": scrub(text, 800)}
        dets = ((data.get("results") or {}).get("detectors") or [])
        dets = [d for d in dets if isinstance(d, dict)]
        rank = {"High": 0, "Medium": 1, "Low": 2, "Informational": 3, "Optimization": 4}
        conf = {"High": 0, "Medium": 1, "Low": 2}
        counts = {}
        for d in dets:
            counts[d.get("impact") or "?"] = counts.get(d.get("impact") or "?", 0) + 1
        top = []
        for d in sorted(dets, key=lambda d: (rank.get(d.get("impact"), 9),
                                             conf.get(d.get("confidence"), 9))):
            where = None
            els = d.get("elements") or []
            if els and isinstance(els[0], dict):
                sm = els[0].get("source_mapping") or {}
                f = sm.get("filename_relative") or sm.get("filename_short")
                lines = sm.get("lines") or []
                if f:
                    where = "%s:%s" % (f, lines[0]) if lines else f
            row = {"impact": d.get("impact"), "confidence": d.get("confidence"),
                   "check": d.get("check"),
                   "description": scrub((d.get("description") or "").strip().splitlines()[0]
                                        if (d.get("description") or "").strip() else "", 300)}
            if where:
                row["where"] = where
            top.append(row)
            if len(top) >= ANALYZE_TOP:
                break
        return {"tool": "slither", "report": report, "total": len(dets), "counts": counts,
                "top": top, "note": _FINDINGS_NOTE}

    @staticmethod
    def _sum_aderyn(data, report):
        if not isinstance(data, dict):
            return {"error": "aderyn wrote an unexpected report.", "code": "bad_report",
                    "report": report}
        ic = data.get("issue_count") if isinstance(data.get("issue_count"), dict) else {}
        counts = {k: v for k, v in ic.items() if isinstance(v, int)}
        top = []
        for sev in ("high", "medium", "low"):
            block = data.get("%s_issues" % sev)
            for issue in ((block or {}).get("issues") or []) if isinstance(block, dict) else []:
                if not isinstance(issue, dict):
                    continue
                inst = [i for i in (issue.get("instances") or []) if isinstance(i, dict)]
                row = {"severity": sev, "title": scrub(issue.get("title") or "", 200),
                       "detector": issue.get("detector_name"), "instances": len(inst)}
                if inst and inst[0].get("contract_path"):
                    row["where"] = "%s:%s" % (inst[0]["contract_path"], inst[0].get("line_no"))
                top.append(row)
                if len(top) >= ANALYZE_TOP:
                    break
            if len(top) >= ANALYZE_TOP:
                break
        return {"tool": "aderyn", "report": report, "total": sum(counts.values()),
                "counts": counts, "top": top, "note": _FINDINGS_NOTE}


_FINDINGS_NOTE = ("The full report is in the file above. Findings are leads, not proof: read "
                  "the code, and confirm what matters with a test (a local fork when it needs "
                  "real state).")

default = Kit()


def _shutdown_default():
    try:
        default.shutdown()
    except Exception:                                            # noqa: BLE001
        pass


atexit.register(_shutdown_default)
