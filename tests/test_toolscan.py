"""CLI TOOLS HERE (toolscan.py, app `_ts_*`, flag `tool_scan`), 2026-10-29.

Hermetic: every command goes through a fake runner, `which` is a fake, nothing
real is run (tests/conftest.py fences toolscan's default runner for the whole
suite and fails a test that reaches it).
"""
import json
import threading
import time

import pytest

import toolscan


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #

class FakeRunner:
    """run(argv, timeout, output=True) -> (rc, text) | None, recording calls."""

    def __init__(self, versions=None, logins=None, wsl_running="", wsl_out="",
                 leak="alice@example.com token=ghp_LEAKLEAKLEAKLEAKLEAK"):
        self.versions = versions or {}
        self.logins = logins or {}
        self.wsl_running = wsl_running
        self.wsl_out = wsl_out
        self.leak = leak
        self.calls = []
        self.lock = threading.Lock()

    def __call__(self, argv, timeout, output=True):
        with self.lock:
            self.calls.append((list(argv), timeout, output))
        exe = str(argv[0])
        if exe == "wsl.exe":
            if argv[1] == "-l":
                return (0, self.wsl_running) if self.wsl_running is not None else None
            return (0, self.wsl_out)
        name = exe.rsplit("/", 1)[-1]
        rest = tuple(argv[1:])
        tool = toolscan._BY_NAME.get(name) or toolscan._BY_NAME.get("flyctl")
        if tool and tool.get("login") and rest == tuple(tool["login"]):
            got = self.logins.get(name)
            if got is None:
                return None                      # a timeout / crash
            # The real output would carry an e-mail or a token: the scan must
            # never ask for it.
            assert output is False
            return (got, "")
        return (0, "%s version %s\n%s" % (name, self.versions.get(name, "1.2.3"), self.leak))

    def login_calls(self):
        return [c for c in self.calls if c[2] is False]


def which_of(*names):
    found = {n: "/usr/bin/" + n for n in names}
    return lambda exe: found.get(exe)


# --------------------------------------------------------------------------- #
# The tool list
# --------------------------------------------------------------------------- #

def test_the_tool_list_is_data_and_covers_the_owners_clis():
    names = [t["name"] for t in toolscan.TOOLS]
    assert len(names) == len(set(names))
    for wanted in ("shopify", "vercel", "netlify", "gh", "stripe", "supabase", "firebase",
                   "wrangler", "aws", "gcloud", "az", "heroku", "railway", "flyctl",
                   "docker", "kubectl", "terraform", "pnpm", "bun", "deno", "uv"):
        assert wanted in names, wanted
    for t in toolscan.TOOLS:
        assert t["exe"] and all(isinstance(e, str) and e for e in t["exe"])
        assert t["version"] and isinstance(t["version"], tuple)
        assert "hint" in t and len(t["hint"]) <= 80


def test_only_status_commands_verified_in_the_vendor_docs_are_used():
    with_login = {t["name"]: tuple(t["login"]) for t in toolscan.TOOLS if t.get("login")}
    assert with_login == {
        "gh": ("auth", "status"),
        "vercel": ("whoami",),
        "netlify": ("status", "--json"),
        "wrangler": ("whoami", "--json"),
        "aws": ("sts", "get-caller-identity"),
        "az": ("account", "show"),
        "heroku": ("auth:whoami",),
        "railway": ("whoami",),
        "flyctl": ("auth", "whoami", "--json"),
    }
    # Variants the docs/source show exit 0 when logged OUT: never used.
    assert with_login["gh"] != ("auth", "status", "--json")
    assert with_login["netlify"] != ("status",)
    assert with_login["wrangler"] != ("whoami",)
    assert "firebase" not in with_login            # login:list exits 0 with no account
    for name in ("shopify", "stripe", "supabase", "gcloud", "docker", "kubectl",
                 "terraform", "pnpm", "bun", "deno", "uv"):
        assert name not in with_login


@pytest.mark.parametrize("text,want", [
    ("gh version 2.62.0 (2024-11-14)", "2.62.0"),
    ("aws-cli/2.27.41 Python/3.11.6 Linux/5.10", "2.27.41"),
    ("Docker version 27.1.1, build 6312585", "27.1.1"),
    ("Client Version: v1.31.0\nKustomize Version: v5.4.2", "1.31.0"),
    ('{"terraform_version": "1.9.8", "platform": "windows_amd64"}', "1.9.8"),
    ("netlify-cli/17.37.1 win32-x64 node-v20.11.1", "17.37.1"),
    ("4.0.0", "4.0.0"),
    ("", None),
    ("not a version", None),
])
def test_parse_version(text, want):
    assert toolscan.parse_version(text) == want


# --------------------------------------------------------------------------- #
# The scan
# --------------------------------------------------------------------------- #

def test_only_installed_tools_appear_with_version_and_login():
    run = FakeRunner(versions={"gh": "2.62.0", "vercel": "37.1.0"},
                     logins={"gh": 0, "vercel": 1})
    snap = toolscan.scan(which=which_of("gh", "vercel", "docker", "shopify"), run=run,
                         windows=False)
    assert set(snap["native"]) == {"gh", "vercel", "docker", "shopify"}
    assert snap["native"]["gh"] == {"version": "2.62.0", "login": True}
    assert snap["native"]["vercel"] == {"version": "37.1.0", "login": False}
    assert snap["native"]["docker"]["login"] is None            # no status command
    assert snap["native"]["shopify"]["login"] is None
    assert snap["wsl"] == {}
    # Never a login call for a tool without a verified status command.
    called = {c[0][0].rsplit("/", 1)[-1] for c in run.login_calls()}
    assert called == {"gh", "vercel"}


def test_a_status_command_that_times_out_is_unknown_not_logged_out():
    run = FakeRunner(logins={"gh": None})
    snap = toolscan.scan(which=which_of("gh"), run=run, windows=False)
    assert snap["native"]["gh"]["login"] is None


def test_an_aws_network_error_is_unknown_not_logged_out():
    for rc, want in ((0, True), (253, False), (254, None), (255, None)):
        run = FakeRunner(logins={"aws": rc})
        snap = toolscan.scan(which=which_of("aws"), run=run, windows=False)
        assert snap["native"]["aws"]["login"] is want, rc


def test_the_output_of_a_command_is_never_kept():
    run = FakeRunner(logins={"gh": 0})
    snap = toolscan.scan(which=which_of("gh", "docker"), run=run, windows=False)
    blob = json.dumps(snap) + toolscan.block(snap) + json.dumps(toolscan.rows(snap))
    assert "alice@example.com" not in blob
    assert "ghp_LEAK" not in blob


def test_a_crashing_runner_or_which_never_raises():
    def boom(*_a, **_k):
        raise RuntimeError("x")
    snap = toolscan.scan(which=boom, run=boom, windows=True)
    assert snap["native"] == {} and snap["wsl"] == {}


# --------------------------------------------------------------------------- #
# WSL
# --------------------------------------------------------------------------- #

UTF16_RUNNING = "U\x00b\x00u\x00n\x00t\x00u\x00\r\x00\n\x00"


def test_wsl_is_only_looked_into_when_a_distro_is_already_running():
    run = FakeRunner(wsl_running="")
    snap = toolscan.scan(which=which_of(), run=run, windows=True)
    assert snap["wsl"] == {}
    argvs = [c[0] for c in run.calls]
    assert argvs == [["wsl.exe", "-l", "--running", "-q"]]       # never `wsl -d` (boot)


def test_wsl_is_never_touched_off_windows():
    run = FakeRunner(wsl_running=UTF16_RUNNING)
    toolscan.scan(which=which_of(), run=run, windows=False)
    assert not any(c[0][0] == "wsl.exe" for c in run.calls)


def test_wsl_tools_are_read_from_the_running_distro():
    out = ("bash: cannot set terminal process group\n"
           "T|gh|gh version 2.40.1 (2024-01-01)\nL|gh|1\n"
           "T|supabase|2.3.4\nT|aws|aws-cli/2.1.0 Python\nL|aws|255\n"
           "T|nope|9.9\nL|uv|0\n")
    run = FakeRunner(wsl_running=UTF16_RUNNING, wsl_out=out)
    snap = toolscan.scan(which=which_of(), run=run, windows=True)
    rows = snap["wsl"]["Ubuntu"]
    assert rows["gh"] == {"version": "2.40.1", "login": False}
    assert rows["supabase"] == {"version": "2.3.4", "login": None}
    assert rows["aws"]["login"] is None                          # 255: network error
    assert "nope" not in rows and "uv" not in rows
    script_call = [c for c in run.calls if c[0][:3] == ["wsl.exe", "-d", "Ubuntu"]][0]
    assert script_call[0][3:6] == ["-e", "bash", "-lic"]


def test_the_wsl_script_never_captures_a_status_command_output():
    script = toolscan.wsl_script()
    login_lines = [ln for ln in script.splitlines() if '"L|' in ln]
    assert len(login_lines) == sum(1 for t in toolscan.TOOLS if t.get("login"))
    for ln in login_lines:
        seg = ln[ln.index("timeout %d" % int(toolscan.LOGIN_TIMEOUT)):]
        assert ">/dev/null 2>&1" in seg.split(";")[0]
        assert "</dev/null" in seg.split(";")[0]


def test_a_stopped_distro_keeps_its_last_result():
    prev = {"wsl": {"Ubuntu": {"gh": {"version": "2.0.0", "login": True}}}}
    run = FakeRunner(wsl_running="")
    snap = toolscan.scan(which=which_of(), run=run, windows=True, previous=prev)
    assert snap["wsl"] == prev["wsl"]


def test_running_distros_skips_docker_desktop_and_caps_the_count():
    text = "Ubuntu\r\ndocker-desktop\r\nDebian\r\nkali-linux\r\n"
    run = lambda argv, timeout, output=True: (0, text)       # noqa: E731
    assert toolscan.running_distros(run) == ["Ubuntu", "Debian"]


# --------------------------------------------------------------------------- #
# The block
# --------------------------------------------------------------------------- #

def _snap(native=None, wsl=None):
    return {"at": 1.0, "native": native or {}, "wsl": wsl or {}}


def test_the_block_names_the_tools_and_who_must_log_in():
    snap = _snap({"gh": {"version": "2", "login": True},
                  "vercel": {"version": "3", "login": False},
                  "shopify": {"version": "4", "login": None},
                  "docker": {"version": "5", "login": None}},
                 {"Ubuntu": {"supabase": {"version": "1", "login": None}}})
    text = toolscan.block(snap)
    assert text.startswith(toolscan.MARKER)
    assert "gh (logged in)" in text
    assert "vercel (not logged in: ask the user to run `vercel login`)" in text
    assert "shopify" in text and "docker" in text
    assert "in WSL Ubuntu: supabase" in text
    assert "never run an interactive login yourself" in text
    assert len(text) <= toolscan.BLOCK_MAX
    assert toolscan.block(snap) == text                          # deterministic


def test_the_block_is_empty_without_a_scan_or_a_tool():
    assert toolscan.block(None) == ""
    assert toolscan.block(_snap()) == ""
    assert toolscan.block("junk") == ""


@pytest.mark.parametrize("limit", [350, 300, 250, 200, 150, 120, 100, 90])
def test_the_block_never_crosses_its_limit(limit):
    native = {n: {"version": "1.0", "login": False} for n in toolscan._NAMES}
    text = toolscan.block(_snap(native, {"Ubuntu": dict(native)}), limit)
    assert len(text) <= limit
    if text:
        assert text.startswith(toolscan.MARKER)


def test_rows_for_the_dashboard():
    rows = toolscan.rows(_snap({"gh": {"version": "2", "login": True}},
                               {"Ubuntu": {"uv": {"version": "0.4", "login": None}}}))
    assert rows[0]["name"] == "gh" and rows[0]["where"] == "native"
    assert rows[0]["login_checked"] is True and rows[0]["login_cmd"] == "gh auth login"
    assert rows[1]["where"] == "WSL Ubuntu" and rows[1]["login_checked"] is False


# --------------------------------------------------------------------------- #
# The cache
# --------------------------------------------------------------------------- #

def test_snapshot_is_off_when_disabled(monkeypatch):
    monkeypatch.setattr(toolscan, "ENABLED", False)
    called = []
    monkeypatch.setattr(toolscan, "scan", lambda **k: called.append(1) or _snap())
    assert toolscan.snapshot(wait=1.0) is None
    assert toolscan.refresh() is False
    assert called == []


def test_snapshot_refreshes_in_the_background_and_never_waits(monkeypatch):
    gate = threading.Event()
    calls = []

    def slow_scan(**kw):
        calls.append(kw)
        gate.wait(5)
        return _snap({"gh": {"version": "2", "login": True}})

    monkeypatch.setattr(toolscan, "scan", slow_scan)
    t0 = time.monotonic()
    assert toolscan.snapshot(wait=0) is None                     # nothing cached yet
    assert time.monotonic() - t0 < 1.0
    gate.set()
    snap = toolscan.snapshot(wait=5.0)
    assert snap["native"]["gh"]["login"] is True
    assert toolscan.snapshot(wait=0) is snap                     # fresh: no new scan
    assert len(calls) == 1


def test_a_stale_snapshot_is_refreshed_with_the_previous_one(monkeypatch):
    first = _snap({"gh": {"version": "2", "login": True}},
                  {"Ubuntu": {"uv": {"version": "0.4", "login": None}}})
    seen = []

    def fake_scan(previous=None, **_k):
        seen.append(previous)
        return first if previous is None else _snap({"gh": {"version": "3", "login": True}})

    monkeypatch.setattr(toolscan, "scan", fake_scan)
    assert toolscan.snapshot(wait=5.0) is first
    monkeypatch.setattr(toolscan, "TTL", 0.0)
    toolscan.snapshot(wait=5.0)
    assert seen[-1] is first


# --------------------------------------------------------------------------- #
# Where the block goes: the craft brief, Multi prompts, the /v1 opening turn
# --------------------------------------------------------------------------- #

BLOCK = ("CLI TOOLS HERE: gh (logged in), vercel (not logged in: ask the user to run "
         "`vercel login`). Never run an interactive login yourself; ask the user.")


@pytest.fixture
def app_mod(monkeypatch):
    import app
    monkeypatch.setattr(app, "_ts_on", lambda: True)
    monkeypatch.setattr(app, "_ts_block_text", lambda max_chars=None, wait=0.0: (
        BLOCK if max_chars is None or len(BLOCK) <= max_chars else ""))
    return app


def test_the_craft_brief_carries_the_block_on_tool_carrying_turns(app_mod):
    import craft
    body = craft.system_message("fix the failing test in utils.py", tools=True)["content"]
    assert BLOCK in body
    assert craft.system_message("what is a closure?", tools=False) is None
    assert len(craft.system_message("create a landing page for my saas")["content"]) \
        <= craft.BRIEF_CEILING_CHARS


def test_the_tools_block_is_dropped_before_the_machine_block(monkeypatch):
    import craft
    text = "fix the bug in utils.py"
    monkeypatch.setattr(craft, "_ENV_SOURCE", None)
    monkeypatch.setattr(craft, "_TOOLS_SOURCE", None)
    base = len(craft.system_message(text)["content"])
    room = craft.BRIEF_CEILING_CHARS - base - 2           # "\n\n" joins one extra
    # The machine block alone fits; with the tools block it would not.
    machine = "THIS MACHINE: " + "m" * (room - 20)
    monkeypatch.setattr(craft, "_ENV_SOURCE", lambda t_: machine)
    monkeypatch.setattr(craft, "_TOOLS_SOURCE", lambda t_: BLOCK)
    body = craft.system_message(text)["content"]
    assert machine in body and BLOCK not in body
    assert len(body) <= craft.BRIEF_CEILING_CHARS
    # Room for both: both ship, the machine block first.
    monkeypatch.setattr(craft, "_ENV_SOURCE", lambda t_: "THIS MACHINE: node 22.")
    body = craft.system_message(text)["content"]
    assert body.index("THIS MACHINE") < body.index(BLOCK)
    # Neither fits: both dropped, the brief is the plain one.
    monkeypatch.setattr(craft, "_ENV_SOURCE", lambda t_: "M" * (room + 10))
    assert len(craft.system_message(text)["content"]) == base


def test_without_a_source_the_brief_is_byte_for_byte_unchanged(monkeypatch):
    import craft
    monkeypatch.setattr(craft, "_TOOLS_SOURCE", None)
    before = craft.system_message("fix the bug in utils.py")["content"]
    monkeypatch.setattr(craft, "_TOOLS_SOURCE", lambda text: "")
    assert craft.system_message("fix the bug in utils.py")["content"] == before


def test_multi_prompts_get_the_block_inside_the_env_budget(app_mod, monkeypatch):
    import swarm_windows
    machine = "THIS MACHINE: node 22. " + "x" * 200
    monkeypatch.setattr(app_mod, "_dp_env_block", lambda goal, who="session": machine)
    out = swarm_windows._env_text("build an online store", "worker")
    assert out.startswith(machine) and BLOCK in out
    assert len(out) <= swarm_windows.ENV_BLOCK_CHARS
    big = "THIS MACHINE: " + "y" * 600
    monkeypatch.setattr(app_mod, "_dp_env_block", lambda goal, who="session": big)
    out = swarm_windows._env_text("build an online store", "planner")
    assert out == big                                            # no room: machine kept


def _msgs(*extra):
    return [{"role": "system", "content": "You are opencode."},
            {"role": "user", "content": "deploy the site to vercel"}] + list(extra)


TOOLS = [{"type": "function", "function": {"name": "bash", "parameters": {}}}]


def test_the_v1_opening_turn_gets_one_note(app_mod):
    out = app_mod._ts_apply_note(_msgs(), TOOLS)
    assert [m["role"] for m in out] == ["system", "system", "user"]
    assert out[1]["content"] == BLOCK
    again = app_mod._ts_apply_note(out, TOOLS)
    assert again is out                                          # never twice


def test_no_note_on_a_continuation_a_tool_free_turn_or_compaction(app_mod):
    cont = _msgs({"role": "assistant", "content": None,
                  "tool_calls": [{"id": "c1", "type": "function",
                                  "function": {"name": "bash", "arguments": "{}"}}]},
                 {"role": "tool", "tool_call_id": "c1", "content": "ok"})
    assert app_mod._ts_apply_note(cont, TOOLS) is cont
    m = _msgs()
    assert app_mod._ts_apply_note(m, None) is m
    comp = [{"role": "user", "content": "Your task is to create a detailed summary of the "
                                        "conversation so far"}]
    assert app_mod.ctxwin.is_compaction_request(comp)
    assert app_mod._ts_apply_note(comp, TOOLS) is comp


def test_no_note_when_the_brief_already_carries_the_block(app_mod):
    m = [{"role": "system", "content": "brief\n\n" + BLOCK},
         {"role": "user", "content": "deploy it"}]
    assert app_mod._ts_apply_note(m, TOOLS) is m
    w = [{"role": "user", "content": "Phase 2 ...\n" + BLOCK}]
    assert app_mod._ts_apply_note(w, TOOLS) is w


def test_the_flag_switches_the_note_off(app_mod, monkeypatch):
    monkeypatch.setattr(app_mod, "_ts_on", lambda: False)
    m = _msgs()
    assert app_mod._ts_apply_note(m, TOOLS) is m


def test_the_real_block_text_respects_the_flag(monkeypatch):
    import app
    monkeypatch.setattr(toolscan, "snapshot", lambda wait=0.0: _snap(
        {"gh": {"version": "2", "login": True}}))
    monkeypatch.setattr(app, "_ts_on", lambda: True)
    assert app._ts_block_text().startswith(toolscan.MARKER)
    monkeypatch.setattr(app, "_ts_on", lambda: False)
    assert app._ts_block_text() == ""


class _Resp:
    headers = {}
    status_code = 200
    text = ""

    def json(self):
        return {"choices": [{"finish_reason": "stop", "message": {
            "role": "assistant", "content": "OK"}}]}

    def close(self):
        pass

    def iter_lines(self, decode_unicode=False):
        return iter(())


def _hop(app_mod, monkeypatch, no_craft=False):
    """One real _upstream_chat hop against a fake provider -> the posted body."""
    posted = []
    monkeypatch.setattr(app_mod.requests, "post",
                        lambda *a, **kw: posted.append(kw.get("json")) or _Resp())
    monkeypatch.setattr(app_mod, "_post_with_header_deadline",
                        lambda deadline, post, **kw: posted.append(kw.get("json")) or _Resp())
    msgs = _msgs()
    payload = {"model": "open", "messages": msgs, "tools": TOOLS}
    if no_craft:
        payload["_no_craft"] = True
    with app_mod.app.test_request_context("/v1/chat/completions"):
        app_mod._ctx_begin({"model": "auto"}, msgs, app_mod._est_tokens(msgs, TOOLS))
        app_mod._upstream_chat("uncloseai", payload, False)
    assert posted, "the hop never posted"
    return posted[-1]


def _count_block(body):
    return sum(str(m.get("content") or "").count(toolscan.MARKER)
               for m in body["messages"])


def test_a_real_hop_sends_the_block_exactly_once(app_mod, monkeypatch):
    import craft
    assert _count_block(_hop(app_mod, monkeypatch)) == 1       # in the craft brief
    monkeypatch.setattr(craft, "_TOOLS_SOURCE", None)
    body = _hop(app_mod, monkeypatch)
    assert _count_block(body) == 1                             # the note stands in
    assert [m["content"] for m in body["messages"]].count(BLOCK) == 1
    assert _count_block(_hop(app_mod, monkeypatch, no_craft=True)) == 0
