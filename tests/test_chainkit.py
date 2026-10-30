"""The chain_* smart-contract tools (chainkit.py) and their MCP wiring.

Hermetic: every subprocess, HTTP call, clock and thread is a fake injected into
chainkit.Kit; tests/conftest.py makes chainkit's real defaults fail loudly, so a
test can neither start anvil nor touch the network. No WSL, Foundry, slither or
aderyn is needed.
"""
import json
import os
import shlex
import threading

import pytest

import app
import chainkit
import hub_mcp

ADDR = "0x" + "ab" * 20
ADDR2 = "0x" + "cd" * 20
TX = "0x" + "11" * 32


# --------------------------------------------------------------------------- #
# fakes
# --------------------------------------------------------------------------- #

class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class Http:
    """Routes a request to the first handler whose key is in the URL."""

    def __init__(self, routes=None):
        self.routes = dict(routes or {})
        self.calls = []

    def __call__(self, method, url, body=None, headers=None, timeout=None, max_bytes=None):
        self.calls.append({"method": method, "url": url,
                           "body": json.loads(body) if body else None})
        for key, h in self.routes.items():
            if key in url:
                out = h(method, url, json.loads(body) if body else None) if callable(h) else h
                if isinstance(out, Exception):
                    raise out
                status, payload = out
                return status, payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        raise ConnectionError("no route for the fake")


def rpc_ok(result):
    return lambda m, u, b: (200, {"jsonrpc": "2.0", "id": 1, "result": result})


class Proc:
    def __init__(self, pid=4242):
        self.pid = pid
        self.rc = None

    def poll(self):
        return self.rc


class Spawner:
    def __init__(self):
        self.calls, self.procs = [], []

    def __call__(self, argv, log_path):
        self.calls.append((list(argv), log_path))
        p = Proc(5000 + len(self.procs))
        self.procs.append(p)
        return p


class Killer:
    def __init__(self):
        self.killed = []

    def __call__(self, proc):
        self.killed.append(proc.pid)
        proc.rc = -9


REAPERS = []


def _threads(fn):
    """Analyses run on a real thread; the fork reaper (which sleeps on the fake
    clock) is recorded, not run -- tests call Kit.sweep() themselves."""
    if getattr(fn, "__name__", "") == "_reaper_loop":
        REAPERS.append(fn)
        return None
    t = threading.Thread(target=fn, daemon=True)
    t.start()
    return t


def make_kit(tmp_path, *, http=None, run=None, which=None, wsl=False, guard=None,
             settings=None, hub_ports=None, clock=None, analyze_wait=5.0, logs=None,
             start_thread=None, spawn=None, kill=None, port_free=None):
    clock = clock or Clock()
    return chainkit.Kit(
        http=http or Http(), run=run or (lambda argv, timeout, cwd=None: None),
        spawn=spawn or Spawner(), kill=kill or Killer(), which=which or (lambda n: None),
        clock=clock, wall=lambda: 1_700_000_000.0, sleep=clock.sleep,
        port_free=port_free or (lambda p: True),
        start_thread=start_thread or _threads,
        wsl_available=lambda: wsl, windows=True,
        settings=settings or (lambda k: None),
        guard=guard if guard is not None else (lambda folder: None),
        hub_ports=hub_ports or (lambda: {8787}),
        log=(logs.append if logs is not None else None),
        analyze_wait=analyze_wait, tmp_dir=str(tmp_path))


def rpc(method, params=None, name="tools/call", rid=1):
    return hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": rid, "method": name,
                               "params": {"name": method, "arguments": params or {}}})[0]


def tool_json(out):
    return json.loads(out["result"]["content"][0]["text"])


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """The app's real wiring, with a fake-built Kit as chainkit.default."""
    holder = {}

    def use(**kw):
        kit = make_kit(tmp_path, **kw)
        monkeypatch.setattr(chainkit, "default", kit)
        holder["kit"] = kit
        return kit
    monkeypatch.setattr(app, "_chain_on", lambda: True)
    yield use


# --------------------------------------------------------------------------- #
# MCP surface: listed, flag-gated, -32602 on bad arguments
# --------------------------------------------------------------------------- #

def test_the_seven_tools_are_listed_with_schemas(wired):
    wired()
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    names = [t["name"] for t in out["result"]["tools"]]
    for name in chainkit.TOOLS:
        assert name in names
    for t in chainkit.tool_schemas():
        assert t["inputSchema"]["type"] == "object"
        assert t["description"]
    read = next(t for t in chainkit.tool_schemas() if t["name"] == "chain_read")
    assert "READ-ONLY" in read["description"]
    assert "eth_sendRawTransaction" not in json.dumps(read)


def test_flag_off_means_not_listed_and_refused(wired, monkeypatch):
    kit = wired()
    monkeypatch.setattr(app, "_chain_on", lambda: False)
    out, _ = hub_mcp.handle_rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert not [t for t in out["result"]["tools"] if t["name"].startswith("chain_")]
    err = rpc("chain_tools_status")
    assert err["error"]["code"] == -32602 and "Unknown tool" in err["error"]["message"]
    assert kit._http_fn.calls == []


def test_the_real_flag_defaults_on(monkeypatch):
    monkeypatch.setattr(app.config, "get_flag", lambda name, default=False: default)
    assert app._chain_on() is True
    monkeypatch.setattr(app.config, "get_flag",
                        lambda name, default=False: (_ for _ in ()).throw(OSError("x")))
    assert app._chain_on() is False                        # unreadable config = off


@pytest.mark.parametrize("name,args,word", [
    ("chain_read", {"chain": "ethereum", "method": "eth_sendRawTransaction",
                    "params": ["0x00"]}, "not allowed"),
    ("chain_read", {"chain": "ethereum", "method": "eth_sendTransaction", "params": [{}]},
     "not allowed"),
    ("chain_read", {"chain": "ethereum", "method": "eth_sign", "params": []}, "not allowed"),
    ("chain_read", {"chain": "ethereum", "method": "personal_sign", "params": []}, "not allowed"),
    ("chain_read", {"chain": "ethereum", "method": "eth_getBalance", "params": ["0x12"]},
     "address"),
    ("chain_read", {"chain": "atlantis", "method": "eth_chainId"}, "unknown chain"),
    ("chain_read", {"chain": "ethereum", "method": "eth_call",
                    "params": [{"to": ADDR, "data": "0x"}, "latest", {"x": 1}]}, "params"),
    ("chain_read", {"chain": "ethereum", "method": "eth_call",
                    "params": [{"to": ADDR, "privateKey": "0x1"}]}, "not allowed"),
    ("chain_read", {"chain": "ethereum", "method": "eth_getLogs",
                    "params": [{"fromBlock": 1, "toBlock": 5000}]}, "limited"),
    ("chain_read", {"chain": "ethereum", "method": "eth_getLogs",
                    "params": [{"address": ADDR}]}, "fromBlock"),
    ("chain_read", {"chain": "ethereum", "method": "eth_getTransactionReceipt",
                    "params": ["0x12"]}, "hash"),
    ("chain_source", {"address": "0xnothex", "chain": "ethereum", "project_dir": "C:/x"},
     "address"),
    ("chain_source", {"address": ADDR, "chain": "ethereum"}, "project_dir"),
    ("chain_fork_start", {"chain": "ethereum", "block": -4}, "negative"),
    ("chain_fork_stop", {"id": "../../x"}, "fork id"),
    ("chain_analyze", {"project_dir": "C:/x", "tool": "mythril"}, "tool"),
    ("chain_tools_status", {"refresh": "yes"}, "refresh"),
])
def test_bad_arguments_are_jsonrpc_32602(wired, name, args, word):
    kit = wired()
    out = rpc(name, args)
    assert out["error"]["code"] == -32602, out
    assert word in out["error"]["message"]
    assert kit._http_fn.calls == []                         # nothing left the machine


def test_the_param_checker_is_a_second_allowlist():
    for method in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign",
                   "personal_sign", "anvil_setBalance", "debug_traceTransaction"):
        with pytest.raises(chainkit.ArgError):
            chainkit._read_params(method, [])


def test_a_failure_inside_a_tool_never_raises(wired):
    def boom(*a, **k):
        raise RuntimeError("upstream exploded")
    wired(http=boom)
    out = rpc("chain_read", {"chain": "base", "method": "eth_blockNumber"})
    body = tool_json(out)
    assert out["result"]["isError"] is True and body["code"] == "unavailable"


def test_the_conftest_fence_blocks_real_effects(tmp_path):
    kit = chainkit.Kit(guard=lambda f: None, tmp_dir=str(tmp_path))
    out = kit.call("chain_read", {"chain": "ethereum", "method": "eth_blockNumber"})
    assert out["code"] == "unavailable" and "AssertionError" in out["error"]


# --------------------------------------------------------------------------- #
# chain_read
# --------------------------------------------------------------------------- #

def test_read_posts_one_allowlisted_call_to_the_public_rpc(wired):
    http = Http({"mainnet.base.org": rpc_ok("0x10")})
    wired(http=http)
    out = rpc("chain_read", {"chain": "base", "method": "eth_blockNumber"})
    body = tool_json(out)
    assert body["result"] == "0x10" and body["decimal"] == "16"
    assert body["via"] == "public RPC"
    call = http.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://mainnet.base.org"
    assert call["body"] == {"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []}


def test_read_normalises_params_and_defaults_the_block(wired):
    http = Http({"publicnode": rpc_ok("0x0")})
    wired(http=http)
    rpc("chain_read", {"chain": "1", "method": "eth_getStorageAt", "params": [ADDR, 3]})
    assert http.calls[-1]["body"]["params"] == [ADDR, "0x3", "latest"]
    rpc("chain_read", {"chain": "mainnet", "method": "eth_getLogs",
                       "params": [{"address": ADDR, "fromBlock": 100, "toBlock": "0x7d0"}]})
    flt = http.calls[-1]["body"]["params"][0]
    assert flt["fromBlock"] == "0x64" and flt["toBlock"] == "0x7d0"
    rpc("chain_read", {"chain": "ethereum", "method": "eth_call",
                       "params": [{"to": ADDR, "data": "0x70a08231"}]})
    assert http.calls[-1]["body"]["params"] == [{"to": ADDR, "data": "0x70a08231"}, "latest"]


def test_read_is_rate_limited_per_chain(wired):
    http = Http({"arbitrum": rpc_ok("0x1")})
    wired(http=http)
    for _ in range(chainkit.READS_PER_MINUTE):
        assert "result" in tool_json(rpc("chain_read", {"chain": "arbitrum",
                                                        "method": "eth_chainId"}))
    out = rpc("chain_read", {"chain": "arbitrum", "method": "eth_chainId"})
    body = tool_json(out)
    assert out["result"]["isError"] and body["code"] == "rate_limited" and body["retry_after"] >= 1
    assert len(http.calls) == chainkit.READS_PER_MINUTE


def test_read_caps_big_results_and_long_lists(wired):
    big = "0x" + "ff" * (chainkit.RESULT_MAX_CHARS)
    logs = [{"logIndex": hex(i)} for i in range(chainkit.LIST_MAX_ITEMS + 50)]
    http = Http({"optimism": rpc_ok(big), "polygon": rpc_ok(logs)})
    wired(http=http)
    body = tool_json(rpc("chain_read", {"chain": "optimism", "method": "eth_getCode",
                                        "params": [ADDR]}))
    assert body["result_truncated"] and "result" not in body
    assert len(body["result_preview"]) == chainkit.RESULT_MAX_CHARS
    body = tool_json(rpc("chain_read", {"chain": "polygon", "method": "eth_getLogs",
                                        "params": [{"fromBlock": 1, "toBlock": 2}]}))
    assert len(body["result"]) == chainkit.LIST_MAX_ITEMS and body["truncated_items"] == 50


def test_rpc_errors_and_private_urls_never_leak(wired):
    secret_url = "https://rpc.example.org/v2/SECRETKEY123"
    logs = []
    http = Http({"SECRETKEY123": lambda m, u, b: (200, {
        "jsonrpc": "2.0", "id": 1,
        "error": {"code": 3, "message": "execution reverted at " + secret_url,
                  "data": "0x08c379a0"}})})
    wired(http=http, logs=logs,
          settings=lambda k: {"ethereum": secret_url, "zksync": "https://zk.example.org"}
          if k == chainkit.RPC_SETTING else None)
    out = rpc("chain_read", {"chain": "ethereum", "method": "eth_call",
                             "params": [{"to": ADDR, "data": "0x"}]})
    text = out["result"]["content"][0]["text"]
    body = json.loads(text)
    assert body["code"] == "rpc_error" and body["rpc_code"] == 3
    assert "SECRETKEY123" not in text and "SECRETKEY123" not in "".join(logs)
    assert http.calls[0]["url"] == secret_url               # the owner's RPC was used
    # a chain known only from the setting is accepted
    assert rpc("chain_read", {"chain": "zksync", "method": "eth_chainId"}).get("error") is None


def test_an_override_that_is_not_https_is_ignored(wired):
    http = Http({"publicnode": rpc_ok("0x1"), "evil": rpc_ok("0x2")})
    wired(http=http, settings=lambda k: {"ethereum": "http://evil.example.org"}
          if k == chainkit.RPC_SETTING else None)
    rpc("chain_read", {"chain": "ethereum", "method": "eth_chainId"})
    assert http.calls[0]["url"] == chainkit.CHAINS["ethereum"]["rpc"]


# --------------------------------------------------------------------------- #
# chain_source
# --------------------------------------------------------------------------- #

def _sourcify_answer(extra_sources=None, proxy=False):
    sources = {"contracts/Token.sol": {"content": "pragma solidity ^0.8.20;\ncontract T {}\n"},
               "@openzeppelin/contracts/token/ERC20/ERC20.sol": {"content": "// oz\n"}}
    sources.update(extra_sources or {})
    data = {"match": "exact_match", "runtimeMatch": "exact_match", "creationMatch": "match",
            "chainId": "1", "address": ADDR, "verifiedAt": "2026-01-01T00:00:00Z",
            "sources": sources,
            "compilation": {"language": "Solidity", "compiler": "solc",
                            "compilerVersion": "0.8.20+commit.a1b79de6", "name": "T",
                            "fullyQualifiedName": "contracts/Token.sol:T",
                            "compilerSettings": {"optimizer": {"enabled": True, "runs": 200},
                                                 "evmVersion": "paris", "remappings": ["a=b"]}},
            "abi": [{"type": "function", "name": "totalSupply"}]}
    if proxy:
        data["proxyResolution"] = {"isProxy": True, "proxyType": "EIP1967Proxy",
                                   "implementations": [{"address": ADDR2, "name": "Impl"}]}
    return data


def test_source_writes_the_verified_files_and_answers_short(wired, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    http = Http({"sourcify.dev": (200, _sourcify_answer(proxy=True))})
    wired(http=http)
    out = rpc("chain_source", {"address": ADDR, "chain": "ethereum", "project_dir": str(proj)})
    body = tool_json(out)
    assert http.calls[0]["url"] == ("https://sourcify.dev/server/v2/contract/1/%s"
                                    "?fields=sources,compilation,abi,proxyResolution" % ADDR)
    target = proj / ".chain-src" / ("ethereum-" + ADDR.lower())
    assert body["folder"] == str(target.resolve()) or os.path.samefile(body["folder"], target)
    assert (target / "contracts" / "Token.sol").read_text() .startswith("pragma solidity")
    assert (target / "@openzeppelin" / "contracts" / "token" / "ERC20" / "ERC20.sol").exists()
    assert json.loads((target / "abi.json").read_text())[0]["name"] == "totalSupply"
    meta = json.loads((target / "chain-source.json").read_text())
    assert meta["compilation"]["compilerVersion"].startswith("0.8.20")
    assert (proj / ".chain-src" / ".gitignore").read_text().strip().endswith("*")
    assert body["compiler"]["version"].startswith("0.8.20")
    assert body["compiler"]["optimizer"] == {"enabled": True, "runs": 200}
    assert body["proxy"]["implementations"] == [{"address": ADDR2, "name": "Impl"}]
    assert "PROXY" in body["note"]
    assert "pragma solidity" not in json.dumps(body)        # never the source itself


def test_source_paths_can_never_leave_the_folder(wired, tmp_path):
    proj = tmp_path / "proj"
    proj.mkdir()
    evil = {"../../escape.sol": {"content": "x"}, "/abs/path/A.sol": {"content": "x"},
            "C:\\Windows\\B.sol": {"content": "x"}, "con.sol": {"content": "x"},
            ".git/hooks/pre-commit": {"content": "x"}, "a/./../../../C.sol": {"content": "x"},
            "lib/x.sol": {"content": 5}}
    wired(http=Http({"sourcify.dev": (200, _sourcify_answer(evil))}))
    body = tool_json(rpc("chain_source", {"address": ADDR, "chain": "1",
                                          "project_dir": str(proj)}))
    target = (proj / ".chain-src" / ("ethereum-" + ADDR.lower())).resolve()
    for root, _dirs, files in os.walk(tmp_path):
        for f in files:
            full = os.path.realpath(os.path.join(root, f))
            assert full.startswith(str(target)) or f == ".gitignore", full
    assert not (tmp_path / "escape.sol").exists()
    assert "escape.sol" in body["file_list"]
    assert "_git/hooks/pre-commit" in body["file_list"] and "_con.sol" in body["file_list"]
    assert body["skipped_files"] == 1                       # the non-text entry


@pytest.mark.parametrize("raw,want", [
    ("../../a.sol", "a.sol"), ("/x/y.sol", "x/y.sol"), ("C:\\a\\b.sol", "a/b.sol"),
    ("nul.txt", "_nul.txt"), ("a b/c$.sol", "a_b/c_.sol"), ("..", None), ("", None),
    (".env", "_env"), ("x/..../y.sol", "x/_/y.sol"),
])
def test_safe_rel(raw, want):
    assert chainkit.safe_rel(raw) == want


def test_source_not_verified_bad_request_and_rate_limit(wired, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    wired(http=Http({"/1/": (404, {"match": None}), "/56/": (400, {
        "customCode": "unsupported_chain", "message": "Chain 56 is not supported",
        "errorId": "x"}), "/10/": (429, b"slow down")}))
    a = tool_json(rpc("chain_source", {"address": ADDR, "chain": "1", "project_dir": str(proj)}))
    assert a["code"] == "not_verified" and "eth_getCode" in a["error"]
    b = tool_json(rpc("chain_source", {"address": ADDR, "chain": "bsc", "project_dir": str(proj)}))
    assert b["code"] == "bad_request" and "not supported" in b["error"]
    c = tool_json(rpc("chain_source", {"address": ADDR, "chain": "op", "project_dir": str(proj)}))
    assert c["code"] == "rate_limited"
    assert not (proj / ".chain-src").exists()


def test_source_any_numeric_chain_id_is_asked_of_sourcify(wired, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    http = Http({"/v2/contract/324/": (200, _sourcify_answer())})
    wired(http=http)
    body = tool_json(rpc("chain_source", {"address": ADDR, "chain": "324",
                                          "project_dir": str(proj)}))
    assert body["chain_id"] == 324 and (proj / ".chain-src" / ("324-" + ADDR.lower())).is_dir()


def test_source_respects_the_folder_guard_before_any_request(wired, tmp_path):
    http = Http({"sourcify.dev": (200, _sourcify_answer())})
    wired(http=http, guard=lambda folder: "project_dir is the hub's own folder.")
    body = tool_json(rpc("chain_source", {"address": ADDR, "chain": "1",
                                          "project_dir": str(tmp_path)}))
    assert body["code"] == "bad_folder" and http.calls == []


def test_source_refuses_a_download_folder_that_is_not_a_folder(wired, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".chain-src").write_text("i am a file")
    wired(http=Http({"sourcify.dev": (200, _sourcify_answer())}))
    body = tool_json(rpc("chain_source", {"address": ADDR, "chain": "1",
                                          "project_dir": str(proj)}))
    assert body["code"] == "bad_folder"
    assert (proj / ".chain-src").read_text() == "i am a file"


def test_source_refuses_a_linked_download_folder(wired, tmp_path):
    proj, outside = tmp_path / "p", tmp_path / "outside"
    proj.mkdir()
    outside.mkdir()
    try:
        os.symlink(str(outside), str(proj / ".chain-src"), target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("no symlink privilege here")
    wired(http=Http({"sourcify.dev": (200, _sourcify_answer())}))
    body = tool_json(rpc("chain_source", {"address": ADDR, "chain": "1",
                                          "project_dir": str(proj)}))
    assert body["code"] == "bad_folder" and os.listdir(outside) == []


# --------------------------------------------------------------------------- #
# chain_tools_status
# --------------------------------------------------------------------------- #

def _runner(table, calls):
    def run(argv, timeout, cwd=None):
        calls.append(list(argv))
        if argv[0] == "wsl.exe":
            return table.get("wsl")
        return table.get(tuple(argv))
    return run


def test_status_reports_native_and_wsl_tools_and_never_installs(wired):
    calls = []
    paths = {"forge": "C:/f/forge.exe", "cast": "C:/f/cast.exe", "node": "C:/n/node.exe",
             "npx": "C:/n/npx.cmd"}
    table = {("C:/f/forge.exe", "--version"): (0, "forge Version: 1.3.5-stable\nCommit SHA: x\n"),
             ("C:/f/cast.exe", "--version"): (0, "cast Version: 1.3.5-stable\n"),
             ("C:/n/node.exe", "--version"): (0, "v22.11.0\n"),
             ("C:/n/npx.cmd", "--version"): (0, "10.9.0\n"),
             "wsl": (0, "bash: no job control in this shell\n"
                        "@@anvil\tanvil Version: 1.3.5-stable Commit SHA: x\n"
                        "@@slither\t0.10.4 \n@@solc\tsolc, the solidity compiler commandline "
                        "interface Version: 0.8.26+commit.8a97fa7a.Linux.g++ \n")}
    wired(which=lambda n: paths.get(n), run=_runner(table, calls), wsl=True)
    body = tool_json(rpc("chain_tools_status"))
    t = body["tools"]
    assert t["forge"]["native"] == "1.3.5" and t["node"]["native"] == "22.11.0"
    assert t["anvil"] == {"wsl": "1.3.5"} and t["slither"] == {"wsl": "0.10.4"}
    assert t["solc"] == {"wsl": "0.8.26"}
    assert t["chisel"] == "missing" and t["aderyn"] == "missing"
    assert "foundryup" in body["install"]["foundry"] and "aderyn" in body["install"]
    assert "slither" not in body["install"] and body["wsl_checked"] is True
    assert "never installs" in body["note"]
    for argv in calls:                     # only version probes, never an installer
        assert argv[-1] == "--version" or argv[0] == "wsl.exe"
        assert not any(w in " ".join(argv) for w in ("foundryup", "pip install", "npm install",
                                                     "| bash", "curl"))


def test_status_is_cached_and_refreshable(wired):
    calls = []
    wired(which=lambda n: "C:/x/" + n if n == "forge" else None,
          run=_runner({("C:/x/forge", "--version"): (0, "forge 0.2.0 (abc 2024)")}, calls))
    a = tool_json(rpc("chain_tools_status"))
    n = len(calls)
    tool_json(rpc("chain_tools_status"))
    assert len(calls) == n
    tool_json(rpc("chain_tools_status", {"refresh": True}))
    assert len(calls) == 2 * n and a["tools"]["forge"]["native"] == "0.2.0"


# --------------------------------------------------------------------------- #
# forks
# --------------------------------------------------------------------------- #

def _anvil_words(argv):
    """The anvil arguments of a spawn, native or through `wsl.exe ... -lic 'exec anvil ...'`."""
    return shlex.split(argv[-1])[1:] if argv[0] == "wsl.exe" else list(argv)


def _anvil_answers(spawn, ids=None):
    """Loopback as real anvils answer it: the live fake anvil started on that port
    gives the chain id of the chain it forks (anvil keeps the upstream's id)."""
    def answer(method, url, body):
        port = int(url.split("127.0.0.1:", 1)[1].split("/")[0])
        for (argv, _log), proc in zip(reversed(spawn.calls), reversed(spawn.procs)):
            words = _anvil_words(argv)
            if proc.rc is None and int(words[words.index("--port") + 1]) == port:
                upstream = words[words.index("--fork-url") + 1]
                cid = (ids or {}).get(upstream) or next(
                    c["id"] for c in chainkit.CHAINS.values() if c["rpc"] == upstream)
                return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(cid)}
        return ConnectionRefusedError("nothing listens on %d" % port)
    return answer


def _fork_kit(wired, *, native=True, ready=True, wsl=False, clock=None, logs=None,
              hub_ports=None):
    spawn, kill = Spawner(), Killer()
    calls = []
    table = {("C:/f/anvil.exe", "--version"): (0, "anvil Version: 1.3.5-stable\n"),
             "wsl": (0, "@@anvil\tanvil Version: 1.3.5-stable\n")}
    routes = {"127.0.0.1": _anvil_answers(spawn) if ready else ConnectionRefusedError("no"),
              "publicnode": rpc_ok("0x1")}
    http = Http(routes)
    kit = wired(which=(lambda n: "C:/f/anvil.exe" if (native and n == "anvil") else None),
                run=_runner(table, calls), wsl=wsl, spawn=spawn, kill=kill, http=http,
                clock=clock, logs=logs, hub_ports=hub_ports)
    return kit, spawn, kill, http


def test_fork_start_runs_anvil_on_loopback_and_skips_the_hub_port(wired):
    logs = []
    kit, spawn, kill, http = _fork_kit(wired, logs=logs, hub_ports=lambda: {18545})
    out = rpc("chain_fork_start", {"chain": "ethereum", "block": 21000000})
    body = tool_json(out)
    assert body["rpc_url"] == "http://127.0.0.1:18546" and body["port"] == 18546
    argv = spawn.calls[0][0]
    assert argv[0] == "C:/f/anvil.exe"
    assert argv[argv.index("--host") + 1] == "127.0.0.1"
    assert argv[argv.index("--fork-block-number") + 1] == "21000000"
    assert argv[argv.index("--fork-url") + 1] == chainkit.CHAINS["ethereum"]["rpc"]
    assert "publicnode" not in json.dumps(body) and "publicnode" not in "".join(logs)
    assert "not real" in body["note"] and body["block"] == 21000000


def test_fork_reuse_limit_stop_and_list(wired):
    kit, spawn, kill, _ = _fork_kit(wired)
    a = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    again = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    assert again["id"] == a["id"] and len(spawn.calls) == 1
    b = tool_json(rpc("chain_fork_start", {"chain": "arbitrum"}))
    c = rpc("chain_fork_start", {"chain": "optimism"})
    assert tool_json(c)["code"] == "too_many" and c["result"]["isError"]
    listed = tool_json(rpc("chain_fork_list"))
    assert {f["id"] for f in listed["forks"]} == {a["id"], b["id"]}
    assert tool_json(rpc("chain_fork_stop", {"id": a["id"]})) == {"stopped": a["id"]}
    assert spawn.procs[0].pid in kill.killed
    assert tool_json(rpc("chain_fork_stop", {"id": a["id"]}))["code"] == "not_found"


def test_forks_stop_when_idle_when_they_exit_and_at_shutdown(wired):
    clock = Clock()
    kit, spawn, kill, _ = _fork_kit(wired, clock=clock)
    a = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    b = tool_json(rpc("chain_fork_start", {"chain": "polygon"}))
    clock.t += chainkit.FORK_IDLE_SECONDS - 10
    tool_json(rpc("chain_read", {"chain": b["id"], "method": "eth_chainId"}))   # b is used
    clock.t += 20
    kit.sweep()
    ids = {f["id"] for f in tool_json(rpc("chain_fork_list"))["forks"]}
    assert ids == {b["id"]} and spawn.procs[0].pid in kill.killed
    spawn.procs[1].rc = 1                                   # anvil died on its own
    assert tool_json(rpc("chain_fork_list"))["forks"] == []
    c = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    assert c["id"] not in (a["id"], b["id"])
    kit.shutdown()
    assert spawn.procs[-1].pid in kill.killed


def test_the_reaper_starts_once_and_ends_when_no_fork_is_left(wired):
    REAPERS.clear()
    clock = Clock()
    kit, spawn, kill, _ = _fork_kit(wired, clock=clock)
    tool_json(rpc("chain_fork_start", {"chain": "base"}))
    tool_json(rpc("chain_fork_start", {"chain": "polygon"}))
    assert len(REAPERS) == 1
    REAPERS[0]()            # each fake sleep moves the clock a minute: both go idle
    assert kit._forks == {} and kit._reaper is False and len(kill.killed) == 2


def test_read_through_a_fork_goes_to_loopback_without_a_limit(wired):
    kit, spawn, _, http = _fork_kit(wired)
    fid = tool_json(rpc("chain_fork_start", {"chain": "sepolia"}))["id"]
    for _ in range(chainkit.READS_PER_MINUTE + 5):
        body = tool_json(rpc("chain_read", {"chain": fid, "method": "eth_getBalance",
                                            "params": [ADDR]}))
    assert body["via"].startswith("local fork") and http.calls[-1]["url"].startswith(
        "http://127.0.0.1:")


def test_fork_through_wsl_when_anvil_is_only_there(wired):
    kit, spawn, _, _ = _fork_kit(wired, native=False, wsl=True)
    body = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    argv = spawn.calls[0][0]
    assert argv[:4] == ["wsl.exe", "-e", "bash", "-lic"]
    assert argv[4].startswith("exec anvil --fork-url https://mainnet.base.org --host 127.0.0.1")
    assert body["runs"] == "wsl"


def test_fork_that_never_comes_up_is_killed_and_explained(wired, tmp_path):
    kit, spawn, kill, _ = _fork_kit(wired, ready=False)

    def spawn_with_log(argv, log_path):
        with open(log_path, "w") as fh:
            fh.write("Error: failed to get fork block from https://ethereum-rpc.publicnode.com/x\n")
        return Spawner.__call__(spawn, argv, log_path)
    kit._spawn_fn = spawn_with_log
    body = tool_json(rpc("chain_fork_start", {"chain": "ethereum"}))
    assert body["code"] == "fork_failed"
    assert "publicnode" not in body["log_tail"] and "failed to get fork block" in body["log_tail"]
    assert spawn.procs[0].pid in kill.killed
    assert tool_json(rpc("chain_fork_list"))["forks"] == []


def test_fork_without_anvil_says_how_to_install_it(wired):
    wired()
    body = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    assert body["code"] == "not_installed" and "foundryup" in body["install"]


# ---- several starts at once (2026-10-30) -----------------------------------

class _LateProc(Proc):
    """A fake anvil whose port was taken: alive while it fetches, then it exits."""

    def __init__(self, pid):
        super().__init__(pid)
        self.lives = None

    def poll(self):
        if self.rc is None and self.lives is not None:
            self.lives -= 1
            if self.lives < 0:
                self.rc = 1
        return self.rc


class AnvilNet:
    """Fake loopback with real port ownership. A fake anvil binds its port only
    when it is up -- after `before_bind`, which stands for the seconds anvil
    spends fetching the upstream state -- and only when no live anvil holds it;
    one whose port was taken stays alive a moment, then exits, as anvil does."""

    def __init__(self):
        self.lock = threading.Lock()
        self.bound = {}           # port -> (proc, chain id)
        self.procs = []
        self.before_bind = None

    def _holder(self, port):
        held = self.bound.get(port)
        return held if held and held[0].rc is None else None

    def port_free(self, port):
        with self.lock:
            return self._holder(port) is None

    def spawn(self, argv, log_path):
        words = _anvil_words(argv)
        port = int(words[words.index("--port") + 1])
        upstream = words[words.index("--fork-url") + 1]
        cid = next(c["id"] for c in chainkit.CHAINS.values() if c["rpc"] == upstream)
        with self.lock:
            proc = _LateProc(6000 + len(self.procs))
            self.procs.append(proc)
        if self.before_bind:
            self.before_bind(argv)
        with self.lock:
            if self._holder(port) is None:
                self.bound[port] = (proc, cid)
            else:
                proc.lives = 3            # "address already in use", a moment later
        return proc

    def answer(self, method, url, body):
        port = int(url.split("127.0.0.1:", 1)[1].split("/")[0])
        with self.lock:
            held = self._holder(port)
        if held is None:
            return ConnectionRefusedError("nothing listens on %d" % port)
        return 200, {"jsonrpc": "2.0", "id": 1, "result": hex(held[1])}

    def serving(self, port):
        """The chain the anvil really listening on `port` forks (None: nothing)."""
        with self.lock:
            held = self._holder(port)
        return held and next(n for n, c in chainkit.CHAINS.items() if c["id"] == held[1])


def _net_kit(wired, net):
    table = {("C:/f/anvil.exe", "--version"): (0, "anvil Version: 1.3.5-stable\n")}
    kill = Killer()
    kit = wired(which=lambda n: "C:/f/anvil.exe" if n == "anvil" else None,
                run=_runner(table, []), spawn=net.spawn, kill=kill,
                http=Http({"127.0.0.1": net.answer}), port_free=net.port_free)
    return kit, kill


def test_two_forks_started_at_once_never_share_a_port(wired):
    """Two parallel chain_fork_start calls both find 18545 free (anvil binds only
    once it has fetched the upstream state); each fork must get its own port and
    be checked against its own chain."""
    net = AnvilNet()
    kit, _ = _net_kit(wired, net)
    gate, passed = threading.Barrier(2, timeout=5), set()

    def port_free(port):
        me = threading.get_ident()
        if port == 18545 and me not in passed:
            passed.add(me)
            try:
                gate.wait()               # both starts test the same port together
            except threading.BrokenBarrierError:
                pass
        return net.port_free(port)
    kit._port_free_fn = port_free
    out = {}

    def start(chain):
        out[chain] = kit.call("chain_fork_start", {"chain": chain})
    threads = [threading.Thread(target=start, args=(c,)) for c in ("ethereum", "base")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    ports = {chain: body.get("port") for chain, body in out.items()}
    assert sorted(ports.values()) == [18545, 18546], out
    for chain, port in ports.items():
        assert net.serving(port) == chain
        assert out[chain]["chain_id"] == chainkit.CHAINS[chain]["id"]
    assert sorted((f["chain"], f["port"]) for f in kit._forks.values()) == \
        sorted(ports.items())
    assert kit._pending == {}


def test_a_start_still_coming_up_holds_its_port_and_its_slot(wired):
    """While one fork is still fetching, other starts must not take its port,
    must count it against FORK_MAX, and a start of the same chain must not run a
    second anvil."""
    net = AnvilNet()
    kit, _ = _net_kit(wired, net)
    seen = {}

    def while_ethereum_fetches(argv):
        if seen:
            return
        seen["busy"] = True
        seen["base"] = kit.call("chain_fork_start", {"chain": "base"})
        seen["polygon"] = kit.call("chain_fork_start", {"chain": "polygon"})
        seen["ethereum"] = kit.call("chain_fork_start", {"chain": "ethereum"})
    net.before_bind = while_ethereum_fetches
    eth = kit.call("chain_fork_start", {"chain": "ethereum"})
    assert eth.get("port") and seen["base"].get("port") and eth["port"] != seen["base"]["port"]
    assert net.serving(eth["port"]) == "ethereum"
    assert net.serving(seen["base"]["port"]) == "base"
    assert seen["polygon"]["code"] == "too_many" and seen["polygon"]["starting"] == 1
    assert seen["ethereum"]["code"] == "starting"
    assert len(kit._forks) == chainkit.FORK_MAX and kit._pending == {}
    assert len(net.procs) == 2                       # no anvil for the refused starts


def test_a_failed_start_gives_its_slot_and_port_back(wired):
    kit, spawn, kill, _ = _fork_kit(wired, ready=False)
    assert tool_json(rpc("chain_fork_start", {"chain": "base"}))["code"] == "fork_failed"
    assert kit._pending == {}
    kit._http_fn.routes["127.0.0.1"] = _anvil_answers(spawn)
    a = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    b = tool_json(rpc("chain_fork_start", {"chain": "arbitrum"}))
    assert a["port"] == 18545 and b["port"] == 18546 and len(kit._forks) == 2


def test_a_port_that_answers_as_another_chain_is_not_taken_for_the_fork(wired):
    kit, spawn, kill, http = _fork_kit(wired)
    http.routes["127.0.0.1"] = rpc_ok("0x1")         # some other program: an Ethereum node
    body = tool_json(rpc("chain_fork_start", {"chain": "base"}))
    assert body["code"] == "wrong_chain" and "8453" in body["error"]
    assert spawn.procs[0].pid in kill.killed
    assert kit._forks == {} and kit._pending == {}
    assert tool_json(rpc("chain_fork_list"))["forks"] == []


def test_a_chain_only_the_owners_setting_names_takes_the_id_it_answers(wired):
    kit, spawn, kill, http = _fork_kit(wired)
    kit._settings = lambda k: ({"mychain": "https://rpc.example.com/x"}
                               if k == chainkit.RPC_SETTING else None)
    http.routes["127.0.0.1"] = rpc_ok("0x539")
    body = tool_json(rpc("chain_fork_start", {"chain": "mychain"}))
    assert body["chain"] == "mychain" and body["port"] == 18545 and body["chain_id"] is None
    assert kill.killed == []


# --------------------------------------------------------------------------- #
# chain_analyze
# --------------------------------------------------------------------------- #

SLITHER_REPORT = {"success": True, "error": None, "results": {"detectors": [
    {"check": "naming-convention", "impact": "Informational", "confidence": "High",
     "description": "Parameter x is not in mixedCase\n",
     "elements": [{"source_mapping": {"filename_relative": "src/T.sol", "lines": [9]}}]},
    {"check": "reentrancy-eth", "impact": "High", "confidence": "Medium",
     "description": "Reentrancy in Vault.withdraw() (src/Vault.sol#20-30):\n\tExternal calls...",
     "elements": [{"source_mapping": {"filename_relative": "src/Vault.sol", "lines": [20, 21]}}]},
    {"check": "unchecked-transfer", "impact": "Medium", "confidence": "Medium",
     "description": "Vault.pay ignores return value", "elements": []},
]}}

ADERYN_REPORT = {"files_summary": {}, "files_details": {},
                 "issue_count": {"high": 1, "low": 2}, "detectors_used": [],
                 "high_issues": {"issues": [{"title": "Arbitrary from in transferFrom",
                                             "description": "...", "detector_name": "arbitrary-transfer-from",
                                             "instances": [{"contract_path": "src/Vault.sol",
                                                            "line_no": 42, "src": "1:2",
                                                            "src_char": "1:2"}]}]},
                 "low_issues": {"issues": [{"title": "Centralization risk", "description": "",
                                            "detector_name": "centralization-risk",
                                            "instances": [{}, {}]},
                                           {"title": "Unspecific pragma", "description": "",
                                            "detector_name": "unspecific-solidity-pragma",
                                            "instances": []}]}}


def _analyzer_run(report, calls, block=None):
    def run(argv, timeout, cwd=None):
        calls.append((list(argv), cwd, timeout))
        if argv[-1] == "--version" or argv[0] == "wsl.exe" and "command -v" in argv[-1]:
            name = os.path.basename(argv[0]).split(".")[0]
            return (0, "%s 0.10.4\n" % name)
        if block is not None:
            block.wait(10)
        rel = argv[argv.index("--json") + 1] if "--json" in argv else argv[argv.index("-o") + 1]
        with open(os.path.join(cwd, rel), "w", encoding="utf-8") as fh:
            json.dump(report, fh)
        return (255, "Slither analysed 3 contracts, 3 results\n")
    return run


def test_slither_report_is_written_and_summarised(wired, tmp_path):
    proj = tmp_path / "vault"
    proj.mkdir()
    calls = []
    wired(which=lambda n: "C:/py/Scripts/slither.exe" if n == "slither" else None,
          run=_analyzer_run(SLITHER_REPORT, calls))
    out = rpc("chain_analyze", {"project_dir": str(proj)})
    body = tool_json(out)
    assert not out["result"].get("isError")
    argv, cwd, timeout = calls[-1]
    assert argv[:4] == ["C:/py/Scripts/slither.exe", ".", "--json", argv[3]]
    assert argv[3].startswith(".chain-reports/slither-") and argv[3].endswith(".json")
    assert os.path.samefile(cwd, proj) and timeout == chainkit.ANALYZE_TIMEOUT
    assert body["counts"] == {"Informational": 1, "High": 1, "Medium": 1} and body["total"] == 3
    assert [f["check"] for f in body["top"]] == ["reentrancy-eth", "unchecked-transfer",
                                                  "naming-convention"]
    assert body["top"][0]["where"] == "src/Vault.sol:20"
    assert os.path.isfile(body["report"]) and (proj / ".chain-reports" / ".gitignore").exists()
    assert "not proof" in body["note"]


def test_aderyn_report_is_summarised(wired, tmp_path):
    proj = tmp_path / "vault"
    proj.mkdir()
    calls = []
    wired(which=lambda n: "C:/c/aderyn.exe" if n == "aderyn" else None,
          run=_analyzer_run(ADERYN_REPORT, calls))
    body = tool_json(rpc("chain_analyze", {"project_dir": str(proj), "tool": "aderyn"}))
    argv = calls[-1][0]
    assert argv[:4] == ["C:/c/aderyn.exe", ".", "--no-snippets", "-o"]
    assert body["counts"] == {"high": 1, "low": 2} and body["total"] == 3
    assert body["top"][0] == {"severity": "high", "title": "Arbitrary from in transferFrom",
                              "detector": "arbitrary-transfer-from", "instances": 1,
                              "where": "src/Vault.sol:42"}


def test_a_long_analysis_answers_running_then_its_result(wired, tmp_path):
    proj = tmp_path / "vault"
    proj.mkdir()
    gate, calls = threading.Event(), []
    kit = wired(which=lambda n: "C:/s/slither" if n == "slither" else None,
                run=_analyzer_run(SLITHER_REPORT, calls, block=gate), analyze_wait=0.0)
    first = tool_json(rpc("chain_analyze", {"project_dir": str(proj)}))
    assert first["state"] == "running" and first["job_id"].startswith("an-")
    gate.set()
    job = next(iter(kit._jobs.values()))
    assert job["done"].wait(10)
    done = tool_json(rpc("chain_analyze", {"job_id": first["job_id"]}))
    assert done["total"] == 3 and done["job_id"] == first["job_id"]
    assert tool_json(rpc("chain_analyze", {"job_id": "an-nothere"}))["code"] == "not_found"


def test_analysis_without_the_tool_or_with_a_bad_folder(wired, tmp_path):
    calls = []
    wired(run=_analyzer_run(SLITHER_REPORT, calls))
    body = tool_json(rpc("chain_analyze", {"project_dir": str(tmp_path)}))
    assert body["code"] == "not_installed" and "slither-analyzer" in body["install"]
    wired(guard=lambda f: "project_dir is too broad.", run=_analyzer_run(SLITHER_REPORT, calls))
    assert tool_json(rpc("chain_analyze", {"project_dir": str(tmp_path)}))["code"] == "bad_folder"
    assert [c for c in calls if c[0][-1] != "--version"] == []


def test_an_analyzer_that_writes_nothing_is_reported(wired, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()

    def run(argv, timeout, cwd=None):
        if argv[-1] == "--version":
            return (0, "0.10.4")
        return (1, "Error: compilation failed, see https://secret.example/key\n")
    wired(which=lambda n: "C:/s/slither" if n == "slither" else None, run=run)
    body = tool_json(rpc("chain_analyze", {"project_dir": str(proj)}))
    assert body["code"] == "no_report" and "secret.example" not in body["output_tail"]


@pytest.mark.skipif(os.name != "nt", reason="drive-letter paths")
def test_analysis_runs_in_wsl_when_the_tool_is_only_there(wired, tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    calls = []

    def run(argv, timeout, cwd=None):
        calls.append((list(argv), cwd))
        if argv[0] == "wsl.exe" and "command -v" in argv[-1]:
            return (0, "@@slither\t0.10.4\n")
        return (0, "")
    wired(run=run, wsl=True)
    rpc("chain_analyze", {"project_dir": str(proj)})
    argv, cwd = calls[-1]
    real = os.path.realpath(str(proj))
    assert argv[:2] == ["wsl.exe", "--cd"] and argv[2] == chainkit.windows_to_wsl(real)
    assert argv[3:6] == ["-e", "bash", "-lic"] and argv[6].startswith("exec slither . --json ")
    assert cwd is None and argv[2].startswith("/mnt/")


# --------------------------------------------------------------------------- #
# the app's folder guard
# --------------------------------------------------------------------------- #

@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("USERPROFILE", str(h))
    monkeypatch.setenv("HOME", str(h))
    return h


def test_guard_accepts_a_project_under_home(home):
    proj = home / "code" / "vault"
    proj.mkdir(parents=True)
    assert app._chain_folder_guard(str(proj)) is None


def test_guard_refusals(home, tmp_path, monkeypatch):
    outside = tmp_path / "elsewhere" / "proj"
    outside.mkdir(parents=True)
    g = app._chain_folder_guard
    assert "absolute" in g("relative/proj")
    assert "existing" in g(str(home / "nope"))
    assert "too broad" in g(str(home))
    assert "too broad" in g(os.path.abspath(os.sep))
    assert "hub's own folder" in g(os.path.dirname(os.path.abspath(app.__file__)))
    assert "outside your home" in g(str(outside))
    state = home / "state"
    state.mkdir()
    monkeypatch.setattr(app.config, "state_dir", lambda: str(state))
    assert "state folder" in g(str(state))
    real_get = app.config.get_setting
    monkeypatch.setattr(app.config, "get_setting",
                        lambda k, *a, **kw: [str(outside.parent)]
                        if k == "chain_project_roots" else real_get(k, *a, **kw))
    assert g(str(outside)) is None                          # the owner listed its parent


def test_guard_wsl_rules():
    ok = app._chain_wsl_ok
    assert ok("\\\\wsl.localhost\\Ubuntu\\home\\me\\vault")
    assert ok("\\\\wsl$\\Debian\\root\\vault")
    for bad in ("\\\\wsl.localhost\\Ubuntu\\", "\\\\wsl.localhost\\Ubuntu\\home",
                "\\\\wsl.localhost\\Ubuntu\\home\\me", "\\\\wsl.localhost\\Ubuntu\\mnt\\c",
                "\\\\wsl.localhost\\Ubuntu\\etc", "C:\\x\\y"):
        assert not ok(bad), bad


def test_an_unwired_kit_writes_and_runs_nothing(tmp_path):
    kit = chainkit.Kit(http=Http(), tmp_dir=str(tmp_path))
    out = kit.call("chain_source", {"address": ADDR, "chain": "1", "project_dir": str(tmp_path)})
    assert out["code"] == "bad_folder" and kit._http_fn.calls == []
